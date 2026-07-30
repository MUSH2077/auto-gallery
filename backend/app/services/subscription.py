import logging
from uuid import UUID

from sqlalchemy import select, delete as sql_delete, update as sql_update, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.subscription import SubscriptionRepository
from app.models import SubscriptionSource, DownloadJob, ImportJob, CreatorLink, Subscription, SourceCreator
from app.providers import registry
from app.services.settings import get_subscription_defaults

logger = logging.getLogger(__name__)

class SubscriptionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = SubscriptionRepository(db)

    async def get_subscription(self, sub_id: UUID):
        sub = await self.repo.get(sub_id)
        if not sub:
            raise ValueError("Subscription not found")
        return sub

    async def create_subscription(self, data: dict):
        # Merge system defaults: provided values take precedence over defaults
        defaults = await get_subscription_defaults(self.db)
        merged = {**defaults, **{k: v for k, v in data.items() if v is not None}}
        sub = await self.repo.create(merged)
        await self.db.commit()
        return sub

    async def update_subscription(self, sub_id: UUID, data: dict):
        sub = await self.get_subscription(sub_id)
        old_creator_id = sub.creator_id
        new_creator_id = data.get("creator_id")

        sub = await self.repo.update(sub, data)

        # When creator_id changes, propagate to SourceCreators that were
        # previously linked to the old creator_id AND match this
        # subscription's sources.  This prevents works from being split
        # across two Creator records while avoiding cross-subscription
        # mutation.
        if (
            new_creator_id is not None
            and old_creator_id is not None
            and str(new_creator_id) != str(old_creator_id)
        ):
            # Collect the sources (e.g. "pixiv", "x") of this subscription
            ss_result = await self.db.execute(
                select(SubscriptionSource.source).where(
                    SubscriptionSource.subscription_id == sub_id
                )
            )
            sub_sources = [row[0] for row in ss_result.all()]
            if sub_sources:
                await self.db.execute(
                    sql_update(SourceCreator)
                    .where(
                        SourceCreator.creator_id == old_creator_id,
                        SourceCreator.source.in_(sub_sources),
                    )
                    .values(creator_id=new_creator_id)
                )
                logger.info(
                    "Propagated creator_id change: moved SourceCreators "
                    "from %s to %s (sources: %s)",
                    old_creator_id, new_creator_id, sub_sources,
                )

        await self.db.commit()
        return sub

    async def delete_subscription(self, sub_id: UUID):
        sub = await self.get_subscription(sub_id)

        # 1. Delete download jobs and their import jobs first
        djs = await self.db.execute(
            select(DownloadJob).where(DownloadJob.subscription_id == sub_id)
        )
        for dj in djs.scalars().all():
            await self.db.execute(
                sql_delete(ImportJob).where(ImportJob.download_job_id == dj.id)
            )
            await self.db.delete(dj)

        # 2. Delete subscription sources (no more FK references from download_jobs)
        await self.db.execute(
            sql_delete(SubscriptionSource).where(SubscriptionSource.subscription_id == sub_id)
        )

        # 3. Delete the subscription
        await self.db.delete(sub)
        await self.db.commit()

    async def add_source(self, data: dict):
        source = data.get("source", "")
        try:
            provider = registry.get(source)
        except KeyError:
            raise ValueError(f"Unknown source provider: {source}")
        url = data.get("source_url", "")
        if url:
            normalized = provider.normalize_url(url) or url
            if not provider.validate_url(normalized):
                raise ValueError(f"Invalid URL for source '{source}': {url}")
            data["source_url"] = normalized
        ss = await self.repo.add_source(data)
        await self.db.commit()

        # Auto-enrich creator with Danbooru reference data
        sub = await self.repo.get(ss.subscription_id)
        if sub:
            await self._enrich_creator_from_danbooru(sub.creator_id, url)

        return ss

    async def list_sources(self, subscription_id: UUID):
        return await self.repo.list_sources(subscription_id)

    async def update_source(self, ss_id: UUID, data: dict):
        ss = await self.repo.get_source(ss_id)
        if not ss:
            raise ValueError("SubscriptionSource not found")
        if "source_url" in data and data.get("source_url"):
            try:
                provider = registry.get(ss.source)
            except KeyError:
                raise ValueError(f"Unknown source provider: {ss.source}")
            normalized = provider.normalize_url(data["source_url"]) or data["source_url"]
            if not provider.validate_url(normalized):
                raise ValueError(f"Invalid URL for source '{ss.source}': {data['source_url']}")
            data["source_url"] = normalized
        ss = await self.repo.update_source(ss, data)
        await self.db.commit()
        # Refresh so the returned object is fully loaded for Pydantic serialization.
        # commit() expires all loaded state; without this, lazy-loading fails with
        # MissingGreenlet when FastAPI serializes the response.
        await self.db.refresh(ss)
        return ss

    async def delete_source(self, ss_id: UUID):
        ss = await self.repo.get_source(ss_id)
        if not ss:
            raise ValueError("SubscriptionSource not found")
        await self.repo.delete_source(ss)
        await self.db.commit()

    async def _enrich_creator_from_danbooru(self, creator_id: UUID, source_url: str) -> int:
        """Query Danbooru for this source URL and create CreatorLink suggestions."""
        from app.services.creator_enrichment import fetch_and_link_danbooru_artist
        result = await fetch_and_link_danbooru_artist(
            self.db, creator_id, source_url=source_url, reraise_unavailable=False,
        )
        if result:
            logger.info("Danbooru enrichment: %d links created for creator %s from URL %s",
                        result, creator_id, source_url)
        return result

    async def trigger_sync(self, subscription_id: UUID) -> dict:
        from app.services.tasks import TaskService
        from app.services.subscription_enqueue import enqueue_subscription_source_sync
        from app.services.cache import invalidate_api_caches

        sub = await self.db.get(Subscription, subscription_id)
        if not sub:
            raise ValueError("Subscription not found")

        sources = await self.db.execute(
            select(SubscriptionSource).where(
                and_(
                    SubscriptionSource.subscription_id == subscription_id,
                    SubscriptionSource.is_enabled == True,
                )
            )
        )
        sub_sources = sources.scalars().all()
        if not sub_sources:
            return {"status": "ok", "message": "No enabled sources", "job_ids": []}

        task_service = TaskService(self.db)
        parent_task = await task_service.create_task(
            kind="admin",
            operation_type="subscription-sync-batch",
            title=f"Sync subscription {subscription_id}",
            status="running",
            queue_name="downloads",
            progress={"phase": "scanning", "label": "Checking subscription sources", "current": 0, "total": len(sub_sources)},
            meta={"subscription_id": str(subscription_id), "scope": "subscription"},
        )

        job_ids = []
        task_ids = []
        skipped = []
        errors = []

        for index, ss in enumerate(sub_sources, start=1):
            result = await enqueue_subscription_source_sync(
                self.db,
                ss.id,
                trigger="manual_subscription",
                force=False,
                parent_task_id=parent_task.id,
                force_reason="subscription_sync_now",
            )
            if result["status"] == "enqueued":
                job_ids.append(result["job_id"])
                if result.get("task_id"):
                    task_ids.append(result["task_id"])
            elif result["status"] == "error":
                errors.append(result)
            else:
                skipped.append(result)
            await task_service.update_task(
                parent_task,
                progress={"phase": "enqueuing", "label": "Enqueuing source downloads", "current": index, "total": len(sub_sources)},
            )

        summary = {
            "enqueued_count": len(job_ids),
            "skipped_count": len(skipped),
            "error_count": len(errors),
            "job_ids": job_ids,
            "task_ids": task_ids,
            "skipped": skipped,
            "errors": errors,
        }

        if errors and not job_ids:
            await task_service.update_task(parent_task, status="failed", result=summary, error="All enqueue attempts failed")
            await self.db.commit()
            return {"status": "error", "message": "All enqueue attempts failed", "task_id": str(parent_task.id), **summary}
        if errors:
            invalidate_api_caches("subscriptions", "creators")
            await task_service.update_task(parent_task, status="complete", result=summary, progress={"phase": "complete", "label": "Subscription sync enqueued", "current": len(sub_sources), "total": len(sub_sources)})
            await self.db.commit()
            return {"status": "partial_error", "message": f"Enqueued {len(job_ids)} jobs, {len(errors)} failed", "task_id": str(parent_task.id), **summary}
        invalidate_api_caches("subscriptions", "creators")
        await task_service.update_task(parent_task, status="complete", result=summary, progress={"phase": "complete", "label": "Subscription sync enqueued", "current": len(sub_sources), "total": len(sub_sources)})
        await self.db.commit()
        return {"status": "ok", "message": f"Enqueued {len(job_ids)} download jobs", "task_id": str(parent_task.id), **summary}
