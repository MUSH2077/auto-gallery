from uuid import UUID

from sqlalchemy import select, func

from app.models import Tag, WorkTag
from app.repositories.base import BaseRepository


class TagRepository(BaseRepository[Tag]):
    model = Tag

    async def list_all(self, offset: int = 0, limit: int = 100,
                       sort_by: str = "usage_count",
                       sort_order: str = "desc") -> list[Tag]:
        count_sub = (
            select(func.count(WorkTag.work_id))
            .where(WorkTag.tag_id == Tag.id)
            .correlate(Tag)
            .scalar_subquery()
        )

        sort_col = Tag.normalized_name if sort_by == "name" else count_sub
        sort_fn = sort_col.desc() if sort_order == "desc" else sort_col.asc()

        result = await self.session.execute(
            select(Tag, count_sub.label("usage_count"))
            .offset(offset).limit(limit)
            .order_by(sort_fn, Tag.normalized_name)
        )
        tags = []
        for row in result:
            tag = row[0]
            tag.usage_count = row[1] or 0
            tags.append(tag)
        return tags

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
