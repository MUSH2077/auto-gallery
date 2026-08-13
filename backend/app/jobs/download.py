import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.jobs.download_outcome import classify_no_metadata_outcome
from app.jobs.worker_control import (
    ControlListener,
    HeartbeatPublisher,
    signal_process_group,
)
from app.models.subscription_source import SubscriptionSource
from app.repositories.download_job import DownloadJobRepository
from app.models.task_state import transition_download_job
from app.jobs.stage_timing import stage_timer
from app.services.job_manifest import append_manifest_event, update_manifest
from app.services.job_progress import apply_download_progress, apply_import_progress, publish_progress
from app.services.download_finalization import finalize_download_job
from app.services.download_dispatch import prepare_download_dispatch, publish_prepared_download
from app.services.import_dispatch import prepare_import_dispatch, publish_prepared_import
from app.services.redis_client import get_redis
from app.services.settings import (
    build_effective_gallerydl_config,
    extractor_key_for_source,
    get_download_defaults,
)
from app.services.artifact_discovery import group_metadata_by_work, media_files_for_group
from app.services.download_staging import (
    DownloadStage,
    DownloadStageConflict,
    DownloadStageDiscoveryError,
    DownloadStageManifestError,
    staging_enabled,
    validate_gallerydl_staging_config,
)
from app.services.sync_outcome import build_sync_outcome, had_sync_baseline
from app.services.heavy_io import heavy_io_async_job
from app.services.stage_metrics import measured_async_job
from app.services.search_projection_outbox import request_search_projection

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
    """Compatibility wrapper for safe, recoverable staging promotion."""
    if not stage_root.exists():
        return []
    stage = DownloadStage.from_existing(stage_root, download_root)
    promotion = stage.promote()
    stage.mark_registered()
    return list(promotion.paths)


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


async def _download_source_identity(job_id: str) -> str | None:
    """Serialize writers of the same gallery-dl archive by source key."""

    try:
        async with async_session() as db:
            job = await DownloadJobRepository(db).get(UUID(job_id))
            if not job:
                return None
            return f"gallerydl-archive:{job.source}"
    except Exception:
        logger.warning("Could not resolve source lease identity for %s", job_id, exc_info=True)
        return f"download-job:{job_id}"


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stop_gallerydl_process(
    proc: subprocess.Popen,
    *,
    initial_signal: int = signal.SIGTERM,
    graceful_timeout: float = 5.0,
    kill_timeout: float = 10.0,
) -> int:
    """Stop, escalate and reap gallery-dl before files can be promoted.

    ``start_new_session=True`` makes the process id the process-group id.  We
    check the group after reaping the leader as gallery-dl may have spawned
    ffmpeg or another helper that outlives it.
    """

    process_group_id = proc.pid
    if proc.poll() is None:
        signal_process_group(proc.pid, initial_signal)
        try:
            proc.wait(timeout=graceful_timeout)
        except subprocess.TimeoutExpired:
            signal_process_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=kill_timeout)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"gallery-dl pid {proc.pid} did not exit after SIGKILL"
                ) from exc

    # The group leader may exit before one of its helpers.  Use the stable
    # start-new-session group id directly, then wait briefly for the group to
    # disappear.  Promotion is forbidden until this succeeds.
    if _process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + kill_timeout
        while _process_group_exists(process_group_id) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _process_group_exists(process_group_id):
            raise RuntimeError(
                f"gallery-dl process group {process_group_id} survived SIGKILL"
            )
    if proc.poll() is None:
        raise RuntimeError(f"gallery-dl pid {proc.pid} was not reaped")
    return int(proc.returncode)


