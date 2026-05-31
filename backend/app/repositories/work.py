from uuid import UUID

from sqlalchemy import select, func, and_, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Work, WorkSource, Asset, AssetSource, SourceCreator


class WorkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self, offset: int = 0, limit: int = 50,
                       search: str | None = None,
                       source: str | None = None,
                       creator_id: str | None = None,
                       is_nsfw: bool | None = None,
                       is_favorite: bool | None = None,
                       is_ai_generated: bool | None = None,
                       sort_by: str = "created_at",
                       sort_order: str = "desc") -> tuple[list[Work], int]:
        # Scalar subquery for source (first alphabetically)
        src_sub = (
            select(WorkSource.source)
            .where(WorkSource.work_id == Work.id)
            .correlate(Work)
            .limit(1)
            .scalar_subquery()
        )

        # Scalar subquery for creator name
        cname_sub = (
            select(SourceCreator.display_name)
            .select_from(WorkSource)
            .join(SourceCreator, SourceCreator.source_creator_id == WorkSource.source_creator_id)
            .where(WorkSource.work_id == Work.id)
            .correlate(Work)
            .limit(1)
            .scalar_subquery()
        )

        # Scalar subquery for creator UUID
        cid_sub = (
            select(SourceCreator.creator_id)
            .select_from(WorkSource)
            .join(SourceCreator, SourceCreator.source_creator_id == WorkSource.source_creator_id)
            .where(WorkSource.work_id == Work.id)
            .correlate(Work)
            .limit(1)
            .scalar_subquery()
        )

        # Ugoira detection: check if any asset has .gif or .zip extension
        ugoira_sub = (
            select(func.count(AssetSource.id) > 0)
            .select_from(WorkSource)
            .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .join(Asset, Asset.id == AssetSource.asset_id)
            .where(
                WorkSource.work_id == Work.id,
                Asset.file_name.ilike("%.gif") | Asset.file_name.ilike("%.zip"),
            )
            .correlate(Work)
            .scalar_subquery()
        )

        stmt = (
            select(
                Work,
                func.coalesce(
                    select(func.count(AssetSource.id))
                    .select_from(WorkSource)
                    .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
                    .where(WorkSource.work_id == Work.id)
                    .correlate(Work)
                    .scalar_subquery(),
                    0,
                ).label("asset_count"),
                src_sub.label("source"),
                cname_sub.label("creator_name"),
                cid_sub.label("creator_id"),
                func.coalesce(ugoira_sub, False).label("has_ugoira"),
            )
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

        if conditions:
            stmt = stmt.where(and_(*conditions))

        # Count query (same filters, no limit/offset)
        count_stmt = select(func.count(Work.id))
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
            works.append(w)
            work_ids.append(w.id)

        # Fetch preview asset IDs (first 10 per work) for multi-page preview
        if work_ids:
            from app.models.work_source import WorkSource as WS
            from app.models.asset_source import AssetSource as AS
            from app.models.asset import Asset as AM
            asset_stmt = (
                select(WS.work_id, AM.id)
                .join(AS, AS.work_source_id == WS.id)
                .join(AM, AM.id == AS.asset_id)
                .where(WS.work_id.in_(work_ids))
                .order_by(WS.work_id, AM.file_name)
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
        return await self.session.get(Work, work_id)

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
        """Return per-source work counts per day for a creator timeline grid."""
        cols = [
            func.date(Work.posted_at).label("date"),
            WorkSource.source,
            func.count(Work.id).label("cnt"),
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
        for date_val, source, cnt in rows:
            day = str(date_val)
            if day not in days:
                days[day] = {"date": day, "total": 0}
            days[day][source] = cnt
            days[day]["total"] += cnt
            sources.add(source)
        return list(days.values()), sorted(sources)

