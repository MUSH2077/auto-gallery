from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.tag import TagRead, TagCreate, TagUpdate
from app.repositories.tag import TagRepository

router = APIRouter()


@router.get("", response_model=list[TagRead])
async def list_tags(offset: int = 0, limit: int = 100, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    return await repo.list_all(offset, limit)


@router.get("/{tag_id}", response_model=TagRead)
async def get_tag(tag_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.get(tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    return tag


@router.post("", response_model=TagRead)
async def create_tag(data: TagCreate, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.create(data.model_dump())
    await db.commit()
    return tag


@router.put("/{tag_id}", response_model=TagRead)
async def update_tag(tag_id: UUID, data: TagUpdate, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.get(tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    tag = await repo.update(tag, data.model_dump(exclude_unset=True))
    await db.commit()
    return tag


@router.delete("/{tag_id}")
async def delete_tag(tag_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.get(tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    await repo.delete(tag)
    await db.commit()
    return {"status": "ok"}
