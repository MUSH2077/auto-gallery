from uuid import UUID

from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Work, WorkSource, Asset, AssetSource, SourceCreator, Tag, WorkTag, WorkCurationState


class WorkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self, offset: int = 0, limit: int = 50,
                       search: str | None = None,
                       source: str | None = None,
                       creator_id: str | None = None,
                       tag: str | None = None,
                       is_nsfw: bool | None = None,
                       is_favorite: bool | None = None,
                       is_ai_generated: bool | None = None,
                       curation_visibility: str = "visible",
                       sort_by: str = "created_at",
                       sort_order: str = "desc",
                       precomputed_total: int | None = None) -> tuple[list[Work], int]:
        # ── Pre-aggregated derived tables (run once, not per-row) ──
        ws_agg = (
            select(
                WorkSource.work_id,
                WorkSource.source,
                SourceCreator.display_name.label("creator_name"),
                SourceCreator.creator_id.label("creator_id"),
            )
            .outerjoin(SourceCreator,
                       (SourceCreator.source_creator_id == WorkSource.source_creator_id)
                       & (SourceCreator.source == WorkSource.source))
            .distinct(WorkSource.work_id)
            .subquery()
        )

        ac_agg = (
            select(
                WorkSource.work_id,
                func.count(AssetSource.id).label("asset_count"),
            )
            .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .group_by(WorkSource.work_id)
            .subquery()
        )

        ug_agg = (
            select(
                WorkSource.work_id,
                (func.count(AssetSource.id) > 0).label("has_ugoira"),
            )
            .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .join(Asset, Asset.id == AssetSource.asset_id)
            .where(
                Asset.file_name.ilike("%.gif") | Asset.file_name.ilike("%.zip"),
            )
            .group_by(WorkSource.work_id)
            .subquery()
        )

        stmt = (
            select(
                Work,
                func.coalesce(ac_agg.c.asset_count, 0).label("asset_count"),
                ws_agg.c.source.label("source"),
                ws_agg.c.creator_name.label("creator_name"),
                ws_agg.c.creator_id.label("creator_id"),
                func.coalesce(ug_agg.c.has_ugoira, False).label("has_ugoira"),
            )
            .outerjoin(ws_agg, ws_agg.c.work_id == Work.id)
            .outerjoin(ac_agg, ac_agg.c.work_id == Work.id)
            .outerjoin(ug_agg, ug_agg.c.work_id == Work.id)
            .outerjoin(WorkCurationState, WorkCurationState.work_id == Work.id)
        )

        # Build WHERE conditions
        conditions = []
        if search:
            conditions.append(Work.title.ilike(f"%{search}%"))
        if is_nsfw is not None:
            conditions.append(Work.is_nsfw == is_nsfw)
        if is_favorite is not None:
            conditions.append(Work.is_favorite == is_favorite)
        if is_ai_generated is not None:
            conditions.append(Work.is_ai_generated == is_ai_generated)
        if curation_visibility and curation_visibility != "all":
            if curation_visibility == "visible":
                conditions.append(or_(WorkCurationState.visibility.is_(None), WorkCurationState.visibility == "visible"))
            else:
                conditions.append(WorkCurationState.visibility == curation_visibility)

        # For source and creator filters, need EXISTS subquery
        if source:
            conditions.append(
                Work.id.in_(
                    select(WorkSource.work_id).where(WorkSource.source == source)
                )
            )
        if creator_id:
            conditions.append(
                Work.id.in_(
                    select(WorkSource.work_id)
                    .join(SourceCreator, SourceCreator.source_creator_id == WorkSource.source_creator_id)
                    .where(SourceCreator.creator_id == UUID(creator_id))
                )
            )
        if tag:
            conditions.append(
                Work.id.in_(
                    select(WorkTag.work_id)
                    .join(Tag, Tag.id == WorkTag.tag_id)
                    .where(Tag.normalized_name == tag)
                )
            )

        if conditions:
            stmt = stmt.where(and_(*conditions))

        # Count query (same filters, no limit/offset). COUNT is a full scan of
        # the filtered set — O(rows) — so callers can pass a cached total
        # (keyed by filters, not by page) to skip it entirely on deep paging.
        if precomputed_total is not None:
            total = precomputed_total
        else:
            count_stmt = select(func.count(Work.id)).outerjoin(WorkCurationState, WorkCurationState.work_id == Work.id)
            if conditions:
                count_stmt = count_stmt.where(and_(*conditions))
            count_result = await self.session.execute(count_stmt)
            total = count_result.scalar_one()

        # Sort
        sort_col = getattr(Work, sort_by, Work.created_at)
        stmt = stmt.order_by(sort_col.desc() if sort_order == "desc" else sort_col.asc())

        stmt = stmt.offset(offset).limit(limit)

        result = await self.session.execute(stmt)
        works = []
        work_ids = []
        for row in result:
            w = row[0]
            w.asset_count = row[1] or 0
            w.source = row[2]
            w.creator_name = row[3]
            w.creator_id = str(row[4]) if row[4] else None
            w.has_ugoira = bool(row[5])
            w.preview_asset_ids = []
            w.curation_visibility = curation_visibility if curation_visibility != "all" else "visible"
            works.append(w)
            work_ids.append(w.id)

        # Fetch preview asset IDs (first 10 per work) for multi-page preview
        if work_ids:
            asset_stmt = (
                select(WorkSource.work_id, Asset.id)
                .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
                .join(Asset, Asset.id == AssetSource.asset_id)
                .where(WorkSource.work_id.in_(work_ids))
                .order_by(WorkSource.work_id, Asset.file_name)
            )
            asset_result = await self.session.execute(asset_stmt)
            asset_map: dict = {}
            for wid, aid in asset_result:
                asset_map.setdefault(wid, []).append(str(aid))
            for w in works:
                w.preview_asset_ids = asset_map.get(w.id, [])[:10]

        return works, total

    async def toggle_favorite(self, work: Work) -> Work:
        work.is_favorite = not work.is_favorite
        await self.session.commit()
        await self.session.refresh(work)
        return work

    async def get(self, work_id: UUID) -> Work | None:
        work = await self.session.get(Work, work_id)
        if work is None:
            return None
        from app.models.creator import Creator
        row = await self.session.execute(
            select(Creator.id, Creator.display_name, Creator.name)
            .join(SourceCreator, SourceCreator.creator_id == Creator.id)
            .join(WorkSource, WorkSource.source_creator_id == SourceCreator.source_creator_id)
            .where(WorkSource.work_id == work_id)
            .limit(1)
        )
        result = row.one_or_none()
        if result:
            work.creator_id = result[0]
            work.creator_name = result[1] or result[2]
        return work

    async def get_sources(self, work_id: UUID) -> list[WorkSource]:
        result = await self.session.execute(
            select(WorkSource).where(WorkSource.work_id == work_id)
        )
        return list(result.scalars().all())

    async def get_assets(self, work_source_id: UUID) -> list[Asset]:
        result = await self.session.execute(
            select(Asset)
            .join(AssetSource, AssetSource.asset_id == Asset.id)
            .where(AssetSource.work_source_id == work_source_id)
        )
        return list(result.scalars().all())
    async def get_creator_timeline(self, creator_id: UUID,
                                    from_date: str | None = None,
                                    to_date: str | None = None) -> tuple[list[dict], list[str]]:
        """Return per-source work counts and work IDs per day for a creator timeline grid."""
        cols = [
            func.date(Work.posted_at).label("date"),
            WorkSource.source,
            func.count(Work.id).label("cnt"),
            func.array_agg(func.distinct(Work.id)).label("work_ids"),
        ]
        stmt = (
            select(*cols)
            .select_from(Work)
            .join(WorkSource, WorkSource.work_id == Work.id)
            .join(SourceCreator,
                  (SourceCreator.source_creator_id == WorkSource.source_creator_id)
                  & (SourceCreator.source == WorkSource.source))
            .where(SourceCreator.creator_id == creator_id)
            .where(Work.posted_at.isnot(None))
        )
        if from_date:
            stmt = stmt.where(Work.posted_at >= from_date)
        if to_date:
            stmt = stmt.where(Work.posted_at < to_date)
        stmt = stmt.group_by(func.date(Work.posted_at), WorkSource.source).order_by("date")
        result = await self.session.execute(stmt)
        rows = result.all()
        days: dict[str, dict] = {}
        sources: set[str] = set()
        for date_val, source, cnt, work_ids in rows:
            day = str(date_val)
            if day not in days:
                days[day] = {"date": day, "total": 0}
            days[day][source] = cnt
            days[day][f"{source}_ids"] = [str(wid) for wid in (work_ids or [])]
            days[day]["total"] += cnt
            sources.add(source)
        return list(days.values()), sorted(sources)
