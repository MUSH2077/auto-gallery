from fastapi import APIRouter

router = APIRouter()


@router.get("")
async def search(q: str = "", offset: int = 0, limit: int = 20):
    return {"results": [], "total": 0, "query": q}
