from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Creator, SourceCreator, CreatorLink


class CreatorRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self, offset: int = 0, limit: int = 50, is_favorite: bool | None = None) -> list[Creator]:
        stmt = select(Creator).offset(offset).limit(limit).order_by(Creator.name)
        if is_favorite is not None:
            stmt = stmt.where(Creator.is_favorite == is_favorite)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get(self, creator_id: UUID) -> Creator | None:
        return await self.session.get(Creator, creator_id)

    async def create(self, data: dict) -> Creator:
        creator = Creator(**data)
        self.session.add(creator)
        await self.session.flush()
        return creator

    async def update(self, creator: Creator, data: dict) -> Creator:
        for key, value in data.items():
            if value is not None:
                setattr(creator, key, value)
        await self.session.flush()
        return creator

    async def delete(self, creator: Creator) -> None:
        await self.session.delete(creator)
        await self.session.flush()

    async def add_source_creator(self, data: dict) -> SourceCreator:
        sc = SourceCreator(**data)
        self.session.add(sc)
        await self.session.flush()
        return sc

    async def get_source_creator(self, sc_id: UUID) -> SourceCreator | None:
        return await self.session.get(SourceCreator, sc_id)

    async def add_link(self, data: dict) -> CreatorLink:
        link = CreatorLink(**data)
        self.session.add(link)
        await self.session.flush()
        return link

    async def get_link(self, link_id: UUID) -> CreatorLink | None:
        return await self.session.get(CreatorLink, link_id)

    async def update_link(self, link: CreatorLink, data: dict) -> CreatorLink:
        for key, value in data.items():
            if value is not None:
                setattr(link, key, value)
        await self.session.flush()
        return link

    async def list_links(self, creator_id: UUID) -> list[CreatorLink]:
        result = await self.session.execute(
            select(CreatorLink)
            .where(CreatorLink.creator_id == creator_id)
            .order_by(CreatorLink.confidence.desc())
        )
        return list(result.scalars().all())

    async def list_source_creators(self, creator_id: UUID) -> list[SourceCreator]:
        from sqlalchemy import select
        result = await self.session.execute(
            select(SourceCreator).where(SourceCreator.creator_id == creator_id)
        )
        return list(result.scalars().all())

    async def delete_link(self, link: CreatorLink) -> None:
        await self.session.delete(link)
        await self.session.flush()
