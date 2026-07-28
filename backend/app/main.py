import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import api_router
from app.api.media import router as media_router
from app.config import settings
from app.database import async_session, engine
from app.services.danbooru import DanbooruUnavailableError

# Configure stdlib logging from settings.
# NOTE: do NOT use logging.basicConfig() here — it is a no-op once the root
# logger already has a handler, which silently left the root level at WARNING
# (dropping every INFO log) and added no stdout handler (so `docker compose
# logs backend` showed nothing from the app). We configure the root logger
# explicitly instead.
import logging.handlers, os as _os, sys as _sys

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
_root_logger = logging.getLogger()
_root_logger.setLevel(_log_level)

# 1. stdout — this is what `docker compose logs backend` captures. Primary
#    place to read/copy logs.
_stream_handler = logging.StreamHandler(_sys.stdout)
_stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_root_logger.addHandler(_stream_handler)

# 2. rotating file for persistence across restarts (under APP_CONFIG_ROOT/logs).
try:
    _log_dir = _os.path.join(settings.app_config_root, "logs")
    _os.makedirs(_log_dir, exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(
        _os.path.join(_log_dir, "backend.log"), maxBytes=10 * 1024 * 1024, backupCount=5
    )
    _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    _root_logger.addHandler(_file_handler)
except OSError:
    pass

# 3. Route uvicorn's own loggers through the root handlers so access/error
#    lines land in the same stream + file at the same level.
for _uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _uv_logger = logging.getLogger(_uv_name)
    _uv_logger.handlers = []
    _uv_logger.propagate = True

# Install in-memory ring buffer for log API
from app.services.log_buffer import install as install_log_buffer
install_log_buffer()

# Bridge structlog -> stdlib so all loggers output consistently
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Critical service secrets are validated at config load time; the admin
    # bootstrap password may use the documented first-login default and is
    # always forced through password rotation.
    logger.info("backend starting", log_level=settings.log_level)
    from app.auth import ensure_admin_user
    from app.services.settings import ensure_gallerydl_config
    try:
        ensure_gallerydl_config()
    except Exception as e:
        logger.warning("ensure_gallerydl_config failed", error=str(e))
    try:
        await ensure_admin_user()
    except Exception as e:
        logger.warning("ensure_admin_user failed (may be first run before migration)", error=str(e))

    # Operation state is intentionally retained across API restarts. Workers
    # own terminal transitions and long-running rebuilds resume from checkpoints.

    # ── Start WebSocket Redis listener ──
    from app.services.ws_manager import manager as ws_manager
    ws_task = asyncio.create_task(ws_manager.start_redis_listener())
    logger.info("WebSocket Redis listener started")

    # ── Start stale detection background task ──
    async def stale_detection_loop():
        while True:
            await asyncio.sleep(30)
            try:
                async with async_session() as db:
                    from app.services.task_engine import TaskEngine
                    engine = TaskEngine(db)
                    count = await engine.detect_stale_tasks()
                    if count:
                        logger.info("Stale detection: marked %d tasks as stale", count)
            except Exception:
                logger.warning("Stale detection cycle failed", exc_info=True)

    stale_task = asyncio.create_task(stale_detection_loop())
    logger.info("Stale detection background task started")

    async def import_recovery_loop():
        while True:
            await asyncio.sleep(120)
            try:
                async with async_session() as db:
                    from app.services.import_recovery import recover_import_pipeline
                    from app.services.redis_client import get_redis
                    result = await recover_import_pipeline(db, redis_client=get_redis())
                    if result.get("imports_enqueued") or result.get("imports_marked_stale"):
                        logger.info("Import recovery repaired pipeline gap", **result)
            except Exception:
                logger.warning("Import recovery cycle failed", exc_info=True)

    import_recovery_task = asyncio.create_task(import_recovery_loop())
    logger.info("Import recovery background task started")

    async def asset_dedup_outbox_loop():
        # PostgreSQL is authoritative for pending storage effects. This small
        # recovery loop closes the gap when an RQ enqueue is lost during an API
        # or worker restart; file hashing itself runs off the event loop.
        while True:
            await asyncio.sleep(60)
            try:
                from app.jobs.asset_dedup import process_asset_dedup_outbox

                result = await process_asset_dedup_outbox(limit=20)
                if result["processed"] or result["failed"]:
                    logger.info("Asset dedup outbox drained", **result)
            except Exception:
                logger.warning("Asset dedup outbox recovery failed", exc_info=True)

    asset_dedup_task = asyncio.create_task(asset_dedup_outbox_loop())
    logger.info("Asset dedup outbox recovery started")

    # ── Memory monitor — logs RSS so an OOM leaves a visible climb in the
    #    logs + the last thing that was happening. WARNs past a threshold. ──
    async def memory_monitor_loop():
        warned = False
        while True:
            await asyncio.sleep(settings.memory_log_interval_seconds)
            try:
                rss = _process_rss_mb()
                if rss is None:
                    continue
                if rss >= settings.memory_warn_mb:
                    logger.warning(
                        "High backend memory: RSS=%.0fMB (warn threshold=%dMB). "
                        "GET /api/v1/admin/memory for a top-object census.",
                        rss, settings.memory_warn_mb)
                    warned = True
                else:
                    if warned:
                        logger.info("Backend memory back to normal: RSS=%.0fMB", rss)
                        warned = False
                    logger.info("Backend memory: RSS=%.0fMB", rss)
            except Exception:
                logger.warning("Memory monitor cycle failed", exc_info=True)

    memory_task = asyncio.create_task(memory_monitor_loop())
    logger.info("Memory monitor background task started (warn>=%dMB, every %ds)",
                settings.memory_warn_mb, settings.memory_log_interval_seconds)

    yield

    # ── Cleanup ──
    ws_task.cancel()
    stale_task.cancel()
    import_recovery_task.cancel()
    asset_dedup_task.cancel()
    memory_task.cancel()
    try:
        await ws_task
    except asyncio.CancelledError:
        pass
    try:
        await stale_task
    except asyncio.CancelledError:
        pass
    try:
        await import_recovery_task
    except asyncio.CancelledError:
        pass
    try:
        await asset_dedup_task
    except asyncio.CancelledError:
        pass
    try:
        await memory_task
    except asyncio.CancelledError:
        pass
    await engine.dispose()
    logger.info("backend stopped")


def _process_rss_mb() -> float | None:
    """Current resident set size of this process in MB, or None if unavailable.
    Reads /proc/self/status (Linux/containers); this is what the OOM killer
    weighs against the container memory limit."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # kB -> MB
    except Exception:
        pass
    try:
        import resource
        # ru_maxrss is peak, in kB on Linux — fallback only.
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return None


app = FastAPI(title="auto-gallery", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    safe_body = "<redacted>"
    raw = b""
    try:
        raw = await request.body()
        import json as _json
        data = _json.loads(raw[:2000])
        # Redact any key whose name contains a sensitive substring
        _sensitive_substrings = (
            "password", "token", "secret", "key", "credential", "cookie",
        )
        for key in list(data.keys()):
            if any(ss in key.lower() for ss in _sensitive_substrings):
                data[key] = "***REDACTED***"
        safe_body = str(data)
    except Exception:
        safe_body = f"<{len(raw)} bytes, parse error>"
    logger.error("Validation error on %s %s: %s | body: %s",
                 request.method, request.url.path, exc.errors(), safe_body)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(DanbooruUnavailableError)
async def danbooru_unavailable_handler(request: Request, exc: DanbooruUnavailableError):
    logger.warning("Danbooru unavailable during %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "Danbooru API is unreachable; try again later.",
                 "error_code": "danbooru_unavailable"},
    )

app.include_router(api_router)
app.include_router(media_router)


async def _service_readiness() -> dict[str, str]:
    services = {"postgres": "unknown", "redis": "unknown", "meilisearch": "unknown"}

    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        services["postgres"] = "up"
    except Exception:
        services["postgres"] = "down"

    try:
        from app.services.redis_client import get_redis
        get_redis().ping()
        services["redis"] = "up"
    except Exception:
        services["redis"] = "down"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.meili_url.rstrip('/')}/health",
                headers={"Authorization": f"Bearer {settings.meili_master_key}"},
                timeout=2,
            )
        services["meilisearch"] = "up" if resp.status_code == 200 else "down"
    except Exception:
        services["meilisearch"] = "down"

    return services


_readiness_cache: dict | None = None
_readiness_cache_ts: float = 0.0
_READINESS_CACHE_TTL = 15.0


@app.get("/api/v1/system/ready")
async def ready():
    global _readiness_cache, _readiness_cache_ts
    now_mono = time.monotonic()
    if _readiness_cache is not None and (now_mono - _readiness_cache_ts) < _READINESS_CACHE_TTL:
        services = _readiness_cache
    else:
        services = await _service_readiness()
        _readiness_cache = services
        _readiness_cache_ts = now_mono
    all_up = all(v == "up" for v in services.values())
    return JSONResponse(
        status_code=200 if all_up else 503,
        content={"status": "ok" if all_up else "degraded", "services": services},
    )


@app.get("/api/v1/system/health")
async def health():
    services = await _service_readiness()

    # Disk space check (cached)
    disk = "unknown"
    try:
        import shutil
        for path, label in [(settings.download_root, "downloads"), (settings.library_root, "library")]:
            usage = shutil.disk_usage(path)
            pct = usage.free / usage.total
            if pct < 0.05:
                disk = "critical"
                break
            elif pct < 0.10:
                disk = "warning"
        if disk == "unknown":
            disk = "ok"
    except Exception:
        disk = "unknown"

    all_up = all(v == "up" for v in services.values())
    business = {
        "queues": {},
        "jobs": {},
        "scheduler": {},
        "gallerydl": {},
    }
    try:
        from app.services.redis_client import get_redis
        from rq import Queue
        from rq.registry import ScheduledJobRegistry

        r = get_redis()
        default_q = Queue(connection=r)
        scheduled_q = Queue(name="scheduled", connection=r)
        scheduled_reg = ScheduledJobRegistry(queue=scheduled_q)
        business["queues"] = {
            "default": len(default_q),
            "scheduled": len(scheduled_q),
            "scheduled_registry": scheduled_reg.count,
        }
        sync_jobs = [
            scheduled_q.fetch_job(job_id)
            for job_id in scheduled_reg.get_job_ids()
        ]
        business["scheduler"] = {
            "sync_scheduled": str(any(j and "sync_subscriptions" in (j.func_name or "") for j in sync_jobs)).lower(),
        }
    except Exception:
        logger.debug("Business queue health skipped", exc_info=True)

    try:
        from sqlalchemy import func, select
        from app.models import DownloadJob, ImportJob, SubscriptionSource

        async with async_session() as session:
            download_counts = await session.execute(
                select(DownloadJob.status, func.count(DownloadJob.id)).group_by(DownloadJob.status)
            )
            import_counts = await session.execute(
                select(ImportJob.status, func.count(ImportJob.id)).group_by(ImportJob.status)
            )
            auth_unhealthy = await session.execute(
                select(func.count(SubscriptionSource.id)).where(SubscriptionSource.auth_healthy == False)
            )
        business["jobs"] = {
            "downloads": {status: count for status, count in download_counts.all()},
            "imports": {status: count for status, count in import_counts.all()},
            "auth_unhealthy_sources": auth_unhealthy.scalar() or 0,
        }
    except Exception:
        logger.debug("Business DB health skipped", exc_info=True)

    try:
        import json
        import subprocess
        config_path = os.path.join(settings.gallerydl_config_root, "config.json")
        version = subprocess.run(["gallery-dl", "--version"], capture_output=True, text=True, timeout=5)
        config_readable = True
        if os.path.exists(config_path):
            with open(config_path) as f:
                json.load(f)
        business["gallerydl"] = {
            "version": (version.stdout or version.stderr or "").strip(),
            "config_readable": config_readable,
        }
    except Exception:
        business["gallerydl"] = {"version": None, "config_readable": False}

    return {
        "status": "ok" if all_up else "degraded",
        "version": "0.1.0",
        "services": services,
        "disk": disk,
        "business": business,
    }
