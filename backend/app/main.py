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

    all_up = all(v == "up" for v in services.values())
    return {
        "status": "ok" if all_up else "degraded",
        "version": "0.1.0",
        "services": services,
    }
