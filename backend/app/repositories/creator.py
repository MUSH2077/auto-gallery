from uuid import UUID

from sqlalchemy import select

from app.models import Creator, SourceCreator, CreatorLink
from app.repositories.base import BaseRepository


class CreatorRepository(BaseRepository[Creator]):
    model = Creator

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
        result = await self.session.execute(
            select(SourceCreator).where(SourceCreator.creator_id == creator_id)
        )
        return list(result.scalars().all())

    async def delete_link(self, link: CreatorLink) -> None:
        await self.session.delete(link)
        await self.session.flush()
