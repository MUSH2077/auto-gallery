from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tag


class TagRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self, offset: int = 0, limit: int = 100) -> list[Tag]:
        result = await self.session.execute(
            select(Tag).offset(offset).limit(limit).order_by(Tag.normalized_name)
        )
        return list(result.scalars().all())

    async def get_or_create(self, normalized_name: str) -> Tag:
        result = await self.session.execute(
            select(Tag).where(Tag.normalized_name == normalized_name)
        )
        tag = result.scalar_one_or_none()
        if tag is None:
            tag = Tag(normalized_name=normalized_name)
            self.session.add(tag)
            await self.session.flush()
        return tag

    async def get(self, tag_id: UUID) -> Tag | None:
        return await self.session.get(Tag, tag_id)

    async def create(self, data: dict) -> Tag:
        tag = Tag(**data)
        self.session.add(tag)
        await self.session.flush()
        return tag

    async def update(self, tag: Tag, data: dict) -> Tag:
        for key, value in data.items():
            if value is not None:
                setattr(tag, key, value)
        await self.session.flush()
        return tag

    async def delete(self, tag: Tag) -> None:
        await self.session.delete(tag)
        await self.session.flush()
