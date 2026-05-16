from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import api_router
from app.api.media import router as media_router
from app.config import settings
from app.database import async_session, engine

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("backend starting", log_level=settings.log_level)
    yield
    await engine.dispose()
    logger.info("backend stopped")


app = FastAPI(title="auto-gallery", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3250", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

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
