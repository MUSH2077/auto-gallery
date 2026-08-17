from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tag, WorkSource, WorkSourceTag, WorkTag
from app.repositories.base import BaseRepository


async def source_usage_by_tag(
    session: AsyncSession,
    tag_ids: list[UUID],
    *filters,
) -> dict[UUID, list[dict[str, int | str]]]:
    """Return source composition for many tags with one grouped query."""
    if not tag_ids:
        return {}

    result = await session.execute(
        select(
            WorkSourceTag.tag_id,
            WorkSource.source,
            func.count(func.distinct(WorkSource.work_id)).label("work_count"),
        )
        .join(WorkSource, WorkSource.id == WorkSourceTag.work_source_id)
        .where(WorkSourceTag.tag_id.in_(tag_ids), *filters)
        .group_by(WorkSourceTag.tag_id, WorkSource.source)
        .order_by(
            WorkSourceTag.tag_id,
            func.count(func.distinct(WorkSource.work_id)).desc(),
            WorkSource.source,
        )
    )
    usage: dict[UUID, list[dict[str, int | str]]] = {}
    for tag_id, source, work_count in result.all():
        usage.setdefault(tag_id, []).append({
            "source": str(source),
            "work_count": int(work_count or 0),
        })
    return usage


class TagRepository(BaseRepository[Tag]):
    model = Tag

    async def list_all(self, offset: int = 0, limit: int | None = 100,
                       sort_by: str = "usage_count",
                       sort_order: str = "desc") -> list[Tag]:
        if limit is None:
            counts = (
                select(
                    WorkTag.tag_id.label("tag_id"),
                    func.count(WorkTag.work_id).label("usage_count"),
                )
                .group_by(WorkTag.tag_id)
                .subquery()
            )
            usage_count = func.coalesce(counts.c.usage_count, 0)
            sort_col = Tag.normalized_name if sort_by == "name" else usage_count
            sort_fn = sort_col.desc() if sort_order == "desc" else sort_col.asc()
            result = await self.session.execute(
                select(Tag, usage_count.label("usage_count"))
                .outerjoin(counts, counts.c.tag_id == Tag.id)
                .order_by(sort_fn, Tag.normalized_name)
            )
            tags = []
            for row in result:
                tag = row[0]
                tag.usage_count = row[1] or 0
                tags.append(tag)
            source_usage = await source_usage_by_tag(
                self.session,
                [tag.id for tag in tags],
            )
            for tag in tags:
                tag.source_usage = source_usage.get(tag.id, [])
            return tags

        count_sub = (
            select(func.count(WorkTag.work_id))
            .where(WorkTag.tag_id == Tag.id)
            .correlate(Tag)
            .scalar_subquery()
        )

        sort_col = Tag.normalized_name if sort_by == "name" else count_sub
        sort_fn = sort_col.desc() if sort_order == "desc" else sort_col.asc()

        statement = (
            select(Tag, count_sub.label("usage_count"))
            .offset(offset)
            .order_by(sort_fn, Tag.normalized_name)
        )
        if limit is not None:
            statement = statement.limit(limit)
        result = await self.session.execute(statement)
        tags = []
        for row in result:
            tag = row[0]
            tag.usage_count = row[1] or 0
            tags.append(tag)
        source_usage = await source_usage_by_tag(
            self.session,
            [tag.id for tag in tags],
        )
        for tag in tags:
            tag.source_usage = source_usage.get(tag.id, [])
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
