from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text


class FakeRedis:
    def __init__(self, live_keys: set[str] | None = None):
        self.live_keys = live_keys or set()

    def exists(self, key: str) -> bool:
        return key in self.live_keys


async def _clear_recovery_tables(db):
    await db.execute(text("""
        TRUNCATE
            task_events,
            task_runs,
            storage_artifacts,
            import_jobs,
            download_jobs,
            subscription_sources,
            subscriptions,
            creators
        RESTART IDENTITY CASCADE
    """))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recover_import_pipeline_enqueues_downloaded_job_with_pending_artifacts(monkeypatch):
    from app.database import async_session, engine
    from app.jobs import download as download_module
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.services.import_recovery import recover_import_pipeline

    captured = {}

    async def fake_enqueue_import(download_job_id: str, import_error: str | None = None, new_json_paths=None):
        captured["download_job_id"] = download_job_id
        captured["import_error"] = import_error
        captured["new_json_paths"] = sorted(new_json_paths or [])
        return str(uuid4())

    monkeypatch.setattr(download_module, "_enqueue_import", fake_enqueue_import)
    old = datetime.now(timezone.utc) - timedelta(hours=2)

    try:
        async with async_session() as db:
            await _clear_recovery_tables(db)
            creator = Creator(name="recover", display_name="Recover")
            db.add(creator)
            await db.flush()
            sub = Subscription(creator_id=creator.id, name="Recover")
            db.add(sub)
            await db.flush()
            job = DownloadJob(
                subscription_id=sub.id,
                source="pixiv",
                source_url="https://www.pixiv.net/users/101",
                status="downloaded",
                updated_at=old,
            )
            db.add(job)
            await db.flush()
            db.add(StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/recover/100/metadata.json",
                source="pixiv",
                creator_dir="recover",
                source_work_id="100",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=job.id,
                state="new",
            ))
            await db.commit()

            result = await recover_import_pipeline(db, redis_client=FakeRedis(), stale_after_seconds=60)

            assert result["imports_enqueued"] == 1
            assert result["download_job_ids"] == [str(job.id)]
            assert captured["download_job_id"] == str(job.id)
            assert captured["new_json_paths"] == ["pixiv/recover/100/metadata.json"]
    finally:
        async with async_session() as db:
            await _clear_recovery_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recover_import_pipeline_marks_running_import_without_heartbeat_stale():
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.models.subscription import Subscription
    from app.services.import_recovery import recover_import_pipeline
    from app.services.tasks import TaskService

    old = datetime.now(timezone.utc) - timedelta(hours=2)

    try:
        async with async_session() as db:
            await _clear_recovery_tables(db)
            creator = Creator(name="stale-import", display_name="Stale Import")
            db.add(creator)
            await db.flush()
            sub = Subscription(creator_id=creator.id, name="Stale Import")
            db.add(sub)
            await db.flush()
            download = DownloadJob(
                subscription_id=sub.id,
                source="pixiv",
                source_url="https://www.pixiv.net/users/202",
                status="importing",
            )
            db.add(download)
            await db.flush()
            import_job = ImportJob(
                download_job_id=download.id,
                status="running",
                updated_at=old,
            )
            db.add(import_job)
            await db.flush()
            await TaskService(db).ensure_import_task(import_job)
            await db.commit()

            result = await recover_import_pipeline(db, redis_client=FakeRedis(), stale_after_seconds=60)
            await db.refresh(import_job)
            task = await TaskService(db).get_by_subject("import_job", import_job.id)

            assert result["imports_marked_stale"] == 1
            assert import_job.status == "stale"
            assert task is not None
            assert task.status == "stale"
            assert "lost import heartbeat" in (task.error_log or "")
    finally:
        async with async_session() as db:
            await _clear_recovery_tables(db)
        await engine.dispose()
