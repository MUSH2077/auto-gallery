from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.creator import CreatorCreate, CreatorRead, CreatorUpdate
from app.schemas.source_creator import SourceCreatorCreate, SourceCreatorRead
from app.schemas.creator_link import CreatorLinkCreate, CreatorLinkRead, CreatorLinkUpdate
from app.services.creator import CreatorService

router = APIRouter()


@router.get("", response_model=list[CreatorRead])
async def list_creators(offset: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    return await svc.list_creators(offset, limit)


@router.get("/{creator_id}", response_model=CreatorRead)
async def get_creator(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        return await svc.get_creator(creator_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("", response_model=CreatorRead, status_code=201)
async def create_creator(data: CreatorCreate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    return await svc.create_creator(data.model_dump())


@router.patch("/{creator_id}", response_model=CreatorRead)
async def update_creator(creator_id: UUID, data: CreatorUpdate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        return await svc.update_creator(creator_id, data.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{creator_id}", status_code=204)
async def delete_creator(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        await svc.delete_creator(creator_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{creator_id}/sources", response_model=SourceCreatorRead, status_code=201)
async def add_source_creator(creator_id: UUID, data: SourceCreatorCreate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    d = data.model_dump()
    d["creator_id"] = creator_id
    return await svc.add_source_creator(d)


@router.get("/{creator_id}/links", response_model=list[CreatorLinkRead])
async def list_creator_links(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    return await svc.list_links(creator_id)


@router.post("/{creator_id}/links", response_model=CreatorLinkRead, status_code=201)
async def add_creator_link(creator_id: UUID, data: CreatorLinkCreate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    d = data.model_dump()
    d["creator_id"] = creator_id
    return await svc.add_link(d)


@router.patch("/{creator_id}/links/{link_id}", response_model=CreatorLinkRead)
async def update_creator_link(
    creator_id: UUID, link_id: UUID, data: CreatorLinkUpdate, db: AsyncSession = Depends(get_db)
):
    svc = CreatorService(db)
    try:
        return await svc.update_link(link_id, data.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.get("/{creator_id}/sources", response_model=list[SourceCreatorRead])
async def list_source_creators(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    return await svc.list_source_creators(creator_id)


@router.delete("/{creator_id}/links/{link_id}", status_code=204)
async def delete_creator_link(creator_id: UUID, link_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        await svc.delete_link(link_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
