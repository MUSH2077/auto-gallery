import logging
import os
import secrets
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import api_router
from app.api.media import router as media_router
from app.config import settings
from app.database import async_session, engine

# Configure stdlib logging from settings
# File-based log persistence with rotation
import logging.handlers, os as _os
_log_dir = _os.path.join(settings.app_config_root, "logs")
try:
    _os.makedirs(_log_dir, exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(
        _os.path.join(_log_dir, "backend.log"), maxBytes=10 * 1024 * 1024, backupCount=5
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(_file_handler)
except OSError:
    pass

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

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
    # Auto-generate and persist SECRET_KEY if still default
    if settings.secret_key == "changeme":
        _key_file = os.path.join(settings.app_config_root, "secret_key")
        if os.path.exists(_key_file):
            with open(_key_file) as _f:
                settings.secret_key = _f.read().strip()
            logger.info("Loaded persisted SECRET_KEY from %s", _key_file)
        else:
            settings.secret_key = secrets.token_hex(32)
            os.makedirs(settings.app_config_root, exist_ok=True)
            with open(_key_file, "w") as _f:
                _f.write(settings.secret_key)
            logger.info("Generated new SECRET_KEY and persisted to %s", _key_file)

    logger.info("backend starting", log_level=settings.log_level)
    from app.auth import ensure_admin_user
    try:
        await ensure_admin_user()
    except Exception as e:
        logger.warning("ensure_admin_user failed (may be first run before migration)", error=str(e))
    yield
    await engine.dispose()
    logger.info("backend stopped")


app = FastAPI(title="auto-gallery", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

from fastapi.exceptions import RequestValidationError
from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    safe_body = "<redacted>"
    try:
        raw = await request.body()
        import json as _json
        data = _json.loads(raw[:2000])
        for key in ("password", "token", "secret", "api_key", "cookie_content", "refresh_token", "cookies", "cookies_path"):
            if key in data:
                data[key] = "***REDACTED***"
        safe_body = str(data)
    except Exception:
        safe_body = f"<{len(raw)} bytes, parse error>"
    logger.error("Validation error on %s %s: %s | body: %s",
                 request.method, request.url.path, exc.errors(), safe_body)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

app.include_router(api_router)
app.include_router(media_router)


@app.get("/api/v1/system/health")
async def health():
    services = {"postgres": "unknown", "redis": "unknown", "meilisearch": "unknown"}

    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        services["postgres"] = "up"
    except Exception:
        services["postgres"] = "down"

    try:
        import redis as redis_lib
        r = redis_lib.from_url(settings.redis_url)
        r.ping()
        r.close()
        services["redis"] = "up"
    except Exception:
        services["redis"] = "down"

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.meili_url}/health",
                headers={"Authorization": f"Bearer {settings.meili_master_key}"},
                timeout=5,
            )
            services["meilisearch"] = "up" if resp.status_code == 200 else "down"
    except Exception:
        services["meilisearch"] = "down"

    # Disk space check
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
        import redis as redis_lib
        from rq import Queue
        from rq.registry import ScheduledJobRegistry

        r = redis_lib.from_url(settings.redis_url)
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
        r.close()
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
