from uuid import UUID

from sqlalchemy import any_, bindparam, select, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
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

    tag_id_array = bindparam(
        "source_usage_tag_ids",
        value=tag_ids,
        type_=ARRAY(PG_UUID(as_uuid=True)),
    )

    result = await session.execute(
        select(
            WorkSourceTag.tag_id,
            WorkSource.source,
            func.count(func.distinct(WorkSource.work_id)).label("work_count"),
        )
        .join(WorkSource, WorkSource.id == WorkSourceTag.work_source_id)
        .where(WorkSourceTag.tag_id == any_(tag_id_array), *filters)
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

    async def page(self, *, offset=0, limit=100, q=None, category=None, sort_by="usage_count", sort_order="desc"):
        filters = []
        if q:
            filters.append(Tag.normalized_name.icontains(q.strip(), autoescape=True))
        if category is not None:
            filters.append(Tag.category == category)
        total = int((await self.session.execute(select(func.count()).select_from(Tag).where(*filters))).scalar_one())
        if sort_by == "usage_count":
            counts = select(WorkTag.tag_id, func.count().label("usage_count")).group_by(WorkTag.tag_id).subquery()
            usage = func.coalesce(counts.c.usage_count, 0)
            statement = select(Tag, usage).outerjoin(counts, counts.c.tag_id == Tag.id).where(*filters)
            statement = statement.order_by(usage.desc() if sort_order == "desc" else usage.asc(), Tag.normalized_name, Tag.id)
            rows = (await self.session.execute(statement.offset(offset).limit(limit))).all()
            tags = [row[0] for row in rows]
            counts_by_id = {row[0].id: int(row[1]) for row in rows}
        else:
            order = Tag.normalized_name.desc() if sort_order == "desc" else Tag.normalized_name.asc()
            tags = list((await self.session.execute(select(Tag).where(*filters).order_by(order, Tag.id).offset(offset).limit(limit))).scalars())
            counts_by_id = dict((await self.session.execute(select(WorkTag.tag_id, func.count()).where(
                WorkTag.tag_id.in_([tag.id for tag in tags])).group_by(WorkTag.tag_id))).all()) if tags else {}
        composition = await source_usage_by_tag(self.session, [tag.id for tag in tags])
        for tag in tags:
            tag.usage_count = counts_by_id.get(tag.id, 0)
            tag.source_usage = composition.get(tag.id, [])
        return {"items": tags, "total": total, "offset": offset, "limit": limit,
                "next_offset": offset + len(tags) if offset + len(tags) < total else None}

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
