from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text


async def _clear(db):
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


async def _shared_parent(db, *, manifest=None):
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.subscription import Subscription

    creator = Creator(name=f"bounded-parent-{uuid4()}")
    db.add(creator)
    await db.flush()
    subscription = Subscription(
        creator_id=creator.id,
        name="Bounded parent",
    )
    db.add(subscription)
    await db.flush()
    parent = DownloadJob(
        subscription_id=subscription.id,
        source="pixiv",
        source_url="https://www.pixiv.net/users/1980643",
        status="importing",
        manifest=manifest or {
            "disk_import_recovery": True,
            "bounded_import_publication_open": True,
        },
    )
    db.add(parent)
    await db.flush()
    return parent


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bounded_import_children_finalize_shared_parent_only_after_last_batch():
    """A completed 25-work child must not terminalize a parent with siblings."""
    from app.database import async_session, engine
    from app.models.import_job import ImportJob
    from app.services.import_lifecycle import (
        close_bounded_import_publication,
        coordinate_import_parent_completion,
    )

    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(db)
            first = ImportJob(download_job_id=parent.id, status="complete")
            second = ImportJob(download_job_id=parent.id, status="running")
            db.add_all([first, second])
            await db.flush()

            first_result = await coordinate_import_parent_completion(
                db,
                first,
                status="complete",
                stats={
                    "works": 25,
                    "assets": 25,
                    "multi_page": 0,
                    "skipped": 0,
                    "existing": 0,
                },
                total_groups=25,
                message="Imported 25 works",
            )
            assert first_result.should_finalize is False
            assert first_result.parent.status == "importing"
            await db.commit()

            second.status = "complete"
            second_result = await coordinate_import_parent_completion(
                db,
                second,
                status="complete",
                stats={
                    "works": 1,
                    "assets": 2,
                    "multi_page": 1,
                    "skipped": 0,
                    "existing": 0,
                },
                total_groups=1,
                message="Imported 1 work",
            )
            assert second_result.should_finalize is False

            final_result = await close_bounded_import_publication(db, parent.id)
            assert final_result is not None
            assert final_result.should_finalize is True
            assert final_result.status == "complete"
            assert final_result.total_groups == 26
            assert final_result.stats == {
                "works": 26,
                "assets": 27,
                "multi_page": 1,
                "skipped": 0,
                "existing": 0,
            }
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_released_children_keep_disjoint_durable_path_assignments():
    """Lease cleanup must not collapse two retry feeds into one unassigned pool."""
    from app.database import async_session, engine
    from app.models.import_job import ImportJob
    from app.models.storage_artifact import StorageArtifact
    from app.services.artifact_ledger import ArtifactLedger

    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(db)
            first_token = uuid4()
            second_token = uuid4()
            first = ImportJob(
                download_job_id=parent.id,
                status="running",
                execution_token=first_token,
            )
            second = ImportJob(
                download_job_id=parent.id,
                status="running",
                execution_token=second_token,
            )
            db.add_all([first, second])
            await db.flush()
            db.add_all([
                StorageArtifact(
                    storage_root="downloads",
                    file_path="pixiv/bounded/001/metadata.json",
                    source="pixiv",
                    creator_dir="bounded",
                    source_work_id="001",
                    file_name="metadata.json",
                    artifact_type="metadata_json",
                    download_job_id=parent.id,
                    import_job_id=first.id,
                    lease_token=first_token,
                    state="importing",
                ),
                StorageArtifact(
                    storage_root="downloads",
                    file_path="pixiv/bounded/002/metadata.json",
                    source="pixiv",
                    creator_dir="bounded",
                    source_work_id="002",
                    file_name="metadata.json",
                    artifact_type="metadata_json",
                    download_job_id=parent.id,
                    import_job_id=second.id,
                    lease_token=second_token,
                    state="importing",
                ),
            ])
            await db.commit()

            assert await ArtifactLedger(db).release_owned_leases(
                first.id,
                first_token,
            ) == {"001"}
            assert await ArtifactLedger(db).release_owned_leases(
                second.id,
                second_token,
            ) == {"002"}
            await db.commit()

            assert await ArtifactLedger(db).new_metadata_paths(
                parent.id,
                import_job_id=first.id,
            ) == ["pixiv/bounded/001/metadata.json"]
            assert await ArtifactLedger(db).new_metadata_paths(
                parent.id,
                import_job_id=second.id,
            ) == ["pixiv/bounded/002/metadata.json"]
            rows = list((await db.execute(
                select(StorageArtifact).order_by(StorageArtifact.file_path)
            )).scalars())
            assert [row.import_job_id for row in rows] == [first.id, second.id]
            assert [row.state for row in rows] == ["new", "new"]
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_open_publisher_recovery_resumes_one_page_and_closes_idempotently(
    tmp_path,
    monkeypatch,
):
    """A crashed publisher must not strand its adopted page or open flag."""
    from app.config import settings
    from app.database import async_session, engine
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.models.storage_artifact import StorageArtifact
    from app.services.import_recovery import recover_import_pipeline

    download_root = tmp_path / "downloads"
    metadata = download_root / "pixiv" / "bounded" / "001" / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"id": 1}), encoding="utf-8")
    monkeypatch.setattr(settings, "download_root", str(download_root))

    class FakeRedis:
        def setex(self, *_args, **_kwargs):
            return True

        def exists(self, *_args, **_kwargs):
            return False

    async def fake_publish(*_args, **_kwargs):
        return "replayed"

    monkeypatch.setattr("app.jobs.download.get_redis", lambda: FakeRedis())
    monkeypatch.setattr("app.jobs.download.publish_prepared_import", fake_publish)
    monkeypatch.setattr("app.jobs.download.publish_progress", lambda *_a, **_k: None)

    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(db)
            parent.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
            db.add(StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/bounded/001/metadata.json",
                source="pixiv",
                creator_dir="bounded",
                source_work_id="001",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=parent.id,
                state="new",
            ))
            await db.commit()
            parent_id = parent.id

            first = await recover_import_pipeline(
                db,
                redis_client=FakeRedis(),
                stale_after_seconds=60,
                dispatch_grace_seconds=60,
            )
            second = await recover_import_pipeline(
                db,
                redis_client=FakeRedis(),
                stale_after_seconds=60,
                dispatch_grace_seconds=60,
            )

            db.expire_all()
            recovered_parent = await db.get(DownloadJob, parent_id)
            imports = list((await db.execute(
                select(ImportJob).where(ImportJob.download_job_id == parent_id)
            )).scalars())
            artifact = (await db.execute(select(StorageArtifact))).scalar_one()
            assert len(imports) == 1
            assert artifact.import_job_id == imports[0].id
            assert recovered_parent.manifest["bounded_import_publication_open"] is False
            assert first["imports_enqueued"] == 1
            assert second["imports_enqueued"] == 0
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_open_publisher_recovery_finalizes_terminal_child_without_a_page():
    """Recovery must close and finalize when a crashed publisher has no next page."""
    from app.database import async_session, engine
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.services.import_recovery import recover_import_pipeline

    try:
        async with async_session() as db:
            await _clear(db)
            child_id = uuid4()
            parent = await _shared_parent(
                db,
                manifest={
                    "disk_import_recovery": True,
                    "bounded_import_publication_open": True,
                    "bounded_import_batches": {
                        str(child_id): {
                            "status": "complete",
                            "stats": {
                                "works": 1,
                                "assets": 2,
                                "multi_page": 0,
                                "skipped": 0,
                                "existing": 0,
                            },
                            "total_groups": 1,
                            "message": "Imported one work",
                        },
                    },
                },
            )
            parent.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
            db.add(ImportJob(
                id=child_id,
                download_job_id=parent.id,
                status="complete",
            ))
            await db.commit()
            parent_id = parent.id

            first = await recover_import_pipeline(db, stale_after_seconds=60)
            second = await recover_import_pipeline(db, stale_after_seconds=60)

            db.expire_all()
            recovered_parent = await db.get(DownloadJob, parent_id)
            assert recovered_parent.status == "complete"
            assert recovered_parent.manifest["bounded_import_publication_open"] is False
            assert first["imports_enqueued"] == 0
            assert second["imports_enqueued"] == 0
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovery_does_not_close_over_a_skip_locked_unassigned_page():
    """A competing recovery lock is evidence of work, not an empty publisher."""
    from app.database import async_session, engine
    from app.models.download_job import DownloadJob
    from app.models.storage_artifact import StorageArtifact
    from app.services.import_recovery import recover_import_pipeline

    try:
        async with async_session() as setup_db:
            await _clear(setup_db)
            parent = await _shared_parent(setup_db)
            parent.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
            artifact = StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/bounded/locked/metadata.json",
                source="pixiv",
                creator_dir="bounded",
                source_work_id="locked",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=parent.id,
                state="new",
            )
            setup_db.add(artifact)
            await setup_db.commit()
            parent_id = parent.id
            artifact_id = artifact.id

        async with async_session() as lock_db:
            await lock_db.execute(
                select(StorageArtifact)
                .where(StorageArtifact.id == artifact_id)
                .with_for_update()
            )
            async with async_session() as recovery_db:
                recovered = await recover_import_pipeline(
                    recovery_db,
                    stale_after_seconds=60,
                )
                recovery_db.expire_all()
                refreshed = await recovery_db.get(DownloadJob, parent_id)
                assert recovered["imports_enqueued"] == 0
                assert refreshed.manifest["bounded_import_publication_open"] is True
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("child_status", ["paused", "cancelled", "failed"])
async def test_non_success_child_cannot_project_over_active_shared_parent(child_status):
    """Pause/cancel/failure of one bounded child must leave active siblings alive."""
    from app.database import async_session, engine
    from app.models.import_job import ImportJob
    from app.services.import_lifecycle import project_import_pipeline_state
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(db)
            child = ImportJob(download_job_id=parent.id, status=child_status)
            sibling = ImportJob(download_job_id=parent.id, status="running")
            db.add_all([child, sibling])
            await db.flush()
            parent_task = await TaskService(db).ensure_download_task(parent)

            await project_import_pipeline_state(
                db,
                child,
                status=child_status,
                error="fixture failure" if child_status == "failed" else None,
            )
            await db.commit()

            assert parent.status == "importing"
            assert parent_task.status == "running"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_parse_cannot_fail_parent_while_sibling_is_running(
    tmp_path,
    monkeypatch,
):
    """The real empty-parse exit must use shared-parent coordination."""
    from app.config import settings
    from app.database import async_session, engine
    from app.jobs import import_runner
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.models.storage_artifact import StorageArtifact

    download_root = tmp_path / "downloads"
    metadata = download_root / "pixiv" / "bounded" / "invalid" / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings, "download_root", str(download_root))

    class NoopControl:
        command = None
        reason = None

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    @asynccontextmanager
    async def unthrottled(*_args, **_kwargs):
        yield SimpleNamespace(work_units=25, effective_scale=1.0)

    monkeypatch.setattr(import_runner, "ControlListener", NoopControl)
    monkeypatch.setattr(import_runner, "HeartbeatPublisher", NoopControl)
    monkeypatch.setattr(import_runner, "_import_resource_slice", unthrottled)

    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(db)
            child = ImportJob(download_job_id=parent.id, status="enqueued")
            sibling = ImportJob(download_job_id=parent.id, status="running")
            db.add_all([child, sibling])
            await db.flush()
            db.add(StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/bounded/invalid/metadata.json",
                source="pixiv",
                creator_dir="bounded",
                source_work_id="invalid",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=parent.id,
                import_job_id=child.id,
                state="new",
            ))
            await db.commit()
            child_id = child.id
            parent_id = parent.id

        await import_runner.run_import_job(str(child_id))

        async with async_session() as db:
            child = await db.get(ImportJob, child_id)
            parent = await db.get(DownloadJob, parent_id)
            assert child.status == "failed"
            assert parent.status == "importing"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancelled_batch_wins_only_after_publication_and_siblings_finish():
    """Cancellation is held while active, then wins terminal aggregation."""
    from app.database import async_session, engine
    from app.models.import_job import ImportJob
    from app.services.import_lifecycle import coordinate_import_parent_completion

    zero_stats = {
        "works": 0,
        "assets": 0,
        "multi_page": 0,
        "skipped": 0,
        "existing": 0,
    }
    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(
                db,
                manifest={
                    "disk_import_recovery": True,
                    "bounded_import_publication_open": False,
                },
            )
            cancelled = ImportJob(download_job_id=parent.id, status="cancelled")
            sibling = ImportJob(download_job_id=parent.id, status="running")
            db.add_all([cancelled, sibling])
            await db.flush()

            waiting = await coordinate_import_parent_completion(
                db,
                cancelled,
                status="cancelled",
                stats=zero_stats,
                total_groups=0,
                message="Cancelled",
            )
            assert waiting.should_finalize is False

            sibling.status = "complete"
            terminal = await coordinate_import_parent_completion(
                db,
                sibling,
                status="complete",
                stats=zero_stats,
                total_groups=0,
                message="No changes",
            )
            assert terminal.should_finalize is True
            assert terminal.status == "cancelled"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enqueue_waits_for_parent_lock_and_preserves_child_batch_manifest(monkeypatch):
    """Concurrent enqueue and child completion must retain both JSONB updates."""
    from app.database import async_session, engine
    from app.jobs.download import _enqueue_import
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.repositories.download_job import DownloadJobRepository
    from app.services.import_lifecycle import project_import_pipeline_state
    from app.services.tasks import TaskService

    class FakeRedis:
        pass

    async def fake_publish(*_args, **_kwargs):
        return "replayed"

    monkeypatch.setattr("app.jobs.download.get_redis", lambda: FakeRedis())
    monkeypatch.setattr("app.jobs.download.publish_prepared_import", fake_publish)
    monkeypatch.setattr("app.jobs.download.publish_progress", lambda *_a, **_k: None)

    parent_loaded = asyncio.Event()
    continue_enqueue = asyncio.Event()
    original_update_status = DownloadJobRepository.update_status

    async def pause_after_unlocked_parent_read(self, job, status, error_log=None):
        parent_loaded.set()
        await continue_enqueue.wait()
        return await original_update_status(self, job, status, error_log)

    monkeypatch.setattr(
        DownloadJobRepository,
        "update_status",
        pause_after_unlocked_parent_read,
    )

    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(db)
            child = ImportJob(download_job_id=parent.id, status="failed")
            db.add(child)
            await db.flush()
            await TaskService(db).ensure_download_task(parent)
            await db.commit()
            parent_id = parent.id
            child_id = child.id

        enqueue_task = asyncio.create_task(_enqueue_import(str(parent_id)))
        await asyncio.wait_for(parent_loaded.wait(), timeout=5)

        child_result_committed = asyncio.Event()

        async def commit_child_result():
            async with async_session() as child_db:
                child = await child_db.get(ImportJob, child_id)
                await project_import_pipeline_state(
                    child_db,
                    child,
                    status="failed",
                    error="Malformed bounded batch",
                )
                await child_db.commit()
                child_result_committed.set()

        child_task = asyncio.create_task(commit_child_result())
        try:
            await asyncio.wait_for(child_result_committed.wait(), timeout=0.25)
        except TimeoutError:
            # Correct lock ordering blocks the child result behind enqueue's
            # parent lock. The old unlocked read lets it commit first.
            pass
        continue_enqueue.set()
        enqueued_id, _ = await asyncio.wait_for(
            asyncio.gather(enqueue_task, child_task),
            timeout=5,
        )

        async with async_session() as db:
            parent = await db.get(DownloadJob, parent_id)
            batches = parent.manifest.get("bounded_import_batches") or {}
            created_events = [
                event
                for event in parent.manifest.get("events") or []
                if event.get("event") == "import_job_created"
            ]
            assert batches[str(child_id)]["status"] == "failed"
            assert created_events[-1]["import_job_id"] == enqueued_id
            assert int((await db.execute(
                select(func.count(ImportJob.id)).where(
                    ImportJob.download_job_id == parent_id,
                )
            )).scalar_one()) == 2
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
