import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.jobs.worker_control import ControlListener, HeartbeatPublisher
from app.models.subscription_source import SubscriptionSource
from app.repositories.download_job import DownloadJobRepository
from app.models.task_state import transition_download_job
from app.services.job_manifest import append_manifest_event, update_manifest
from app.services.job_progress import apply_download_progress, apply_import_progress, publish_progress
from app.services.redis_client import get_redis
from app.services.settings import build_effective_gallerydl_config, get_download_defaults
from app.services.subscription_enqueue import mark_source_sync_success

logger = logging.getLogger(__name__)

FALLBACK_TIMEOUT = 6000
FALLBACK_STALL_TIMEOUT = 120
FALLBACK_MAX_RETRIES = 4
FALLBACK_BACKOFF_BASE = 60
FALLBACK_GALLERYDL_RETRIES = 3
FALLBACK_GALLERYDL_TIMEOUT = 30
FALLBACK_GALLERYDL_ABORT = 5


def _parse_progress(stderr: str) -> dict | None:
    """Extract download progress info from gallery-dl stderr."""
    import re as _re
    info = {}
    # Match patterns like "[1/50]" or "Downloading 5/10"
    m = _re.search(r'\[(\d+)/(\d+)\]', stderr)
    if m:
        info["current"] = int(m.group(1))
        info["total"] = int(m.group(2))
    # Match "x images" or "x files"
    m = _re.search(r'(\d+)\s*(?:image|file)s?\s*(?:downloaded|found)', stderr, _re.IGNORECASE)
    if m:
        info["downloaded"] = int(m.group(1))
    return info if info else None

