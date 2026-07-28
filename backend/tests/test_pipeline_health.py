from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text


class FakeRedis:
    def __init__(self, live_keys: set[str] | None = None):
        self.live_keys = live_keys or set()

    def exists(self, key: str) -> bool:
        return key in self.live_keys


async def _clear_pipeline_tables(db):
    await db.execute(text("""
        TRUNCATE
            storage_artifacts,
            import_jobs,
            download_jobs,
            work_source_tags,
            work_tags,
            asset_sources,
            assets,
            work_sources,
            works,
            source_creators,
            subscription_sources,
            subscriptions,
            creators
        RESTART IDENTITY CASCADE
    """))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collect_pipeline_health_flags_downloaded_job_with_pending_artifacts_without_import():
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.services.pipeline_health import collect_pipeline_health

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
            creator = Creator(name="stuck", display_name="Stuck")
            db.add(creator)
            await db.flush()
            sub = Subscription(creator_id=creator.id, name="Stuck")
            db.add(sub)
            await db.flush()
            job = DownloadJob(
                subscription_id=sub.id,
                source="pixiv",
                source_url="https://www.pixiv.net/users/1",
                status="downloaded",
                created_at=old,
            )
            db.add(job)
            await db.flush()
            db.add(StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/stuck/100/metadata.json",
                source="pixiv",
                creator_dir="stuck",
                source_work_id="100",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=job.id,
                state="new",
            ))
            await db.flush()

            health = await collect_pipeline_health(db, redis_client=FakeRedis(), stale_after_seconds=60)

            assert health["ok"] is False
            assert health["download_statuses"]["downloaded"] == 1
            assert health["artifact_states"] == [
                {"state": "new", "artifact_type": "metadata_json", "count": 1}
            ]
            assert health["stuck"]["downloaded_without_import"][0]["id"] == str(job.id)
            assert health["stuck"]["downloaded_with_pending_artifacts"][0]["id"] == str(job.id)
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collect_pipeline_health_distinguishes_running_import_heartbeat():
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.models.subscription import Subscription
    from app.services.pipeline_health import collect_pipeline_health

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
            creator = Creator(name="heartbeat", display_name="Heartbeat")
            db.add(creator)
            await db.flush()
            sub = Subscription(creator_id=creator.id, name="Heartbeat")
            db.add(sub)
            await db.flush()
            job = DownloadJob(
                subscription_id=sub.id,
                source="pixiv",
                source_url="https://www.pixiv.net/users/2",
                status="importing",
                created_at=old,
            )
            db.add(job)
            await db.flush()
            import_job = ImportJob(download_job_id=job.id, status="running", created_at=old)
            db.add(import_job)
            await db.flush()

            missing = await collect_pipeline_health(db, redis_client=FakeRedis(), stale_after_seconds=60)
            live = await collect_pipeline_health(
                db,
                redis_client=FakeRedis({f"task:{import_job.id}:heartbeat_ts"}),
                stale_after_seconds=60,
            )

            assert missing["ok"] is False
            assert missing["stuck"]["importing_without_heartbeat"][0]["id"] == str(import_job.id)
            assert live["ok"] is True
            assert live["stuck"]["importing_without_heartbeat"] == []
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()
