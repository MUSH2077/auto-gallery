from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text


class _HeartbeatPipeline:
    def __init__(self, live_keys: set[str]):
        self.live_keys = live_keys
        self.keys: list[str] = []

    def exists(self, key: str):
        self.keys.append(key)
        return self

    def execute(self):
        return [key in self.live_keys for key in self.keys]


class _HeartbeatRedis:
    def __init__(self, live_task_ids=()):
        self.live_keys = {
            f"task:{task_id}:heartbeat_ts" for task_id in live_task_ids
        }
        self.writes: dict[str, object] = {}

    def pipeline(self, *, transaction: bool):
        assert transaction is False
        return _HeartbeatPipeline(self.live_keys)

    def exists(self, key: str):
        return key in self.live_keys

    def setex(self, key: str, _ttl: int, value):
        self.writes[key] = value
        return True


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_child_feed_never_falls_back_to_unassigned_sibling():
    """An explicit child UUID is an exact feed even when none of it is runnable."""
    from app.database import async_session, engine
    from app.models.import_job import ImportJob
    from app.models.storage_artifact import StorageArtifact
    from app.services.artifact_ledger import ArtifactLedger

    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(db)
            child = ImportJob(download_job_id=parent.id, status="failed")
            db.add(child)
            await db.flush()
            db.add_all([
                StorageArtifact(
                    storage_root="downloads",
                    file_path="pixiv/bounded/own/metadata.json",
                    source="pixiv",
                    creator_dir="bounded",
                    source_work_id="own",
                    file_name="metadata.json",
                    artifact_type="metadata_json",
                    download_job_id=parent.id,
                    import_job_id=child.id,
                    state="failed",
                ),
                StorageArtifact(
                    storage_root="downloads",
                    file_path="pixiv/bounded/sibling/metadata.json",
                    source="pixiv",
                    creator_dir="bounded",
                    source_work_id="sibling",
                    file_name="metadata.json",
                    artifact_type="metadata_json",
                    download_job_id=parent.id,
                    state="new",
                ),
            ])
            await db.commit()

            assert await ArtifactLedger(db).new_metadata_paths(
                parent.id,
                import_job_id=child.id,
            ) == []
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_resets_only_child_assignment_reopens_parent_and_aggregates(
    monkeypatch,
):
    """A bounded retry owns its prior failed row and runs under an active parent."""
    import app.services.task_engine as task_engine

    from app.database import async_session, engine
    from app.models.import_job import ImportJob
    from app.models.storage_artifact import StorageArtifact
    from app.services.artifact_ledger import ArtifactLedger
    from app.services.import_lifecycle import coordinate_import_parent_completion
    from app.services.tasks import TaskService

    async def publish_and_commit(db, *_args, **_kwargs):
        await db.commit()
        return "existing"

    monkeypatch.setattr(task_engine, "publish_prepared_import", publish_and_commit)
    monkeypatch.setattr(
        task_engine.TaskEventPublisher,
        "publish_status_change",
        lambda *_args, **_kwargs: None,
    )

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
            parent.status = "failed"
            parent.error_log = "previous bounded failure"
            child = ImportJob(download_job_id=parent.id, status="failed")
            db.add(child)
            await db.flush()
            await TaskService(db).ensure_download_task(parent)
            await TaskService(db).ensure_import_task(child)
            own = StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/bounded/retry-own/metadata.json",
                source="pixiv",
                creator_dir="bounded",
                source_work_id="retry-own",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=parent.id,
                import_job_id=child.id,
                state="failed",
                last_error="parse failed",
            )
            sibling = StorageArtifact(
                storage_root="downloads",
                file_path="pixiv/bounded/retry-sibling/metadata.json",
                source="pixiv",
                creator_dir="bounded",
                source_work_id="retry-sibling",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=parent.id,
                state="new",
            )
            db.add_all([own, sibling])
            await db.commit()
            parent_id = parent.id
            child_id = child.id
            own_id = own.id
            sibling_id = sibling.id

            result = await task_engine.TaskEngine(db).retry_import(child_id)
            db.expire_all()
            retried_parent = await db.get(type(parent), parent_id)
            retried_child = await db.get(ImportJob, child_id)
            own = await db.get(StorageArtifact, own_id)
            sibling = await db.get(StorageArtifact, sibling_id)

            assert result["status"] == "enqueued"
            assert retried_parent.status == "importing"
            assert retried_parent.error_log is None
            assert own.state == "new"
            assert own.import_job_id == retried_child.id
            assert own.last_error is None
            assert sibling.state == "new"
            assert sibling.import_job_id is None
            assert await ArtifactLedger(db).new_metadata_paths(
                retried_parent.id,
                import_job_id=retried_child.id,
            ) == ["pixiv/bounded/retry-own/metadata.json"]

            sibling.state = "done"
            retried_child.status = "complete"
            completion = await coordinate_import_parent_completion(
                db,
                retried_child,
                status="complete",
                stats={
                    "works": 1,
                    "assets": 1,
                    "multi_page": 0,
                    "skipped": 0,
                    "existing": 0,
                },
                total_groups=1,
                message="Imported retry assignment",
            )
            assert completion.should_finalize is True
            assert completion.status == "complete"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_disk_publishers_distinguish_expired_and_live_heartbeats(
    tmp_path,
    monkeypatch,
):
    """Only the crashed admin publisher is staled, resumed, and closed."""
    import app.services.task_engine as task_engine

    from app.config import settings
    from app.database import async_session, engine
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.models.storage_artifact import StorageArtifact
    from app.models.task_run import TaskRun
    from app.services.import_recovery import recover_import_pipeline
    from app.services.tasks import TaskService

    download_root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(download_root))
    stale_publisher_id = uuid4()
    live_publisher_id = uuid4()
    redis = _HeartbeatRedis({live_publisher_id})

    async def fake_publish(*_args, **_kwargs):
        return "replayed"

    async def no_projection(*_args, **_kwargs):
        return 0

    monkeypatch.setattr("app.jobs.download.get_redis", lambda: redis)
    monkeypatch.setattr("app.jobs.download.publish_prepared_import", fake_publish)
    monkeypatch.setattr("app.jobs.download.publish_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(task_engine, "request_search_projection", no_projection)
    monkeypatch.setattr(
        task_engine.TaskEventPublisher,
        "publish_status_change",
        lambda *_args, **_kwargs: None,
    )

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    try:
        async with async_session() as db:
            await _clear(db)
            parents = []
            for label, publisher_id in (
                ("expired", stale_publisher_id),
                ("live", live_publisher_id),
            ):
                metadata = (
                    download_root
                    / "pixiv"
                    / "bounded"
                    / label
                    / "metadata.json"
                )
                metadata.parent.mkdir(parents=True, exist_ok=True)
                metadata.write_text(json.dumps({"id": label}), encoding="utf-8")
                parent = await _shared_parent(
                    db,
                    manifest={
                        "disk_import_recovery": True,
                        "bounded_import_publication_open": True,
                        "bounded_import_publisher_task_id": str(publisher_id),
                    },
                )
                task = await TaskService(db).create_task(
                    task_id=publisher_id,
                    kind="admin",
                    operation_type="admin-disk-import",
                    title=f"{label} publisher",
                    status="running",
                    queue_name="maintenance",
                )
                task.started_at = old
                task.updated_at = old
                parent.updated_at = old
                db.add(StorageArtifact(
                    storage_root="downloads",
                    file_path=f"pixiv/bounded/{label}/metadata.json",
                    source="pixiv",
                    creator_dir="bounded",
                    source_work_id=label,
                    file_name="metadata.json",
                    artifact_type="metadata_json",
                    download_job_id=parent.id,
                    state="new",
                ))
                parents.append(parent)
            await db.commit()
            stale_parent_id, live_parent_id = (parent.id for parent in parents)

            stale_count = await task_engine.TaskEngine(db).detect_stale_tasks(
                redis_client=redis,
                now=datetime.now(timezone.utc),
            )
            first = await recover_import_pipeline(
                db,
                redis_client=redis,
                stale_after_seconds=60,
                dispatch_grace_seconds=60,
            )
            second = await recover_import_pipeline(
                db,
                redis_client=redis,
                stale_after_seconds=60,
                dispatch_grace_seconds=60,
            )

            db.expire_all()
            stale_task = await db.get(TaskRun, stale_publisher_id)
            live_task = await db.get(TaskRun, live_publisher_id)
            stale_parent = await db.get(DownloadJob, stale_parent_id)
            live_parent = await db.get(DownloadJob, live_parent_id)
            stale_imports = list((await db.execute(
                select(ImportJob).where(ImportJob.download_job_id == stale_parent_id)
            )).scalars())
            live_imports = list((await db.execute(
                select(ImportJob).where(ImportJob.download_job_id == live_parent_id)
            )).scalars())

            assert stale_count == 1
            assert stale_task.status == "stale"
            assert live_task.status == "running"
            assert len(stale_imports) == 1
            assert live_imports == []
            assert stale_parent.manifest["bounded_import_publication_open"] is False
            assert live_parent.manifest["bounded_import_publication_open"] is True
            assert first["imports_enqueued"] == 1
            assert second["imports_enqueued"] == 0
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovery_assigns_failed_and_expired_page_without_touching_live_lease(
    tmp_path,
    monkeypatch,
):
    """Recovery publishes a non-empty exact child feed from every eligible state."""
    from app.config import settings
    from app.database import async_session, engine
    from app.models.import_job import ImportJob
    from app.models.storage_artifact import StorageArtifact
    from app.services.artifact_ledger import ArtifactLedger
    from app.services.import_recovery import recover_import_pipeline

    download_root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(download_root))
    redis = _HeartbeatRedis()

    async def fake_publish(*_args, **_kwargs):
        return "replayed"

    monkeypatch.setattr("app.jobs.download.get_redis", lambda: redis)
    monkeypatch.setattr("app.jobs.download.publish_prepared_import", fake_publish)
    monkeypatch.setattr("app.jobs.download.publish_progress", lambda *_a, **_k: None)

    now = datetime.now(timezone.utc)
    paths = {
        "failed": "pixiv/bounded/failed/metadata.json",
        "expired": "pixiv/bounded/expired/metadata.json",
        "live": "pixiv/bounded/live/metadata.json",
    }
    for label, relative in paths.items():
        metadata = download_root / relative
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(json.dumps({"id": label}), encoding="utf-8")

    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(db)
            parent.updated_at = now - timedelta(hours=2)
            expired_token = uuid4()
            live_token = uuid4()
            failed = StorageArtifact(
                storage_root="downloads",
                file_path=paths["failed"],
                source="pixiv",
                creator_dir="bounded",
                source_work_id="failed",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=parent.id,
                state="failed",
                last_error="previous failure",
            )
            expired = StorageArtifact(
                storage_root="downloads",
                file_path=paths["expired"],
                source="pixiv",
                creator_dir="bounded",
                source_work_id="expired",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=parent.id,
                state="importing",
                lease_token=expired_token,
                lease_expires_at=now - timedelta(minutes=5),
            )
            live = StorageArtifact(
                storage_root="downloads",
                file_path=paths["live"],
                source="pixiv",
                creator_dir="bounded",
                source_work_id="live",
                file_name="metadata.json",
                artifact_type="metadata_json",
                download_job_id=parent.id,
                state="importing",
                lease_token=live_token,
                lease_expires_at=now + timedelta(minutes=5),
            )
            db.add_all([failed, expired, live])
            await db.commit()
            failed_id = failed.id
            expired_id = expired.id
            live_id = live.id

            first = await recover_import_pipeline(
                db,
                redis_client=redis,
                stale_after_seconds=60,
            )
            second = await recover_import_pipeline(
                db,
                redis_client=redis,
                stale_after_seconds=60,
            )

            imports = list((await db.execute(
                select(ImportJob).where(ImportJob.download_job_id == parent.id)
            )).scalars())
            assert len(imports) == 1
            import_id = imports[0].id
            feed = await ArtifactLedger(db).new_metadata_paths(
                parent.id,
                import_job_id=import_id,
            )
            assert len(feed) == 2
            assert set(feed) == {paths["expired"], paths["failed"]}

            db.expire_all()
            failed = await db.get(StorageArtifact, failed_id)
            expired = await db.get(StorageArtifact, expired_id)
            live = await db.get(StorageArtifact, live_id)
            assert failed.state == "new"
            assert failed.import_job_id == import_id
            assert failed.last_error is None
            assert expired.state == "new"
            assert expired.import_job_id == import_id
            assert expired.lease_token is None
            assert live.state == "importing"
            assert live.import_job_id is None
            assert live.lease_token == live_token
            assert live.lease_expires_at > now
            assert first["imports_enqueued"] == 1
            assert second["imports_enqueued"] == 0
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_heartbeat_stale_child_uses_open_then_closed_sibling_aggregate(
    monkeypatch,
):
    """Heartbeat expiry cannot stale an open parent, then settles as stale."""
    import app.services.task_engine as task_engine

    from app.database import async_session, engine
    from app.models.import_job import ImportJob
    from app.services.import_lifecycle import (
        close_bounded_import_publication,
        project_import_pipeline_state,
    )
    from app.services.tasks import TaskService

    async def no_projection(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(task_engine, "request_search_projection", no_projection)
    monkeypatch.setattr(
        task_engine.TaskEventPublisher,
        "publish_status_change",
        lambda *_args, **_kwargs: None,
    )

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(db)
            stale_child = ImportJob(
                download_job_id=parent.id,
                status="running",
                created_at=old,
                updated_at=old,
            )
            live_sibling = ImportJob(
                download_job_id=parent.id,
                status="running",
                created_at=old,
                updated_at=old,
            )
            db.add_all([stale_child, live_sibling])
            await db.flush()
            parent_task = await TaskService(db).ensure_download_task(parent)
            stale_task = await TaskService(db).ensure_import_task(stale_child)
            live_task = await TaskService(db).ensure_import_task(live_sibling)
            stale_task.started_at = old
            stale_task.updated_at = old
            live_task.started_at = old
            live_task.updated_at = old
            await db.commit()
            parent_id = parent.id
            stale_child_id = stale_child.id
            live_sibling_id = live_sibling.id

            redis = _HeartbeatRedis({live_sibling_id})
            await task_engine.TaskEngine(db).detect_stale_tasks(
                redis_client=redis,
                now=datetime.now(timezone.utc),
            )
            db.expire_all()
            parent = await db.get(type(parent), parent_id)
            parent_task = await TaskService(db).get_by_subject(
                "download_job",
                parent_id,
            )
            stale_child = await db.get(ImportJob, stale_child_id)
            live_sibling = await db.get(ImportJob, live_sibling_id)
            assert stale_child.status == "stale"
            assert live_sibling.status == "running"
            assert parent.status == "importing"
            assert parent_task.status == "running"

            live_sibling.status = "complete"
            completion = await close_bounded_import_publication(db, parent.id)
            assert completion is not None
            assert completion.should_finalize is True
            assert completion.status == "stale"
            await project_import_pipeline_state(
                db,
                stale_child,
                status="stale",
                error="Redis heartbeat TTL expired while import was running",
                reason_code="lost_heartbeat",
            )
            await db.commit()
            assert parent.status == "stale"
            assert parent_task.status == "stale"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_dispatch_and_parent_projection_complete_without_deadlock(
    monkeypatch,
):
    """Invalid publication releases child locks before parent-first projection."""
    import app.services.import_dispatch as import_dispatch
    import app.services.import_lifecycle as import_lifecycle

    from app.database import async_session, engine
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.services.import_dispatch import (
        prepare_import_dispatch,
        publish_prepared_import,
    )
    from app.services.import_lifecycle import project_import_pipeline_state
    from app.services.tasks import TaskService

    invalid_path_holds_child = asyncio.Event()
    projection_holds_parent = asyncio.Event()
    original_persist = import_dispatch._persist_invalid_dispatch
    original_lock_parent = import_lifecycle._lock_parent

    async def interleaved_persist(db, job, task, error):
        invalid_path_holds_child.set()
        await asyncio.wait_for(projection_holds_parent.wait(), timeout=2)
        return await original_persist(db, job, task, error)

    async def signal_parent_lock(db, download_job_id):
        parent = await original_lock_parent(db, download_job_id)
        current = asyncio.current_task()
        if current is not None and current.get_name() == "bounded-projection":
            projection_holds_parent.set()
        return parent

    monkeypatch.setattr(import_dispatch, "_persist_invalid_dispatch", interleaved_persist)
    monkeypatch.setattr(import_lifecycle, "_lock_parent", signal_parent_lock)

    try:
        async with async_session() as setup_db:
            await _clear(setup_db)
            parent = await _shared_parent(setup_db)
            child = ImportJob(download_job_id=parent.id, status="enqueued")
            sibling = ImportJob(download_job_id=parent.id, status="running")
            setup_db.add_all([child, sibling])
            await setup_db.flush()
            parent_task = await TaskService(setup_db).ensure_download_task(parent)
            prepared = await prepare_import_dispatch(
                setup_db,
                child,
                parent_task_id=parent_task.id,
            )
            prepared.task.queue_name = "wrong-queue"
            await setup_db.commit()
            parent_id = parent.id
            child_id = child.id

        async def publish_invalid():
            async with async_session() as publish_db:
                return await publish_prepared_import(
                    publish_db,
                    child_id,
                    prepared.rq_job_id,
                )

        async def project_pause():
            await invalid_path_holds_child.wait()
            async with async_session() as projection_db:
                projected_child = await projection_db.get(ImportJob, child_id)
                await project_import_pipeline_state(
                    projection_db,
                    projected_child,
                    status="paused",
                )
                await projection_db.commit()

        publication, _ = await asyncio.wait_for(
            asyncio.gather(
                asyncio.create_task(
                    publish_invalid(),
                    name="invalid-publisher",
                ),
                asyncio.create_task(
                    project_pause(),
                    name="bounded-projection",
                ),
            ),
            timeout=5,
        )
        assert publication == "invalid"

        async with async_session() as db:
            child = await db.get(ImportJob, child_id)
            parent = await db.get(DownloadJob, parent_id)
            child_task = await TaskService(db).get_by_subject(
                "import_job",
                child_id,
            )
            assert child.status == "failed"
            assert child_task.status == "failed"
            assert parent.status == "importing"
            assert parent.manifest["bounded_import_batches"][str(child_id)][
                "status"
            ] == "failed"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