# RQ job timeout — must exceed the longest gallery-dl subprocess timeout,
# otherwise RQ kills the function before subprocess.run finishes.
RQ_JOB_TIMEOUT = 7200  # 2 hours


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _promote_staged_files(stage_root: Path, download_root: Path) -> list[Path]:
    """Move one job's staged files into the canonical tree on the same volume."""
    promoted: list[Path] = []
    if not stage_root.exists():
        return promoted
    for staged in sorted(stage_root.rglob("*")):
        if not staged.is_file():
            continue
        target = download_root / staged.relative_to(stage_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
        promoted.append(target)
    for directory in sorted((p for p in stage_root.rglob("*") if p.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        stage_root.rmdir()
    except OSError:
        pass
    return promoted


async def _read_download_defaults():
    """Read download job defaults from system_settings table."""
    try:
        async with async_session() as db:
            return await get_download_defaults(db)
    except Exception:
        logger.warning("Failed to read download_defaults, using fallbacks", exc_info=True)
    return {}


async def _artifact_counts(download_job_id: UUID) -> tuple[int, int, list[str]]:
    from app.services.artifact_ledger import ArtifactLedger
    async with async_session() as db:
        return await ArtifactLedger(db).counts(download_job_id)


AUTH_ERROR_PATTERNS = [
    (r"(?i)401\s*(unauthorized|error)", "HTTP 401 Unauthorized"),
    (r"(?i)403\s*(forbidden|error)", "HTTP 403 Forbidden"),
    (r"(?i)authentication\s*(required|failed|error)", "Authentication required"),
    (r"(?i)cookie.*(expired|invalid|missing)", "Cookie expired or missing"),
    (r"(?i)token.*(expired|invalid|revoked)", "Token expired or invalid"),
    (r"(?i)login.*(required|failed|error)", "Login required"),
    (r"(?i)no.*valid.*(cookie|session|token|auth)", "No valid credentials"),
]


def _cleanup_temp_config(path: str | None):
    """Remove temp config file."""
    if path:
        try:
            os.unlink(path)
        except Exception:
            pass


def _snapshot_metadata_jsons(source: str) -> set[str]:
    """Return a set of absolute paths to all metadata JSON files for a source.

    Used to detect which files were created by THIS gallery-dl invocation
    via before/after diff; this works regardless of configured directory layout.
    """
    source_root = Path(settings.download_root) / source
    if not source_root.exists():
        return set()
    return {str(p) for p in source_root.rglob("*.json") if p.is_file()}


def _count_new_artifacts(source: str, json_before: set[str]) -> tuple[int, int]:
    """Count NEW metadata JSONs and image files created since snapshot.

    Compares current filesystem against json_before to detect only files
    produced by the current gallery-dl run. Image count is scoped to
    directories containing new JSONs, avoiding files from other creators.
    """
    source_root = Path(settings.download_root) / source
    if not source_root.exists():
        return (0, 0, set())

    IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

    json_after = {str(p) for p in source_root.rglob("*.json") if p.is_file()}
    new_jsons = json_after - json_before

    if not new_jsons:
        return (0, 0, set())

    # Count images only in directories that contain new JSONs
    # (avoids counting files from other creators' past downloads)
    new_dirs = {str(Path(jp).parent) for jp in new_jsons}
    img_count = 0
    for d in new_dirs:
        dir_path = Path(d)
        if not dir_path.exists():
            continue
        for p in dir_path.iterdir():
            if p.is_file() and p.suffix.lower() in IMG_EXTS:
                img_count += 1

    return (len(new_jsons), img_count, new_jsons)


AUTH_WARNING_PATTERNS = [
    (r"(?i)no.*PHPSESSID|no.*cookie.*set", "No auth cookie set (R-18 content may be missed)"),
    (r"(?i)warning.*auth|warning.*login|warning.*cookie|warning.*token", "Auth warning in output"),
    (r"(?i)user.*not found|user.*does not exist|user.*left", "User does not exist or has left"),
    (r"(?i)no\s+(valid|working).*(credential|auth|login|session)", "No valid credentials"),
    (r"(?i)request.*failed|connection.*refused|connection.*error|timeout", "Connection error"),
]


async def _enqueue_import(download_job_id: str, import_error: str | None = None, new_json_paths: set[str] | None = None):
    """Create an import job and enqueue it. Returns import_job_id or None.

    If new_json_paths is provided, stores the file list in Redis so the
    import runner can process exactly those files without re-scanning.
    """
    try:
        from rq import Queue
        from app.services.redis_client import get_redis

        async with async_session() as db:
            repo = DownloadJobRepository(db)
            extra = {"error_log": import_error} if import_error else {}
            import_job = await repo.create_import({
                "download_job_id": UUID(download_job_id),
                "status": "enqueued",
                **extra,
            })
            apply_import_progress(
                import_job,
                "enqueued",
                "Queued; waiting for import worker",
                publish=False,
            )
            download_job = await repo.get(UUID(download_job_id))
            if download_job:
                await repo.update_status(download_job, "importing", import_error)
                apply_download_progress(
                    download_job,
                    "importing",
                    "Import job queued; waiting for import worker",
                    publish=False,
                    import_job_id=str(import_job.id),
                )
                append_manifest_event(download_job, "import_job_created", import_job_id=str(import_job.id), reason=import_error)
            await db.commit()
            import_job_id = str(import_job.id)

        # Store new JSON file paths in Redis for the import runner
        _r = get_redis()
        if new_json_paths:
            try:
                _r.setex(
                    f"import:{import_job_id}:files",
                    86400,  # 24h TTL
                    json.dumps(list(new_json_paths)),
                )
            except Exception:
                logger.warning("Failed to store import file list for %s", import_job_id, exc_info=True)
            # Disk fallback: survives Redis restarts and long queue delays
            try:
                fallback_dir = Path(settings.download_root) / ".import-lists"
                fallback_dir.mkdir(parents=True, exist_ok=True)
                fallback_path = fallback_dir / f"{import_job_id}.json"
                with open(fallback_path, "w") as _ff:
                    json.dump(list(new_json_paths), _ff)
            except Exception:
                logger.warning("Failed to write import file list fallback for %s", import_job_id, exc_info=True)

        Queue(name="imports", connection=_r).enqueue(
            "app.jobs.import_runner.run_import_job", import_job_id,
            job_timeout=RQ_JOB_TIMEOUT)
        publish_progress(
            download_job_id,
            "download",
            {
                "stage": "importing",
                "message": "Import job queued; waiting for import worker",
                "import_job_id": import_job_id,
            },
        )
        publish_progress(
            import_job_id,
            "import",
            {
                "stage": "enqueued",
                "message": "Queued; waiting for import worker",
            },
        )
        logger.info("Enqueued import job %s (recovery) for download %s", import_job_id, download_job_id)
        return import_job_id
    except Exception as e:
        logger.error("Failed to enqueue import for download %s: %s", download_job_id, e)
        return None


async def run_download_job(job_id: str):
    job_uuid = UUID(job_id)

    async with async_session() as db:
        repo = DownloadJobRepository(db)
        job = await repo.get(job_uuid)
        if not job:
            return

        # Guard against duplicate execution or paused jobs
        if job.status in ("downloading", "downloaded", "complete"):
            logger.warning("Job %s already in status %s, skipping", job_id, job.status)
            return
        if job.status == "paused":
            logger.info("Job %s is paused, skipping", job_id)
            return

        await repo.update_status(job, "downloading")
        apply_download_progress(
            job,
            "configuring",
            "Download worker started; preparing gallery-dl config",
        )
        if job.subscription_source_id:
            ss = await db.get(SubscriptionSource, job.subscription_source_id)
            if ss:
                ss.last_attempted_at = datetime.now(timezone.utc)
        await db.commit()

    dl_defaults = await _read_download_defaults()
    dl_timeout = int(dl_defaults.get("timeout_seconds", FALLBACK_TIMEOUT))
    stall_timeout = int(dl_defaults.get("stall_timeout_seconds", FALLBACK_STALL_TIMEOUT))
    max_retries = int(dl_defaults.get("max_retries", FALLBACK_MAX_RETRIES))
    backoff_base = int(dl_defaults.get("retry_backoff_base_seconds", FALLBACK_BACKOFF_BASE))
    gdl_retries = int(dl_defaults.get("gallerydl_retries", FALLBACK_GALLERYDL_RETRIES))
    gdl_timeout = int(dl_defaults.get("gallerydl_timeout", FALLBACK_GALLERYDL_TIMEOUT))
    gdl_abort = int(dl_defaults.get("gallerydl_abort", FALLBACK_GALLERYDL_ABORT))

    skip_ai = dl_defaults.get("skip_ai_generated", False)
    ai_config_path = None
    job_config_path = None
    source_url = job.source_url

    # Write per-job gallery-dl config with provider defaults plus admin gallery-dl settings.
    try:
        from app.providers import registry as _reg
        _prov = _reg.get(job.source)
        _provider_cfg = _prov.build_gallerydl_config(None)
        _cfg = build_effective_gallerydl_config(job.source, _provider_cfg)
        if _cfg:
            job_config_path = os.path.join(
                os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"),
                "jobs", f"job-{job_id}.json")
            os.makedirs(os.path.dirname(job_config_path), exist_ok=True)
            with open(job_config_path, "w") as f:
                json.dump(_cfg, f)
            async with async_session() as _cfg_db:
                from app.repositories.download_job import DownloadJobRepository as _DJRepo
                _cfg_j = await _DJRepo(_cfg_db).get(job_uuid)
                if _cfg_j:
                    _cfg_j.gallerydl_config_path = job_config_path
                    update_manifest(_cfg_j, gallerydl_config_path=job_config_path, effective_gallerydl_config=_cfg)
                    append_manifest_event(_cfg_j, "effective_config_written", path=job_config_path)
                    await _cfg_db.commit()
        # Record creator dir so import_runner can scope its scan
        try:
            _creator_dir = _prov.get_creator_dir_from_url(job.source_url)
            if _creator_dir and not job.download_dir:
                async with async_session() as _dir_db:
                    from app.repositories.download_job import DownloadJobRepository as _DJRepo
                    _dir_j = await _DJRepo(_dir_db).get(job_uuid)
                    if _dir_j and not _dir_j.download_dir:
                        _dir_j.download_dir = _creator_dir
                        update_manifest(_dir_j, download_dir=_creator_dir)
                        await _dir_db.commit()
        except Exception:
            logger.debug("Failed to record download_dir for job %s", job_id, exc_info=True)
    except Exception:
        logger.debug("Failed to write per-job config for %s", job_id, exc_info=True)

    # Per-source AI filtering
    if skip_ai:
        if job.source == "pixiv":
            # Pixiv: use gallery-dl extractor config "ai-type": 1 (non-AI only)
            ai_config_path = os.path.join(
                os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"),
                "jobs", f"ai-filter-{job_id}.json")
            os.makedirs(os.path.dirname(ai_config_path), exist_ok=True)
            try:
                with open(ai_config_path, "w") as f:
                    json.dump({"extractor": {"pixiv": {"ai-type": 1}}}, f)
            except Exception:
                logger.warning("Failed to write AI filter config for job %s", job_id, exc_info=True)
                ai_config_path = None

        elif job.source == "danbooru":
            # Danbooru: exclude ai_generated tag from search URL
            import urllib.parse as url_parse
            parsed = url_parse.urlparse(source_url)
            params = url_parse.parse_qs(parsed.query)
            tags = params.get("tags", [""])[0]
            if tags and "-ai_generated" not in tags:
                tags += "+-ai_generated"
                new_query = url_parse.urlencode({"tags": tags}, doseq=True)
                source_url = url_parse.urlunparse(parsed._replace(query=new_query))

    async with async_session() as _progress_db:
        _progress_job = await DownloadJobRepository(_progress_db).get(job_uuid)
        if _progress_job:
            apply_download_progress(
                _progress_job,
                "configuring",
                "Gallery-dl config ready; applying source options",
            )
            await _progress_db.commit()

    result = None
    stderr_lines = deque(maxlen=2000)
    stdout_lines = deque(maxlen=2000)
    proc = None
    control_listener = None
    heartbeat = None

    try:
        config_path = os.path.join(
            os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "config.json")

        cmd = ["gallery-dl"]
        if os.path.exists(config_path):
            cmd.extend(["--config", config_path])
        if job_config_path:
            cmd.extend(["--config", job_config_path])
        if ai_config_path:
            cmd.extend(["--config", ai_config_path])

        archive_path = os.path.join(
            str(settings.download_root), f"archive-{job.source}.sqlite3")
        cmd.extend(["--download-archive", archive_path])

        max_posts = dl_defaults.get("max_posts") or dl_defaults.get("sync_batch_size", 200)
        cmd.extend(["--range", f"1-{max_posts}"])

        # gallery-dl internal controls: per-request retry and abort.
        # NOTE: --timeout is NOT a valid CLI flag in older gallery-dl versions;
        # per-request HTTP timeout is set via the config file instead.
        cmd.extend(["--retries", str(gdl_retries)])
        cmd.extend(["--abort", str(gdl_abort)])

        cmd.extend([
            "--destination", str(settings.download_root),
            source_url,
        ])

        env = os.environ.copy()
        proxy_enabled = False
        try:
            from app.services.proxy import _load_proxy_config, get_proxy_env
            proxy_config = await _load_proxy_config()
            proxy_enabled = proxy_config.get("enabled", False)
            env.update(get_proxy_env(proxy_config))
        except Exception:
            logger.warning("Failed to apply proxy env for download job %s", job_id, exc_info=True)

        # ── Pre-flight check: test proxy / DNS reachability ──
        preflight_warnings: list[str] = []
        try:
            from urllib.parse import urlparse as _urlparse
            import socket as _socket

            # DNS check for source host
            try:
                src_host = _urlparse(source_url).hostname
                if src_host:
                    _socket.getaddrinfo(src_host, 443, proto=_socket.IPPROTO_TCP)
                    logger.debug("Pre-flight DNS OK for %s", src_host)
            except Exception:
                msg = f"DNS resolution failed for source host"
                preflight_warnings.append(msg)
                logger.warning("Pre-flight: %s", msg)

            # Proxy reachability check
            if proxy_enabled:
                proxy_url = proxy_config.get("https_proxy") or proxy_config.get("http_proxy", "")
                if proxy_url:
                    try:
                        proxy_host = _urlparse(proxy_url).hostname
                        proxy_port = _urlparse(proxy_url).port or 8080
                        if proxy_host:
                            _socket.getaddrinfo(proxy_host, proxy_port, proto=_socket.IPPROTO_TCP)
                            logger.debug("Pre-flight: proxy %s:%d resolvable", proxy_host, proxy_port)
                    except Exception:
                        msg = f"Proxy host unreachable: {proxy_url}"
                        preflight_warnings.append(msg)
                        logger.warning("Pre-flight: %s", msg)
        except Exception:
            logger.debug("Pre-flight check skipped", exc_info=True)

        # Record proxy health in Redis for scheduler awareness
        if preflight_warnings:
            try:
                r_ph = get_redis()
                r_ph.hset(f"proxy:health:{job.source}", mapping={
                    "last_check": _now_iso(),
                    "status": "degraded",
                    "warnings": ",".join(preflight_warnings),
                })
                r_ph.expire(f"proxy:health:{job.source}", 3600)
            except Exception:
                pass
        elif preflight_warnings is not None:
            try:
                r_ph = get_redis()
                r_ph.hset(f"proxy:health:{job.source}", mapping={
                    "last_check": _now_iso(),
                    "status": "healthy",
                    "warnings": "",
                })
                r_ph.expire(f"proxy:health:{job.source}", 3600)
            except Exception:
                pass

        async with async_session() as _progress_db:
            _progress_job = await DownloadJobRepository(_progress_db).get(job_uuid)
            if _progress_job:
                apply_download_progress(
                    _progress_job,
                    "downloading",
                    "Starting gallery-dl",
                )
                await _progress_db.commit()

        async with async_session() as _manifest_db:
            _manifest_repo = DownloadJobRepository(_manifest_db)
            _manifest_job = await _manifest_repo.get(job_uuid)
            if _manifest_job:
                update_manifest(
                    _manifest_job,
                    command=cmd,
                    archive_path=archive_path,
                    range=f"1-{max_posts}",
                    proxy_enabled=proxy_enabled,
                    preflight_warnings=preflight_warnings if preflight_warnings else None,
                )
                append_manifest_event(_manifest_job, "gallerydl_started", source_url=source_url)
                apply_download_progress(
                    _manifest_job,
                    "downloading",
                    "Gallery-dl is running",
                )
                await _manifest_db.commit()

        logger.info("Running gallery-dl: %s (timeout=%ds, proxy=%s)", " ".join(cmd), dl_timeout, proxy_enabled)

        # ── Start gallery-dl in its own process group ──
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # process group isolation for safe kill
            env=env,
        )

        # ── Start control listener + heartbeat ──
        control_listener = ControlListener(job_id, proc_pid=proc.pid)
        control_listener.start()
        heartbeat = HeartbeatPublisher(job_id, "download", pid=proc.pid)
        heartbeat.start()

        # ── Stream stderr for progress + collection ──
        progress_pattern = re.compile(r"\[(\d+)/(\d+)\]")
        last_progress_publish = 0.0
        last_progress_time = time.time()  # shared with main thread (GLock)
        progress_lock = threading.Lock()

        def read_output(pipe, sink, parse_progress=False):
            nonlocal last_progress_time, last_progress_publish
            for line in iter(pipe.readline, ""):
                sink.append(line)
                now = time.time()
                with progress_lock:
                    last_progress_time = now
                if not parse_progress:
                    continue
                # Parse and publish progress
                m = progress_pattern.search(line)
                if m:
                    current, total = int(m.group(1)), int(m.group(2))
                    progress = {
                        "stage": "downloading",
                        "current": current,
                        "total": total,
                        "percent": round(current / total * 100, 1) if total else 0,
                        "message": line.strip()[:200],
                    }
                    # Publish throttled (max 2/sec to avoid flooding)
                    if now - last_progress_publish >= 0.5:
                        try:
                            publish_progress(job_id, "download", progress)
                        except Exception:
                            pass
                        last_progress_publish = now
        stderr_thread = threading.Thread(target=read_output, args=(proc.stderr, stderr_lines, True), daemon=True)
        stdout_thread = threading.Thread(target=read_output, args=(proc.stdout, stdout_lines, False), daemon=True)
        stderr_thread.start()
        stdout_thread.start()

        # ── Wait for completion with dual timeout (overall + stall) ──
        STALL_GRACE_PERIOD = 30  # grace period before stall detection kicks in
        POLL_INTERVAL = 5  # seconds between polls
        download_start = time.time()

        # Adaptive timeout: increase on retries (1.0x, 1.5x, 2.0x)
        retry_multiplier = 1 + (job.retry_count * 0.5)
        effective_dl_timeout = int(dl_timeout * retry_multiplier)
        effective_stall_timeout = int(stall_timeout * retry_multiplier)
        if retry_multiplier > 1:
            logger.info("Retry %d: adaptive timeout dl=%ds stall=%ds (base dl=%ds stall=%ds)",
                       job.retry_count, effective_dl_timeout, effective_stall_timeout,
                       dl_timeout, stall_timeout)

        deadline = download_start + effective_dl_timeout
        timed_out = False
        stalled = False
        stall_elapsed = 0
        result = None  # set below after wait completes

        try:
            while proc.poll() is None:
                # Check for pause/cancel signals from control listener
                if control_listener.command in ("pause", "cancel"):
                    break

                now_ts = time.time()
                time_since_start = now_ts - download_start
                # 1) Overall deadline check
                if now_ts > deadline:
                    timed_out = True
                    logger.warning("gallery-dl timed out after %ds for job %s", effective_dl_timeout, job_id)
                    break
                # 2) Stall detection (only after grace period)
                time_since_progress = now_ts - last_progress_time
                if time_since_progress > effective_stall_timeout and time_since_start > STALL_GRACE_PERIOD:
                    stalled = True
                    stall_elapsed = int(time_since_start)
                    logger.warning("gallery-dl stalled (%ds no progress, elapsed %ds) for job %s, killing early",
                                   effective_stall_timeout, stall_elapsed, job_id)
                    break

                time.sleep(POLL_INTERVAL)

            if timed_out or stalled:
                # Kill the process group
                try:
                    os.killpg(os.getpgid(proc.pid), 2)  # SIGINT first
                    time.sleep(2)
                    os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
                except (ProcessLookupError, OSError):
                    pass
                stderr_thread.join(timeout=5)
                stdout_thread.join(timeout=5)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                result = None  # signals timeout/stall to downstream handlers
            else:
                # Normal completion or pause/cancel
                stderr_thread.join(timeout=5)
                stdout_thread.join(timeout=5)
                stdout_text = "".join(stdout_lines)
                stderr_text = "".join(stderr_lines)
                returncode = proc.returncode

                if control_listener.command == "pause":
                    result = None  # signal "paused" to downstream handlers
                    logger.info("Download job %s was paused during gallery-dl execution", job_id)
                elif control_listener.command == "cancel":
                    result = None  # signal "cancelled"
                    logger.info("Download job %s was cancelled during gallery-dl execution", job_id)
                else:
                    # Normal completion
                    result = subprocess.CompletedProcess(
                        cmd, returncode, stdout=stdout_text, stderr=stderr_text
                    )
                    logger.info("gallery-dl exit=%d, stdout=%d bytes, stderr=%d bytes",
                                returncode, len(stdout_text), len(stderr_text))
                    if returncode != 0:
                        logger.warning("gallery-dl stderr (last 500): %s", stderr_text[-500:] if stderr_text else "(none)")

        except Exception:
            # Unexpected error during poll loop — kill process and collect stderr
            logger.warning("Unexpected error during download poll loop for job %s", job_id, exc_info=True)
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                pass
            stderr_thread.join(timeout=5)
            stdout_thread.join(timeout=5)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            result = None

        # ── Record gallery-dl result in manifest ──
        async with async_session() as _manifest_db:
            _manifest_job = await DownloadJobRepository(_manifest_db).get(job_uuid)
            if _manifest_job:
                apply_download_progress(
                    _manifest_job,
                    "post_download",
                    "Gallery-dl finished; scanning downloaded metadata",
                )
                if result is not None and control_listener.command is None:
                    update_manifest(
                        _manifest_job,
                        gallerydl_returncode=result.returncode,
                        stdout_bytes=len(result.stdout or ""),
                        stderr_bytes=len(result.stderr or ""),
                    )
                    append_manifest_event(_manifest_job, "gallerydl_finished", returncode=result.returncode)
                elif control_listener.command == "pause":
                    append_manifest_event(_manifest_job, "gallerydl_interrupted", reason="paused")
                elif control_listener.command == "cancel":
                    append_manifest_event(_manifest_job, "gallerydl_interrupted", reason="cancelled")
                else:
                    update_manifest(_manifest_job, gallerydl_timeout_seconds=dl_timeout)
                    append_manifest_event(_manifest_job, "gallerydl_timeout", timeout_seconds=dl_timeout)
                await _manifest_db.commit()

            # Register newly downloaded files via filesystem scan.
            # Scope to download_dir when available to avoid picking up files
            # from other creators' directories (cross-contamination bug).
            from app.services.artifact_ledger import ArtifactLedger, artifact_row
            source_root = Path(settings.download_root) / job.source
            if job.download_dir:
                scan_root = source_root / job.download_dir
            else:
                scan_root = source_root
            rows = []
            seen = set()
            if scan_root.exists():
                for jf in scan_root.rglob("*.json"):
                    if jf.is_file() and jf.parent != scan_root:
                        for af in jf.parent.iterdir():
                            if af.is_file() and af.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".zip"}:
                                ar = artifact_row(af, Path(settings.download_root), job_uuid)
                                if ar and ar["file_path"] not in seen:
                                    seen.add(ar["file_path"])
                                    rows.append(ar)
                        row = artifact_row(jf, Path(settings.download_root), job_uuid)
                        if row and row["file_path"] not in seen:
                            seen.add(row["file_path"])
                            rows.append(row)
            _registered = await ArtifactLedger(_manifest_db).upsert_many(rows)
            await _manifest_db.commit()
            logger.info("Registered %d artifacts for job %s", _registered, job_id)
            logger.info("Registered %d staged artifacts for job %s", _registered, job_id)

    except Exception as e:
        logger.error("Unexpected error in download job %s: %s", job_id, e, exc_info=True)
        async with async_session() as db2:
            repo2 = DownloadJobRepository(db2)
            j = await repo2.get(job_uuid)
            if j:
                j.retry_count += 1
                error_text = str(e)[:10000]
                if j.retry_count < max_retries:
                    j.last_heartbeat_at = None  # reset heartbeat for fresh retry
                    transition_download_job(j, "failed", f"unexpected error: {error_text}")
                    transition_download_job(j, "enqueued")
                    append_manifest_event(j, "status_changed", from_status="failed", to_status="enqueued", action="retry")
                    apply_download_progress(
                        j,
                        "enqueued",
                        f"Retry queued after unexpected error: {error_text[:180]}",
                    )
                else:
                    await repo2.update_status(j, "failed", f"unexpected error: {error_text}")
                    apply_download_progress(
                        j,
                        "failed",
                        f"Download failed: {error_text[:180]}",
                    )
                await db2.commit()

        # Always try partial import recovery
        metadata_count, image_count, new_json_paths = await _artifact_counts(job_uuid)
        if metadata_count > 0:
            logger.info("Partial recovery: found %d metadata JSONs after error for job %s", metadata_count, job_id)
            await _enqueue_import(str(job_uuid), f"partial import after unexpected error (found {metadata_count} metadata files)")
        _cleanup_temp_config(ai_config_path)
        _cleanup_temp_config(job_config_path)
        return

    finally:
        # ── Stop control listener + heartbeat ──
        if heartbeat:
            heartbeat.stop()
        if control_listener:
            control_listener.stop()

    # ── Cleanup temp configs ──
    _cleanup_temp_config(ai_config_path)
    _cleanup_temp_config(job_config_path)

    # ── Handle subprocess result ──

    async with async_session() as db2:
        repo2 = DownloadJobRepository(db2)
        j = await repo2.get(job_uuid)
        if not j:
            return

        ctrl_cmd = control_listener.command if control_listener else None

        if ctrl_cmd == "pause":
            # TaskEngine already set status to "paused" — don't touch status here.
            # Don't increment retry_count — this was a user action, not a failure.
            logger.info("Download job %s paused by user signal", job_id)

        elif ctrl_cmd == "cancel":
            # TaskEngine already set status to "cancelled" — terminal, no retry.
            logger.info("Download job %s cancelled by user signal", job_id)

        elif result is not None:
            # Normal completion (success or non-zero exit)
            if result.returncode == 0:
                await repo2.update_status(j, "downloaded")
                apply_download_progress(
                    j,
                    "downloaded",
                    "Download complete; waiting for metadata scan",
                )
            else:
                j.retry_count += 1
                if j.retry_count < max_retries:
                    j.last_heartbeat_at = None  # reset heartbeat for fresh retry
                    err_msg = result.stderr[:5000] if result.stderr else None
                    transition_download_job(j, "failed", err_msg)
                    transition_download_job(j, "enqueued")
                    append_manifest_event(j, "status_changed", from_status="failed", to_status="enqueued", action="retry")
                    apply_download_progress(
                        j,
                        "enqueued",
                        "Retry queued after gallery-dl returned an error",
                    )
                else:
                    await repo2.update_status(j, "failed", result.stderr[:5000] if result.stderr else None)
                    apply_download_progress(
                        j,
                        "failed",
                        "Download failed after gallery-dl error",
                    )

            # Auth health monitoring — scan stderr regardless of exit code
            if j.subscription_source_id:
                ss = await db2.execute(
                    select(SubscriptionSource).where(SubscriptionSource.id == j.subscription_source_id)
                )
                source = ss.scalar_one_or_none()
                if source:
                    combined = (result.stderr or "") + (result.stdout or "")
                    auth_issue = None
                    for pattern, label in AUTH_ERROR_PATTERNS:
                        if re.search(pattern, combined):
                            auth_issue = label
                            break
                    if result.returncode == 0 and not auth_issue:
                        source.last_successful_auth = datetime.now(timezone.utc)
                        source.auth_healthy = True
                        source.auth_status = "healthy"
                        source.auth_error_reason = None
                        source.last_auth_checked_at = datetime.now(timezone.utc)
                    elif auth_issue:
                        source.auth_healthy = False
                        source.auth_status = "unhealthy"
                        source.auth_error_reason = auth_issue
                        source.last_auth_checked_at = datetime.now(timezone.utc)
                        logger.warning("Auth issue for subscription_source %s: %s", source.id, auth_issue)

        else:
            # Timeout or stall — use distinguished error message
            j.retry_count += 1
            if stalled:
                timeout_msg = f"stalled: no progress for {effective_stall_timeout}s (elapsed {stall_elapsed}s)"
            else:
                timeout_msg = f"timeout after {effective_dl_timeout}s"
            if j.retry_count < max_retries:
                j.last_heartbeat_at = None  # reset heartbeat for fresh retry
                transition_download_job(j, "failed", timeout_msg)
                transition_download_job(j, "enqueued")
                append_manifest_event(j, "status_changed", from_status="failed", to_status="enqueued", action="retry")
                apply_download_progress(
                    j,
                    "enqueued",
                    f"Retry queued after timeout ({dl_timeout}s)",
                )
            else:
                await repo2.update_status(j, "failed", timeout_msg)
                apply_download_progress(
                    j,
                    "failed",
                    f"Download failed after timeout ({dl_timeout}s)",
                )

        await db2.commit()

    # ── Enqueue import on success, auto-retry on failure, partial recovery on timeout ──

    if result is not None and result.returncode == 0:
        # Full success — but only enqueue import if there are new metadata JSONs.
        # gallery-dl exits 0 even when all files were skipped (already in archive),
        # or when the source has no content at all.
        metadata_count, image_count, new_json_paths = await _artifact_counts(job_uuid)
        async with async_session() as _manifest_db:
            _manifest_job = await DownloadJobRepository(_manifest_db).get(job_uuid)
            if _manifest_job:
                update_manifest(_manifest_job, metadata_json_count=metadata_count, image_count=image_count)
                append_manifest_event(_manifest_job, "artifacts_counted", metadata_json_count=metadata_count, image_count=image_count)
                apply_download_progress(
                    _manifest_job,
                    "post_download",
                    f"Found {metadata_count} metadata files and {image_count} media files",
                    current=metadata_count,
                    total=metadata_count or None,
                    assets=image_count,
                )
                await _manifest_db.commit()
        if metadata_count > 0:
            async with async_session() as _progress_db:
                _progress_job = await DownloadJobRepository(_progress_db).get(job_uuid)
                if _progress_job:
                    apply_download_progress(
                        _progress_job,
                        "enqueuing_import",
                        f"Found {metadata_count} works; queuing import",
                        current=0,
                        total=metadata_count,
                        assets=image_count,
                    )
                    await _progress_db.commit()
            await _enqueue_import(str(job_uuid), new_json_paths=new_json_paths)
        else:
            # No new metadata JSONs — distinguish "already imported" from "nothing to download"
            # Check stderr for warnings that explain the zero-download result
            stderr_text = result.stderr or ""
            stdout_text = result.stdout or ""
            combined = stderr_text + stdout_text
            auth_warning = None
            for pattern, label in AUTH_WARNING_PATTERNS:
                if re.search(pattern, combined):
                    auth_warning = label
                    break

            # Determine status — then commit in single session
            if image_count == 0 and auth_warning:
                new_status = "failed"
                new_error = f"Download produced 0 files: {auth_warning}\n{stderr_text[:2000]}"
            elif image_count == 0:
                new_status = "complete"
                new_error = "Source has no downloadable content or all content is restricted"
            else:
                new_status = "complete"
                new_error = None

            async with async_session() as db3:
                repo3 = DownloadJobRepository(db3)
                j3 = await repo3.get(job_uuid)
                if j3:
                    await repo3.update_status(j3, new_status, new_error)
                    apply_download_progress(
                        j3,
                        "complete" if new_status == "complete" else "failed",
                        new_error or "Download complete; no new metadata to import",
                        assets=image_count,
                    )
                    if new_status == "complete" and j3.subscription_source_id:
                        await mark_source_sync_success(db3, j3.subscription_source_id)
                    await db3.commit()

    elif result is not None and result.returncode != 0:
        # Non-zero exit — maybe partial files were downloaded
        metadata_count, image_count, new_json_paths = await _artifact_counts(job_uuid)
        if metadata_count > 0:
            logger.info("Partial recovery: found %d metadata JSONs after failure for job %s", metadata_count, job_id)
            await _enqueue_import(str(job_uuid), f"partial import after download failure (found {metadata_count} metadata files)")

    else:
        # Timeout or interrupted (pause/cancel) — attempt partial import recovery
        ctrl_cmd = control_listener.command if control_listener else None
        reason = "timeout" if ctrl_cmd is None else f"interrupted ({ctrl_cmd})"
        metadata_count, image_count, new_json_paths = await _artifact_counts(job_uuid)
        if metadata_count > 0:
            logger.info("Partial recovery: found %d metadata JSONs after %s for job %s", metadata_count, reason, job_id)
            await _enqueue_import(str(job_uuid),
                                 f"partial import after {reason} (found {metadata_count} metadata files)",
                                 new_json_paths=new_json_paths)

    # ── Enqueue retry for transient failures ──
    # Only auto-retry on actual failures (non-zero exit, timeout), NOT on pause/cancel.

    ctrl_cmd = control_listener.command if control_listener else None
    if j and j.retry_count < max_retries and ctrl_cmd is None:
        # Only auto-retry non-zero exits and timeouts
        needs_retry = (result is None) or (result.returncode != 0)
        if needs_retry:
            try:
                from rq import Queue
                Queue(name="downloads", connection=get_redis()).enqueue_in(
                    timedelta(seconds=backoff_base * (2 ** (j.retry_count - 1))),
                    "app.jobs.download.run_download_job", job_id,
                    job_timeout=RQ_JOB_TIMEOUT)
                logger.info("Enqueued retry %d/%d for job %s in %ds",
                           j.retry_count, max_retries, job_id,
                           backoff_base * (2 ** (j.retry_count - 1)))
            except Exception:
                logger.warning("Failed to enqueue retry for download job %s", job_id, exc_info=True)
