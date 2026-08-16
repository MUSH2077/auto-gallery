import logging
from uuid import UUID

from sqlalchemy import and_, func, or_, select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.creator import CreatorRepository
from app.models import Creator, CreatorLink, SourceCreator, Subscription, SubscriptionSource, DownloadJob, ImportJob
from app.models.work_source import WorkSource
from app.models.work_tag import WorkTag
from app.models.tag import Tag
from app.models.asset_source import AssetSource
from app.models.storage_artifact import StorageArtifact
from app.services.sync_outcome import download_job_outcome
from app.services.search_projection_outbox import request_search_projection

logger = logging.getLogger(__name__)


class CreatorService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = CreatorRepository(db)

    async def _projection_dependents(
        self,
        creator_id: UUID,
    ) -> tuple[set[UUID], set[UUID], set[UUID]]:
        """Return work/subscription/repository projections owned by a creator.

        The work projection embeds both creator fields and repository IDs.  A
        creator mutation therefore cannot safely update only the creator
        document.  These are set-based reads and the resulting outbox writes
        are chunked by ``request_search_projection``.
        """

        subscription_ids = set((await self.db.execute(
            select(Subscription.id).where(Subscription.creator_id == creator_id)
        )).scalars().all())
        repository_ids = set((await self.db.execute(
            select(SubscriptionSource.id)
            .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
            .where(Subscription.creator_id == creator_id)
        )).scalars().all())

        work_ids = set((await self.db.execute(
            select(WorkSource.work_id)
            .join(
                SourceCreator,
                and_(
                    SourceCreator.source == WorkSource.source,
                    SourceCreator.source_creator_id == WorkSource.source_creator_id,
                ),
            )
            .where(SourceCreator.creator_id == creator_id)
            .distinct()
        )).scalars().all())

        # A repository can be associated with a work by identity or by an
        # exact normalized URL even when no SourceCreator row exists.
        repository_work_ids = (await self.db.execute(
            select(WorkSource.work_id)
            .select_from(WorkSource)
            .join(
                SubscriptionSource,
                or_(
                    and_(
                        SubscriptionSource.source == WorkSource.source,
                        SubscriptionSource.source_creator_id.is_not(None),
                        SubscriptionSource.source_creator_id == WorkSource.source_creator_id,
                    ),
                    and_(
                        SubscriptionSource.source_url.is_not(None),
                        WorkSource.source_url.is_not(None),
                        func.lower(func.rtrim(func.btrim(SubscriptionSource.source_url), "/"))
                        == func.lower(func.rtrim(func.btrim(WorkSource.source_url), "/")),
                    ),
                ),
            )
            .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
            .where(Subscription.creator_id == creator_id)
            .distinct()
        )).scalars().all()
        work_ids.update(repository_work_ids)
        return work_ids, subscription_ids, repository_ids

    async def _request_creator_projection(
        self,
        creator_id: UUID,
        *,
        deleting: bool = False,
        dependents: tuple[set[UUID], set[UUID], set[UUID]] | None = None,
    ) -> None:
        work_ids, subscription_ids, repository_ids = (
            dependents
            if dependents is not None
            else await self._projection_dependents(creator_id)
        )
        await request_search_projection(
            self.db,
            work_ids,
            creator_ids=() if deleting else [creator_id],
            deleted_creator_ids=[creator_id] if deleting else (),
            repository_ids=() if deleting else repository_ids,
            deleted_repository_ids=repository_ids if deleting else (),
            subscription_ids=() if deleting else subscription_ids,
            deleted_subscription_ids=subscription_ids if deleting else (),
        )

    async def toggle_favorite(self, creator_id: UUID):
        creator = await self.get_creator(creator_id)
        creator.is_favorite = not creator.is_favorite
        await self._request_creator_projection(creator_id)
        await self.db.commit()
        await self.db.refresh(creator)
        return creator

    async def get_creator(self, creator_id: UUID):
        creator = await self.repo.get(creator_id)
        if not creator:
            raise ValueError("Creator not found")
        return creator

    async def create_creator(self, data: dict):
        creator = await self.repo.create(data)
        # Danbooru-first: every creation path tries to establish the identity
        # mapping. Never blocks creation — misses/outages come back as status.
        from app.services.creator_enrichment import enrich_creator_from_danbooru
        await enrich_creator_from_danbooru(self.db, creator)
        await self._request_creator_projection(creator.id)
        await self.db.commit()
        return creator

    async def update_creator(self, creator_id: UUID, data: dict):
        creator = await self.get_creator(creator_id)
        creator = await self.repo.update(creator, data)
        await self._request_creator_projection(creator_id)
        await self.db.commit()
        await self.db.refresh(creator)
        return creator

    async def delete_creator(self, creator_id: UUID):
        creator = await self.get_creator(creator_id)
        dependents = await self._projection_dependents(creator_id)

        # 1. Find all subscriptions for this creator
        subs = await self.db.execute(
            select(Subscription).where(Subscription.creator_id == creator_id)
        )
        subscriptions = subs.scalars().all()

        for sub in subscriptions:
            # Delete download_jobs and their import_jobs first (FK to subscription_sources)
            djs = await self.db.execute(
                select(DownloadJob).where(DownloadJob.subscription_id == sub.id)
            )
            for dj in djs.scalars().all():
                await self.db.execute(
                    sql_delete(ImportJob).where(ImportJob.download_job_id == dj.id)
                )
                # Purge the storage ledger for this job too. Otherwise its rows
                # keep state='done' (the FK only SET NULLs on job delete), and a
                # later disk-import skips those files as already-imported — so a
                # deleted creator can never be recovered from disk.
                await self.db.execute(
                    sql_delete(StorageArtifact).where(StorageArtifact.download_job_id == dj.id)
                )
                await self.db.delete(dj)
            # Delete subscription_sources (no more FK from download_jobs)
            await self.db.execute(
                sql_delete(SubscriptionSource).where(SubscriptionSource.subscription_id == sub.id)
            )
            await self.db.delete(sub)

        # 2. Set source_creators.creator_id to NULL (nullable column)
        await self.db.execute(
            sql_delete(SourceCreator).where(SourceCreator.creator_id == creator_id)
        )

        # 3. Delete creator links
        await self.db.execute(
            sql_delete(CreatorLink).where(CreatorLink.creator_id == creator_id)
        )

        # 4. Delete the creator
        await self.db.delete(creator)
        await self._request_creator_projection(
            creator_id,
            deleting=True,
            dependents=dependents,
        )
        await self.db.commit()

    async def add_source_creator(self, data: dict):
        sc = await self.repo.add_source_creator(data)
        work_ids = set((await self.db.execute(
            select(WorkSource.work_id).where(
                WorkSource.source == sc.source,
                WorkSource.source_creator_id == sc.source_creator_id,
            )
        )).scalars().all())
        await request_search_projection(
            self.db,
            work_ids,
            creator_ids=[sc.creator_id] if sc.creator_id else (),
        )
        await self.db.commit()
        return sc

    async def list_links(self, creator_id: UUID):
        return await self.repo.list_links(creator_id)

    async def add_link(self, data: dict):
        link = await self.repo.add_link(data)
        await self.db.commit()
        return link

    async def update_link(self, link_id: UUID, data: dict):
        link = await self.repo.get_link(link_id)
        if not link:
            raise ValueError("CreatorLink not found")
        link = await self.repo.update_link(link, data)
        await self.db.commit()
        await self.db.refresh(link)
        return link

    async def list_source_creators(self, creator_id: UUID):
        return await self.repo.list_source_creators(creator_id)

    async def delete_link(self, link_id: UUID):
        link = await self.repo.get_link(link_id)
        if not link:
            raise ValueError("CreatorLink not found")
        await self.repo.delete_link(link)
        await self.db.commit()

    async def enrich_from_danbooru(self, creator_id: UUID, source_url: str | None = None,
                                    pixiv_id: str | None = None,
                                    artist_name: str | None = None) -> int:
        """Query Danbooru and create CreatorLink suggestions for this creator."""
        from app.services.creator_enrichment import fetch_and_link_danbooru_artist
        return await fetch_and_link_danbooru_artist(
            self.db, creator_id,
            source_url=source_url, pixiv_id=pixiv_id, artist_name=artist_name,
            reraise_unavailable=True,
        )

    async def get_stats(self, creator_id: UUID) -> dict:
        from sqlalchemy import func, and_
        from app.services.cache import cache_get, cache_set, cache_key, TTL

        ck = cache_key("creators:stats", creator_id=str(creator_id))
        cached = cache_get(ck)
        if cached is not None:
            return cached

        # Works per source
        ws_rows = await self.db.execute(
            select(WorkSource.source, func.count(WorkSource.id).label("cnt"))
            .join(SourceCreator, and_(
                SourceCreator.source == WorkSource.source,
                SourceCreator.source_creator_id == WorkSource.source_creator_id))
            .where(SourceCreator.creator_id == creator_id)
            .group_by(WorkSource.source)
            .order_by(func.count(WorkSource.id).desc())
        )
        source_breakdown = [{"source": row[0], "count": row[1]} for row in ws_rows]

        # Top tags
        tag_rows = await self.db.execute(
            select(Tag.normalized_name, func.count(WorkTag.work_id).label("cnt"))
            .join(WorkTag, WorkTag.tag_id == Tag.id)
            .join(WorkSource, WorkSource.work_id == WorkTag.work_id)
            .join(SourceCreator, and_(
                SourceCreator.source == WorkSource.source,
                SourceCreator.source_creator_id == WorkSource.source_creator_id))
            .where(SourceCreator.creator_id == creator_id)
            .group_by(Tag.normalized_name)
            .order_by(func.count(WorkTag.work_id).desc())
            .limit(20)
        )
        tag_distribution = [{"tag": row[0], "count": row[1]} for row in tag_rows]

        # Monthly posting frequency
        month_rows = await self.db.execute(
            select(
                func.to_char(func.date_trunc("month", WorkSource.posted_at), "YYYY-MM").label("month"),
                func.count(WorkSource.id).label("cnt"))
            .join(SourceCreator, and_(
                SourceCreator.source == WorkSource.source,
                SourceCreator.source_creator_id == WorkSource.source_creator_id))
            .where(SourceCreator.creator_id == creator_id)
            .where(WorkSource.posted_at.isnot(None))
            .group_by("month")
            .order_by("month")
        )
        monthly = [{"month": row[0], "count": row[1]} for row in month_rows]

        total_works = sum(s["count"] for s in source_breakdown)
        total_tags = len(tag_distribution)

        asset_result = await self.db.execute(
            select(func.count(AssetSource.id))
            .select_from(WorkSource)
            .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .join(SourceCreator, and_(
                SourceCreator.source == WorkSource.source,
                SourceCreator.source_creator_id == WorkSource.source_creator_id))
            .where(SourceCreator.creator_id == creator_id)
        )
        total_assets = asset_result.scalar() or 0

        data = {
            "creator_id": str(creator_id),
            "total_works": total_works,
            "total_assets": total_assets,
            "total_tags": total_tags,
            "source_breakdown": source_breakdown,
            "tag_distribution": tag_distribution,
            "monthly_frequency": monthly,
        }
        cache_set(ck, data, TTL["creators:stats"])
        return data

    async def get_subscription_overview(self, creator_id: UUID) -> dict:
        from app.models.subscription import Subscription as Sub
        from app.models.subscription_source import SubscriptionSource as SS
        from app.models.download_job import DownloadJob as DJ
        from app.providers import registry

        sub_rows = await self.db.execute(
            select(Sub)
            .where(Sub.creator_id == creator_id)
            .order_by(Sub.created_at.desc())
        )
        subscriptions = list(sub_rows.scalars().all())
        subscription_ids = [s.id for s in subscriptions]

        if not subscription_ids:
            return {
                "creator_id": str(creator_id),
                "subscriptions": [],
                "repositories": [],
                "summary": {
                    "subscription_count": 0,
                    "repository_count": 0,
                    "enabled_repository_count": 0,
                    "running_job_count": 0,
                },
            }

        source_rows = await self.db.execute(
            select(SS)
            .where(SS.subscription_id.in_(subscription_ids))
            .order_by(SS.source, SS.created_at.desc())
        )
        sub_sources = list(source_rows.scalars().all())

        repositories = []
        running_statuses = {"enqueued", "downloading", "downloaded", "importing"}
        running_job_count = 0

        for ss in sub_sources:
            can_download = False
            supports_gallerydl = False
            url_valid = False
            provider_name = ss.source
            provider_display_name = ss.source
            try:
                provider = registry.get(ss.source)
                provider_name = provider.source_name
                provider_display_name = provider.display_name
                can_download = bool(provider.capabilities.can_download)
                supports_gallerydl = bool(provider.capabilities.supports_gallerydl)
                normalized_url = provider.normalize_url(ss.source_url) if ss.source_url else None
                url_valid = bool(ss.source_url and provider.validate_url(normalized_url or ss.source_url))
            except Exception:
                url_valid = False

            latest_job_row = await self.db.execute(
                select(DJ)
                .where(DJ.subscription_source_id == ss.id)
                .order_by(DJ.created_at.desc())
                .limit(1)
            )
            latest_job = latest_job_row.scalar_one_or_none()
            latest_job_payload = None
            if latest_job:
                if latest_job.status in running_statuses:
                    running_job_count += 1
                latest_job_payload = {
                    "id": str(latest_job.id),
                    "status": latest_job.status,
                    "created_at": latest_job.created_at.isoformat() if latest_job.created_at else None,
                    "updated_at": latest_job.updated_at.isoformat() if latest_job.updated_at else None,
                    "error_log_excerpt": (latest_job.error_log or "")[:240] or None,
                }
                latest_job_payload["outcome"] = download_job_outcome(latest_job)

            is_repository = bool(can_download and url_valid and ss.source_url)
            repositories.append({
                "id": str(ss.id),
                "subscription_id": str(ss.subscription_id),
                "source": provider_name,
                "source_display_name": provider_display_name,
                "source_url": ss.source_url,
                "source_creator_id": ss.source_creator_id,
                "is_enabled": ss.is_enabled,
                "auth_healthy": ss.auth_healthy,
                "auth_status": ss.auth_status,
                "auth_error_reason": ss.auth_error_reason,
                "last_auth_checked_at": ss.last_auth_checked_at.isoformat() if ss.last_auth_checked_at else None,
                "last_successful_auth": ss.last_successful_auth.isoformat() if ss.last_successful_auth else None,
                "last_synced_at": ss.last_synced_at.isoformat() if ss.last_synced_at else None,
                "last_attempted_at": ss.last_attempted_at.isoformat() if ss.last_attempted_at else None,
                "can_download": can_download,
                "supports_gallerydl": supports_gallerydl,
                "url_valid": url_valid,
                "is_repository": is_repository,
                "latest_job": latest_job_payload,
                "created_at": ss.created_at.isoformat() if ss.created_at else None,
                "updated_at": ss.updated_at.isoformat() if ss.updated_at else None,
            })

        return {
            "creator_id": str(creator_id),
            "subscriptions": [{
                "id": str(s.id),
                "name": s.name,
                "is_active": s.is_active,
                "sync_enabled": s.sync_enabled,
                "sync_interval_hours": s.sync_interval_hours,
                "schedule_mode": s.schedule_mode,
                "scheduled_times": s.scheduled_times,
                "last_synced_at": s.last_synced_at.isoformat() if s.last_synced_at else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            } for s in subscriptions],
            "repositories": repositories,
            "summary": {
                "subscription_count": len(subscriptions),
                "repository_count": sum(1 for r in repositories if r["is_repository"]),
                "enabled_repository_count": sum(1 for r in repositories if r["is_repository"] and r["is_enabled"]),
                "running_job_count": running_job_count,
            },
        }