async def _enqueue_download_retry(
    download_job_id: UUID,
    *,
    delay_seconds: int,
    action: str = "auto_retry",
) -> bool:
    """Durably prepare and publish one delayed retry through the hard cap."""

    async with async_session() as retry_db:
        retry_job = await DownloadJobRepository(retry_db).get(download_job_id)
        if not retry_job or retry_job.status != "enqueued":
            return False
        prepared = await prepare_download_dispatch(
            retry_db,
            retry_job,
            queue_name="downloads",
            job_timeout=RQ_JOB_TIMEOUT,
            delay_seconds=delay_seconds,
            action=action,
        )
        await publish_prepared_download(
            retry_db,
            retry_job,
            prepared,
            job_timeout=RQ_JOB_TIMEOUT,
            delay_seconds=delay_seconds,
            action=action,
        )
    return True


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


AUTH_WARNING_PATTERNS = [
    (r"(?i)no.*PHPSESSID|no.*cookie.*set", "No auth cookie set (R-18 content may be missed)"),
    (r"(?i)warning.*auth|warning.*login|warning.*cookie|warning.*token", "Auth warning in output"),
    (r"(?i)user.*not found|user.*does not exist|user.*left", "User does not exist or has left"),
    (r"(?i)no\s+(valid|working).*(credential|auth|login|session)", "No valid credentials"),
    (r"(?i)request.*failed|connection.*refused|connection.*error|timeout", "Connection error"),
]


async def _enqueue_import(download_job_id: str, import_error: str | None = None, new_json_paths: set[str] | None = None):
    """Create a durable import publication intent. Returns its import job id.

    If new_json_paths is provided, stores the file list in Redis so the
    import runner can process exactly those files without re-scanning.

    PostgreSQL is committed before RQ publication.  Redis pressure or a short
    disconnect therefore leaves the ImportJob/TaskRun enqueued for the import
    recovery loop instead of incorrectly failing the completed download.
    """
    import_job_id = None
    try:
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
            from app.services.tasks import TaskService
            task_svc = TaskService(db)
            parent_task = None
            if download_job:
                parent_task = await task_svc.ensure_download_task(download_job)
                await task_svc.update_task(parent_task, status="running", progress=download_job.progress_data)
            prepared = await prepare_import_dispatch(
                db,
                import_job,
                parent_task_id=parent_task.id if parent_task else None,
                job_timeout=RQ_JOB_TIMEOUT,
            )
            await db.commit()
            import_job_id = str(import_job.id)
            rq_job_id = prepared.rq_job_id
    except Exception as exc:
        logger.error(
            "Failed to create import publication for download %s: %s",
            download_job_id,
            exc,
            exc_info=True,
        )
        raise RuntimeError(
            f"Could not create import job for download {download_job_id}"
        ) from exc

    # Store new JSON file paths in Redis for the import runner.  The disk copy
    # remains authoritative when Redis is unavailable or publication is delayed.
    try:
        redis_client = get_redis()
    except Exception:
        redis_client = None
        logger.warning(
            "Redis client unavailable while publishing import %s; durable recovery will retry",
            import_job_id,
            exc_info=True,
        )
    if new_json_paths:
        if redis_client is not None:
            try:
                redis_client.setex(
                    f"import:{import_job_id}:files",
                    86400,  # 24h TTL
                    json.dumps(list(new_json_paths)),
                )
            except Exception:
                logger.warning("Failed to store import file list for %s", import_job_id, exc_info=True)
        try:
            fallback_dir = Path(settings.download_root) / ".import-lists"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            fallback_path = fallback_dir / f"{import_job_id}.json"
            with open(fallback_path, "w") as _ff:
                json.dump(list(new_json_paths), _ff)
        except Exception:
            logger.warning("Failed to write import file list fallback for %s", import_job_id, exc_info=True)

    try:
        async with async_session() as publication_db:
            publication = await publish_prepared_import(
                publication_db,
                import_job_id,
                rq_job_id,
                redis_client=redis_client,
            )
    except Exception:
        # The durable pending intent is sufficient for periodic recovery.  This
        # catch also covers an ambiguous failure after RQ accepted the job.
        publication = "deferred"
        logger.warning(
            "Import publication will be recovered job=%s download=%s",
            import_job_id,
            download_job_id,
            exc_info=True,
        )

    waiting_for_redis = publication == "deferred"
    if publication == "invalid":
        download_stage = "failed"
        import_stage = "failed"
        download_message = "Import queue publication failed; operator action required"
        import_message = download_message
    elif waiting_for_redis:
        download_stage = "importing"
        import_stage = "enqueued"
        download_message = "Import job saved; waiting for queue capacity"
        import_message = "Waiting for queue capacity; publication will retry"
    else:
        download_stage = "importing"
        import_stage = "enqueued"
        download_message = "Import job queued; waiting for import worker"
        import_message = "Queued; waiting for import worker"
    try:
        publish_progress(
            download_job_id,
            "download",
            {
                "stage": download_stage,
                "message": download_message,
                "import_job_id": import_job_id,
            },
        )
        publish_progress(
            import_job_id,
            "import",
            {
                "stage": import_stage,
                "message": import_message,
            },
        )
    except Exception:
        logger.warning(
            "Import %s durable publication=%s but progress notification failed",
            import_job_id,
            publication,
            exc_info=True,
        )
    logger.info(
        "Import publication=%s job=%s download=%s",
        publication,
        import_job_id,
        download_job_id,
    )
    return import_job_id


