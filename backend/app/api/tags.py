from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.tag import TagRead
from app.repositories.tag import TagRepository

router = APIRouter()


@router.get("", response_model=list[TagRead])
async def list_tags(offset: int = 0, limit: int = 100, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    return await repo.list_all(offset, limit)
