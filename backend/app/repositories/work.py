from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Work, WorkSource, Asset


class WorkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self, offset: int = 0, limit: int = 50) -> list[Work]:
        from sqlalchemy import func
        from app.models.asset_source import AssetSource
        from app.models.work_source import WorkSource
        result = await self.session.execute(
            select(Work, func.count(AssetSource.id).label("asset_count"))
            .outerjoin(WorkSource, WorkSource.work_id == Work.id)
            .outerjoin(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .group_by(Work.id)
            .offset(offset).limit(limit).order_by(Work.created_at.desc())
        )
        works = []
        for row in result:
            w = row[0]
            w.asset_count = row[1]
            works.append(w)
        return works

    async def get(self, work_id: UUID) -> Work | None:
        return await self.session.get(Work, work_id)

    async def get_sources(self, work_id: UUID) -> list[WorkSource]:
        result = await self.session.execute(
            select(WorkSource).where(WorkSource.work_id == work_id)
        )
        return list(result.scalars().all())

    async def get_assets(self, work_source_id: UUID) -> list[Asset]:
        from app.models import AssetSource
        result = await self.session.execute(
            select(Asset)
            .join(AssetSource, AssetSource.asset_id == Asset.id)
            .where(AssetSource.work_source_id == work_source_id)
        )
        return list(result.scalars().all())
