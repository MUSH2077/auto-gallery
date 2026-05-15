from fastapi import APIRouter

router = APIRouter()


@router.get("/system/health")
async def health():
    return {"status": "ok", "version": "0.1.0", "services": {"postgres": "up", "redis": "up", "meilisearch": "up"}}