@heavy_io_async_job("download", source_identity_resolver=_download_source_identity)
@measured_async_job("download_network")
async def run_download_job(job_id: str):
    job_uuid = UUID(job_id)

    async with async_session() as db:
        repo = DownloadJobRepository(db)
        job = await repo.get(job_uuid)
        if not job:
            return

        # The job can remain queued while its heavy worker waits for the global
        # flock.  Re-read durable state at the actual execution boundary so a
        # pause/cancel/duplicate signal written during that wait wins the race.
        if job.status not in ("enqueued", "pending"):
            logger.info(
                "Download job %s is no longer runnable (status=%s); skipping",
                job_id,
                job.status,
            )
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
    provider = None
    provider_chunk = None
    configuration_error: DownloadStageManifestError | None = None

    # Write per-job gallery-dl config with provider defaults plus admin gallery-dl settings.
    try:
        from app.providers import registry as _reg
        _prov = _reg.get(job.source)
        provider = _prov
        _provider_cfg = _prov.build_gallerydl_config(None)
        _cfg = build_effective_gallerydl_config(job.source, _provider_cfg)
        if staging_enabled():
            validate_gallerydl_staging_config(_cfg)
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
    except DownloadStageManifestError as exc:
        # An unsafe output template can place files outside this job's staged
        # tree.  Continuing without the rejected per-job config would turn a
        # safety violation into an implicit legacy download.  Defer the error
        # to the normal terminal staging-error path so durable job state and
        # its manifest are updated consistently.
        configuration_error = exc
        logger.error("Rejected unsafe gallery-dl config for %s: %s", job_id, exc)
    except Exception:
        logger.warning("Failed to write per-job config for %s", job_id, exc_info=True)

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
    download_stage: DownloadStage | None = None

    try:
        if configuration_error is not None:
            raise configuration_error
        download_root = Path(settings.download_root)
        download_destination = download_root
        if staging_enabled():
            # Fail closed on every staging initialization error.  The legacy
            # whole-source scan is reachable only through the explicit
            # DOWNLOAD_STAGING_ENABLED=false compatibility switch.
            download_stage = DownloadStage.open(download_root, job_id, job.source)
            download_stage.mark_running()
            download_destination = download_stage.root

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
        if provider is not None and provider.capabilities.supports_download_cursor:
            checkpoint = dict((job.manifest or {}).get("provider_cursor") or {}) or None
            provider_chunk = provider.plan_download_chunk(
                checkpoint,
                batch_size=int(max_posts),
            )
        if provider_chunk is not None:
            cmd.extend(provider_chunk.gallerydl_args)
            cursor_mode = "provider"
            cursor_token = provider_chunk.token
        else:
            # ``1-N`` is intentionally retained for providers without a proven
            # stable cursor; shrinking it into positional chunks can skip posts
            # when the remote feed changes between invocations.
            cmd.extend(["--range", f"1-{max_posts}"])
            cursor_mode = "full_fallback"
            cursor_token = None

        # gallery-dl internal controls: per-request retry and abort.
        # NOTE: --timeout is NOT a valid CLI flag in older gallery-dl versions;
        # per-request HTTP timeout is set via the config file instead.
        cmd.extend(["--retries", str(gdl_retries)])
        cmd.extend(["--abort", str(gdl_abort)])

        cmd.extend([
            "--destination", str(download_destination),
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
                logger.warning("Failed to record proxy health for %s", job.source, exc_info=True)
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
                logger.warning("Failed to record proxy health for %s", job.source, exc_info=True)

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
                    staging_root=(
                        str(download_stage.root.relative_to(download_stage.download_root))
                        if download_stage is not None
                        else None
                    ),
                    range=(f"1-{max_posts}" if provider_chunk is None else None),
                    provider_cursor_mode=cursor_mode,
                    provider_cursor_token=cursor_token,
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
                # Check for pause/cancel signals from control listener.  The
                # listener sends SIGTERM promptly; this owner confirms exit and
                # escalates before any staged path can be inspected/promoted.
                if control_listener.command in ("pause", "cancel"):
                    break

                now_ts = time.time()
                time_since_start = now_ts - download_start
                if now_ts > deadline:
                    timed_out = True
                    logger.warning(
                        "gallery-dl timed out after %ds for job %s",
                        effective_dl_timeout,
                        job_id,
                    )
                    break
                time_since_progress = now_ts - last_progress_time
                if (
                    time_since_progress > effective_stall_timeout
                    and time_since_start > STALL_GRACE_PERIOD
                ):
                    stalled = True
                    stall_elapsed = int(time_since_start)
                    logger.warning(
                        "gallery-dl stalled (%ds no progress, elapsed %ds) for job %s, killing early",
                        effective_stall_timeout,
                        stall_elapsed,
                        job_id,
                    )
                    break

                time.sleep(POLL_INTERVAL)

            interrupted = control_listener.command in ("pause", "cancel")
            if proc.poll() is None:
                _stop_gallerydl_process(
                    proc,
                    initial_signal=(
                        signal.SIGTERM if interrupted else signal.SIGINT
                    ),
                    graceful_timeout=5.0,
                    kill_timeout=10.0,
                )
            elif _process_group_exists(proc.pid):
                # The gallery-dl leader exited but left a helper behind.
                _stop_gallerydl_process(proc)

            stderr_thread.join(timeout=5)
            stdout_thread.join(timeout=5)
            if stderr_thread.is_alive() or stdout_thread.is_alive():
                raise RuntimeError("gallery-dl output readers did not drain after process exit")

            stdout_text = "".join(stdout_lines)
            stderr_text = "".join(stderr_lines)
            if timed_out or stalled or interrupted:
                result = None
                if control_listener.command == "pause":
                    logger.info(
                        "Download job %s was paused after gallery-dl exited", job_id
                    )
                elif control_listener.command == "cancel":
                    logger.info(
                        "Download job %s was cancelled after gallery-dl exited", job_id
                    )
            else:
                if proc.returncode is None:
                    raise RuntimeError("gallery-dl completion was not reaped")
                result = subprocess.CompletedProcess(
                    cmd,
                    proc.returncode,
                    stdout=stdout_text,
                    stderr=stderr_text,
                )
                logger.info(
                    "gallery-dl exit=%d, stdout=%d bytes, stderr=%d bytes",
                    proc.returncode,
                    len(stdout_text),
                    len(stderr_text),
                )
                if proc.returncode != 0:
                    logger.warning(
                        "gallery-dl stderr (last 500): %s",
                        stderr_text[-500:] if stderr_text else "(none)",
                    )

        except Exception:
            logger.warning(
                "Unexpected error during download poll loop for job %s",
                job_id,
                exc_info=True,
            )
            # Do not turn a failed cleanup into promotion of a live tree.
            _stop_gallerydl_process(
                proc,
                initial_signal=signal.SIGKILL,
                graceful_timeout=0.1,
                kill_timeout=10.0,
            )
            stderr_thread.join(timeout=5)
            stdout_thread.join(timeout=5)
            raise

        # Freeze control state and stop publishing a heartbeat for a process
        # that has already been reaped.  Post-download promotion/parsing may be
        # slow on NAS storage, but it must not leave a listener armed with a
        # stale pid during that interval.
        control_listener.stop()
        heartbeat.stop()

        # ── Record gallery-dl result in one short transaction ──
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

        # Promotion, JSON parsing and file hashing/stat calls must not hold an
        # AsyncSession transaction.  They can block on NAS I/O for seconds.
        from app.services.artifact_ledger import ArtifactLedger, artifact_row
        from app.providers import registry as _provider_registry

        canonical_root = Path(settings.download_root).resolve()
        rows = []
        seen = set()
        downloaded_work_ids: set[str] = set()
        delta_paths: set[Path] | None = None
        if download_stage is not None:
            if proc is None or proc.poll() is None or _process_group_exists(proc.pid):
                raise RuntimeError(
                    "refusing staging promotion while gallery-dl is still running"
                )
            promotion = download_stage.promote()
            delta_paths = set(promotion.paths)
            metadata_paths = sorted(
                path
                for path in delta_paths
                if path.suffix.lower() == ".json" and path.is_file()
            )
            logger.info(
                "Download staging delta job=%s files=%d metadata=%d",
                job_id,
                len(delta_paths),
                len(metadata_paths),
            )
        else:
            # This compatibility scan is reachable only when staging was
            # explicitly disabled before the job began.
            scan_root = canonical_root / extractor_key_for_source(job.source)
            metadata_paths = []
            if scan_root.exists():
                metadata_paths = [
                    path
                    for path in scan_root.rglob("*.json")
                    if (
                        path.is_file()
                        and path.parent != scan_root
                        and path.stat().st_mtime >= download_start
                    )
                ]

        if metadata_paths:
            discovery_provider = _provider_registry.get(job.source)
            groups, invalid_metadata = group_metadata_by_work(
                discovery_provider,
                metadata_paths,
            )
            if invalid_metadata:
                for invalid_path in invalid_metadata:
                    logger.error("Could not extract work identity from %s", invalid_path)
                if download_stage is not None:
                    download_stage.mark_discovery_failed(invalid_metadata)
                raise DownloadStageDiscoveryError(
                    [
                        path.relative_to(canonical_root).as_posix()
                        if path.is_relative_to(canonical_root)
                        else str(path)
                        for path in invalid_metadata
                    ]
                )
            for source_work_id, items in groups.items():
                downloaded_work_ids.add(source_work_id)
                for jf, _ in items:
                    row = artifact_row(
                        jf,
                        canonical_root,
                        job_uuid,
                        source=job.source,
                        source_work_id=source_work_id,
                    )
                    if row and row["file_path"] not in seen:
                        seen.add(row["file_path"])
                        rows.append(row)
                for asset_path in media_files_for_group(
                    items,
                    source_work_id,
                    allowed_paths=delta_paths,
                ):
                    if delta_paths is not None and asset_path not in delta_paths:
                        continue
                    if delta_paths is None and asset_path.stat().st_mtime < download_start:
                        continue
                    row = artifact_row(
                        asset_path,
                        canonical_root,
                        job_uuid,
                        source=job.source,
                        source_work_id=source_work_id,
                    )
                    if row and row["file_path"] not in seen:
                        seen.add(row["file_path"])
                        rows.append(row)

        completed_full_chunk = (
            result is not None
            and result.returncode == 0
            and control_listener.command is None
        )
        next_checkpoint = None
        if completed_full_chunk and provider_chunk is not None and provider is not None:
            next_checkpoint = provider.complete_download_chunk(
                provider_chunk,
                sorted(downloaded_work_ids),
            )

        # Only ledger registration and cursor mutation occur in this bounded
        # transaction.  A failed/paused/partial path never advances the cursor.
        async with async_session() as _ledger_db:
            _registered = await ArtifactLedger(_ledger_db).upsert_many(rows)
            if (
                completed_full_chunk
                and provider_chunk is not None
                and provider is not None
            ):
                _ledger_job = await DownloadJobRepository(_ledger_db).get(job_uuid)
                if _ledger_job is not None:
                    update_manifest(
                        _ledger_job,
                        provider_cursor=next_checkpoint,
                        provider_cursor_completed_token=provider_chunk.token,
                    )
            await _ledger_db.commit()
        if download_stage is not None:
            # Ledger replay is idempotent.  Persisting this marker only after
            # commit makes a crash at either side safely retryable.
            download_stage.mark_registered()
        logger.info("Registered %d artifacts for job %s", _registered, job_id)

    except Exception as e:
        logger.error("Unexpected error in download job %s: %s", job_id, e, exc_info=True)
        if proc is not None and (
            proc.poll() is None or _process_group_exists(proc.pid)
        ):
            _stop_gallerydl_process(
                proc,
                initial_signal=signal.SIGTERM,
                graceful_timeout=5.0,
                kill_timeout=10.0,
            )
        stage_conflict = isinstance(e, DownloadStageConflict)
        stage_manifest_error = isinstance(e, DownloadStageManifestError)
        stage_discovery_error = isinstance(e, DownloadStageDiscoveryError)
        terminal_stage_error = (
            stage_conflict or stage_manifest_error or stage_discovery_error
        )
        unexpected_retry_count: int | None = None
        async with async_session() as db2:
            repo2 = DownloadJobRepository(db2)
            j = await repo2.get(job_uuid)
            if j:
                error_text = str(e)[:10000]
                if terminal_stage_error:
                    # Retrying cannot resolve a different canonical file and
                    # cannot safely guess through a corrupt recovery manifest.
                    # Keep the stage quarantined for operator resolution.
                    await repo2.update_status(j, "failed", error_text)
                    if stage_conflict:
                        append_manifest_event(
                            j,
                            "staging_conflict",
                            conflicts=e.conflicts,
                        )
                        stage_message = "Download staging conflict; canonical files were not overwritten"
                    elif stage_discovery_error:
                        append_manifest_event(
                            j,
                            "staging_discovery_failed",
                            invalid_metadata=e.invalid_paths,
                        )
                        stage_message = "Downloaded metadata could not be identified; recovery state was retained"
                    else:
                        append_manifest_event(j, "staging_manifest_error", error=error_text)
                        stage_message = "Download staging recovery manifest needs operator attention"
                    apply_download_progress(
                        j,
                        "failed",
                        f"{stage_message}: {error_text[:140]}",
                    )
                else:
                    j.retry_count += 1
                    if j.retry_count < max_retries:
                        unexpected_retry_count = j.retry_count
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
                await request_search_projection(
                    db2,
                    subscription_ids=[j.subscription_id] if j.subscription_id else (),
                )
                await db2.commit()

        if unexpected_retry_count is not None:
            retry_delay = backoff_base * (2 ** (unexpected_retry_count - 1))
            try:
                await _enqueue_download_retry(
                    job_uuid,
                    delay_seconds=retry_delay,
                    action="unexpected_error_retry",
                )
                logger.info(
                    "Enqueued unexpected-error retry %d/%d for job %s in %ds",
                    unexpected_retry_count,
                    max_retries,
                    job_id,
                    retry_delay,
                )
            except Exception:
                # The shared publisher has already made DownloadJob and
                # TaskRun consistently failed; partial-import recovery below
                # remains useful and must still run.
                logger.error(
                    "Failed to enqueue unexpected-error retry for download job %s",
                    job_id,
                    exc_info=True,
                )

        # A staging conflict stays quarantined.  Importing older ledger rows in
        # this branch could incorrectly present the conflict as a recovered job.
        if not terminal_stage_error:
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
        if proc is not None and (
            proc.poll() is None or _process_group_exists(proc.pid)
        ):
            # This is the final ownership boundary for the subprocess.  Never
            # return to RQ (which releases the source/archive lease) with a
            # gallery-dl leader or helper still writing.
            _stop_gallerydl_process(
                proc,
                initial_signal=signal.SIGTERM,
                graceful_timeout=5.0,
                kill_timeout=10.0,
            )

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

        await request_search_projection(
            db2,
            repository_ids=[j.subscription_source_id] if j.subscription_source_id else (),
            subscription_ids=[j.subscription_id] if j.subscription_id else (),
        )
        await db2.commit()

    # ── Enqueue import on success, auto-retry on failure, partial recovery on timeout ──

    if result is not None and result.returncode == 0:
        # Full success — but only enqueue import if there are new metadata JSONs.
        # gallery-dl exits 0 even when all files were skipped (already in archive),
        # or when the source has no content at all.
        async with async_session() as _manifest_db:
            _manifest_job = await DownloadJobRepository(_manifest_db).get(job_uuid)
            if _manifest_job:
                with stage_timer(_manifest_job, "scan"):
                    metadata_count, image_count, new_json_paths = await _artifact_counts(job_uuid)
                update_manifest(_manifest_job, metadata_json_count=metadata_count, image_count=image_count)
                append_manifest_event(_manifest_job, "artifacts_counted", metadata_json_count=metadata_count, image_count=image_count)
                apply_download_progress(
                    _manifest_job,
                    "post_download",
                    None,
                    current=metadata_count,
                    total=metadata_count or None,
                    percent=90,
                    assets=image_count,
                )
                await _manifest_db.commit()
            else:
                metadata_count, image_count, new_json_paths = await _artifact_counts(job_uuid)
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

            # Determine status via pure helper (manual empty -> failed; subscription
            # re-sync with no new content -> complete; see download_outcome.py).
            async with async_session() as _sub_db:
                _sub_job = await DownloadJobRepository(_sub_db).get(job_uuid)
                is_subscription = bool(_sub_job and _sub_job.subscription_source_id)
            decision = classify_no_metadata_outcome(
                image_count=image_count,
                auth_warning=auth_warning,
                is_subscription=is_subscription,
                had_sync_baseline=had_sync_baseline(_sub_job) if _sub_job else False,
            )
            error = decision.error
            if decision.status == "failed" and auth_warning:
                error = f"{error}\n{stderr_text[:2000]}"

            async with async_session() as db3:
                repo3 = DownloadJobRepository(db3)
                j3 = await repo3.get(job_uuid)
                if j3:
                    outcome = (
                        build_sync_outcome(
                            decision.outcome_code,
                            metadata_count=metadata_count,
                            media_count=image_count,
                        )
                        if decision.outcome_code
                        else None
                    )
                    await finalize_download_job(
                        db3,
                        j3,
                        status=decision.status,
                        outcome=outcome,
                        error=error,
                        message=error,
                        assets=image_count,
                    )

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
            retry_delay = backoff_base * (2 ** (j.retry_count - 1))
            try:
                await _enqueue_download_retry(
                    job_uuid,
                    delay_seconds=retry_delay,
                    action="auto_retry",
                )
                logger.info("Enqueued retry %d/%d for job %s in %ds",
                           j.retry_count, max_retries, job_id,
                           retry_delay)
            except Exception:
                logger.error("Failed to enqueue retry for download job %s", job_id, exc_info=True)
                # publish_prepared_download already compensates DownloadJob
                # and TaskRun in one database transaction.
                raise
