"""Real PostgreSQL regressions for terminal import retry and projection locking."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def db():
    from app.database import async_session, engine
    async with async_session() as session:
        await session.execute(text("TRUNCATE task_runs, assets, works, creators, users, search_index_states, search_projection_outbox, scheduler_batch_items, scheduler_batches RESTART IDENTITY CASCADE"))
        await session.commit()
        yield session
        await session.rollback()
    await engine.dispose()


async def test_projection_generation_commits_after_opposite_task_lock_order(db):
    from app.database import async_session
    from app.models import TaskRun, SearchIndexState, SearchProjectionOutbox
    from app.services.search_projection_outbox import request_search_projection, DEFAULT_SUBSCRIPTIONS_INDEX_UID

    task = TaskRun(kind="admin", status="running")
    db.add(task)
    await db.commit()
    task_id = task.id
    task_locked, projection_requested = asyncio.Event(), asyncio.Event()

    async def terminal_writer():
        async with async_session() as session:
            current = (await session.execute(select(TaskRun).where(TaskRun.id == task_id).with_for_update())).scalar_one()
            task_locked.set()
            await projection_requested.wait()
            await request_search_projection(session, subscription_ids=[uuid4()])
            current.title = "terminal transaction committed"
            await session.commit()

    async def projection_then_task_writer():
        await task_locked.wait()
        async with async_session() as session:
            await request_search_projection(session, subscription_ids=[uuid4()])
            projection_requested.set()
            current = (await session.execute(select(TaskRun).where(TaskRun.id == task_id).with_for_update())).scalar_one()
            current.status = "complete"
            await session.commit()

    outcomes = await asyncio.wait_for(asyncio.gather(terminal_writer(), projection_then_task_writer(), return_exceptions=True), 10)
    assert not [str(result) for result in outcomes if isinstance(result, BaseException)]
    await db.rollback()
    state = (await db.execute(select(SearchIndexState).where(SearchIndexState.index_uid == DEFAULT_SUBSCRIPTIONS_INDEX_UID))).scalar_one()
    assert state.database_generation == 2
    assert state.status == "catching_up"
    assert len((await db.execute(select(SearchProjectionOutbox))).scalars().all()) == 2
    assert (await db.get(TaskRun, task_id, populate_existing=True)).status == "complete"


async def test_projection_generation_savepoints_and_outer_rollback_are_atomic(db):
    from app.models import SearchIndexState, SearchProjectionOutbox, TaskRun
    from app.services.search_projection_outbox import request_search_projection, DEFAULT_SUBSCRIPTIONS_INDEX_UID, DEFAULT_TAGS_INDEX_UID

    retained, rolled_back, nested_retained = uuid4(), uuid4(), uuid4()
    await request_search_projection(db, subscription_ids=[retained])
    await request_search_projection(db, subscription_ids=[retained])
    nested = await db.begin_nested()
    await request_search_projection(db, tag_ids=[rolled_back])
    await nested.rollback()
    async with db.begin_nested():
        await request_search_projection(db, tag_ids=[nested_retained])
    # The global hot rows must remain untouched before the outer transaction
    # has finished its ORM/domain writes, even after SAVEPOINT release.
    assert not (await db.execute(select(SearchIndexState))).scalars().all()
    db.add(TaskRun(kind="admin", status="complete", title="pending ORM flush"))
    from sqlalchemy import event
    from app.database import engine
    writes = []
    def record_order(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO task_runs"):
            writes.append("task")
        if statement.startswith("INSERT INTO search_index_states"):
            writes.append(next(value for value in parameters if value in (DEFAULT_SUBSCRIPTIONS_INDEX_UID, DEFAULT_TAGS_INDEX_UID)))
    event.listen(engine.sync_engine, "before_cursor_execute", record_order)
    try:
        await db.commit()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_order)
    assert writes == ["task", *sorted((DEFAULT_SUBSCRIPTIONS_INDEX_UID, DEFAULT_TAGS_INDEX_UID))]
    states = {s.index_uid: s.database_generation for s in (await db.execute(select(SearchIndexState))).scalars()}
    assert states == {DEFAULT_SUBSCRIPTIONS_INDEX_UID: 2, DEFAULT_TAGS_INDEX_UID: 1}
    assert {r.entity_id for r in (await db.execute(select(SearchProjectionOutbox))).scalars()} == {str(retained), str(nested_retained)}
    await db.rollback()
    await request_search_projection(db, subscription_ids=[uuid4()])
    async with db.begin_nested():
        await request_search_projection(db, tag_ids=[uuid4()])
    await db.rollback()
    # Reusing the same Session after rollback must not leak pending intents.
    db.add(TaskRun(kind="admin", status="complete"))
    await db.commit()
    assert {s.index_uid: s.database_generation for s in (await db.execute(select(SearchIndexState))).scalars()} == states
    assert len((await db.execute(select(SearchProjectionOutbox))).scalars().all()) == 2


async def _import_fixture(db, tmp_path, monkeypatch, *, corrupt=False, number=1):
    from PIL import Image
    from app.config import settings
    from app.models import Creator, Subscription, SubscriptionSource, DownloadJob, ImportJob, StorageArtifact
    from app.services.tasks import TaskService
    from app.jobs import import_runner

    downloads, library = tmp_path / "downloads", tmp_path / "library"
    monkeypatch.setattr(settings, "download_root", str(downloads))
    monkeypatch.setattr(settings, "library_root", str(library))
    @asynccontextmanager
    async def admitted_slice(*args, **kwargs):
        yield SimpleNamespace(work_units=1, effective_scale=1.0)
    monkeypatch.setattr(import_runner, "_import_resource_slice", admitted_slice)
    creator = Creator(name="Finalization regression")
    db.add(creator)
    await db.flush()
    subscription = Subscription(creator_id=creator.id, name="Finalization regression")
    db.add(subscription)
    await db.flush()
    source = SubscriptionSource(subscription_id=subscription.id, source="pixiv", source_creator_id="88001", source_url="https://www.pixiv.net/users/88001")
    db.add(source)
    await db.flush()
    parent = DownloadJob(subscription_id=subscription.id, subscription_source_id=source.id, source="pixiv", source_url=source.source_url, status="importing")
    db.add(parent)
    await db.flush()
    child = ImportJob(download_job_id=parent.id, status="enqueued")
    db.add(child)
    await db.flush()
    await TaskService(db).ensure_download_task(parent)
    await TaskService(db).ensure_import_task(child)
    for index in range(number):
        workdir = downloads / f"pixiv/88001/{8800100 + index}"
        workdir.mkdir(parents=True)
        media = workdir / f"{8800100 + index}_p0.jpg"
        Image.new("RGB", (32, 32), "blue").save(media)
        metadata = workdir / f"{8800100 + index}_p0.jpg.json"
        metadata.write_text("{bad JSON" if corrupt else json.dumps({
            "id": 8800100 + index, "num": 0, "title": "Checkpoint work", "user": {"id": 88001, "name": "Finalization regression", "account": "88001"},
            "tags": [], "date": "2026-09-08T00:00:00+00:00", "page_count": 1, "width": 32, "height": 32,
        }))
        for path, kind in ((media, "image"), (metadata, "metadata_json")):
            db.add(StorageArtifact(storage_root="downloads", file_path=str(path.relative_to(downloads)), source="pixiv", creator_dir="88001", source_work_id=str(8800100 + index),
                                   file_name=path.name, artifact_type=kind, download_job_id=parent.id, import_job_id=child.id, state="new"))
    await db.commit()
    return parent.id, child.id, metadata


@pytest.mark.parametrize("without_checkpoint", [False, True])
async def test_retry_after_committed_import_finalization_preserves_outcome_without_reimport(db, tmp_path, monkeypatch, without_checkpoint):
    from app.jobs import import_runner
    from app.models import DownloadJob, ImportJob, Work, Asset, StorageArtifact, TaskRun, RepositorySyncReceipt
    parent_id, child_id, metadata = await _import_fixture(db, tmp_path, monkeypatch)
    original_finalize = import_runner.finalize_download_job
    injected = False
    async def fail_once(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            raise RuntimeError("Injected terminal transaction abort after durable artifact checkpoint")
        return await original_finalize(*args, **kwargs)
    monkeypatch.setattr(import_runner, "finalize_download_job", fail_once)
    await import_runner.run_import_job(str(child_id))
    await db.rollback()
    child = await db.get(ImportJob, child_id, populate_existing=True)
    assert injected
    assert child.status == "enqueued"
    assert child.import_retry_count == 1
    metadata.unlink(missing_ok=True)  # Completed download metadata may already be cleaned up.
    assert {r.state for r in (await db.execute(select(StorageArtifact).where(StorageArtifact.storage_root == "downloads"))).scalars()} == {"done"}
    before_works = {w.id for w in (await db.execute(select(Work))).scalars()}
    before_assets = {a.id for a in (await db.execute(select(Asset))).scalars()}
    assert len(before_works) == len(before_assets) == 1
    task = (await db.execute(select(TaskRun).where(TaskRun.subject_type == "import_job", TaskRun.subject_id == child_id))).scalar_one()
    dispatch = task.meta[import_runner.IMPORT_DISPATCH_META_KEY]
    assert (datetime.fromisoformat(dispatch["available_at"]) - datetime.fromisoformat(dispatch["prepared_at"])).total_seconds() >= 59
    if without_checkpoint:
        from app.services.import_completion_checkpoint import CHECKPOINT_KEY
        task.meta = {key: value for key, value in task.meta.items() if key != CHECKPOINT_KEY}
        await db.commit()
    await db.rollback()
    async def forbid_reimport(*args, **kwargs):
        raise AssertionError("Completed chunks must not re-enter domain import")
    monkeypatch.setattr(import_runner, "_insert_bulk_domain_with_fallback", forbid_reimport)
    await import_runner.run_import_job(str(child_id))
    await db.rollback()
    child = await db.get(ImportJob, child_id, populate_existing=True)
    if without_checkpoint:
        assert child.status == "failed"
        assert "import_completion_evidence_missing" in child.error_log
        assert {w.id for w in (await db.execute(select(Work))).scalars()} == before_works
        assert {a.id for a in (await db.execute(select(Asset))).scalars()} == before_assets
        return
    assert child.status == "complete", child.error_log
    assert child.error_log is None
    assert (await db.get(DownloadJob, parent_id, populate_existing=True)).status == "complete"
    assert {w.id for w in (await db.execute(select(Work))).scalars()} == before_works
    assert {a.id for a in (await db.execute(select(Asset))).scalars()} == before_assets
    tasks = (await db.execute(select(TaskRun).where(TaskRun.subject_id.in_((parent_id, child_id))).execution_options(populate_existing=True))).scalars().all()
    assert len(tasks) == 2 and all(task.status == "complete" for task in tasks)
    receipt = (await db.execute(select(RepositorySyncReceipt).where(RepositorySyncReceipt.source_download_job_id == parent_id))).scalar_one()
    assert receipt.status == "complete" and receipt.works_imported == 1


async def test_real_partial_import_preserves_exhausted_provider_failure(
    db,
    tmp_path,
    monkeypatch,
):
    from app.jobs import import_runner
    from app.models import (
        Asset,
        DownloadJob,
        ImportJob,
        RepositorySyncReceipt,
        TaskRun,
        Work,
    )
    from app.models.scheduler_batch import SchedulerBatch, SchedulerBatchItem
    from app.services.download_failure_evidence import (
        record_unresolved_provider_failure,
    )

    parent_id, child_id, _metadata = await _import_fixture(
        db,
        tmp_path,
        monkeypatch,
    )
    parent = await db.get(DownloadJob, parent_id)
    parent.retry_count = 4
    provider_reason = "gallery-dl exited with code 17"
    parent.error_log = provider_reason
    record_unresolved_provider_failure(
        parent,
        kind="nonzero",
        reason=provider_reason,
        max_retries=4,
    )
    batch = SchedulerBatch(
        task_id=uuid4(),
        request_id=uuid4(),
        mode="force_eligible",
        state="active",
    )
    db.add(batch)
    await db.flush()
    item = SchedulerBatchItem(
        batch_id=batch.id,
        source_id=parent.subscription_source_id,
        source=parent.source,
        status="importing",
        download_job_id=parent.id,
        owns_download=True,
    )
    db.add(item)
    await db.commit()

    await import_runner.run_import_job(str(child_id))

    await db.rollback()
    parent = await db.get(DownloadJob, parent_id, populate_existing=True)
    child = await db.get(ImportJob, child_id, populate_existing=True)
    receipt = (await db.execute(select(RepositorySyncReceipt).where(
        RepositorySyncReceipt.source_download_job_id == parent_id
    ))).scalar_one()
    assert child.status == "complete"
    assert parent.status == "failed"
    assert parent.error_log == provider_reason
    assert receipt.status == "failed"
    assert receipt.error_excerpt == provider_reason
    assert receipt.works_imported == 1
    parent_task = (await db.execute(select(TaskRun).where(
        TaskRun.subject_type == "download_job",
        TaskRun.subject_id == parent_id,
    ))).scalar_one()
    assert parent_task.status == "failed"
    assert parent_task.error_log == provider_reason
    assert len((await db.execute(select(Work))).scalars().all()) == 1
    assert len((await db.execute(select(Asset))).scalars().all()) == 1
    from app.services.scheduler_batches import reconcile_item

    await reconcile_item(db, item)
    assert item.status == "failed"
    assert item.outcome["works_imported"] == 1


async def test_later_success_plans_and_imports_retained_retry_artifact_once(
    db,
    tmp_path,
    monkeypatch,
):
    from app.jobs import download, import_runner
    from app.models import Asset, DownloadJob, ImportJob, StorageArtifact, Work
    from app.services.download_failure_evidence import (
        clear_unresolved_provider_failure,
        record_unresolved_provider_failure,
        unresolved_provider_failure,
    )

    parent_id, abandoned_child_id, _metadata = await _import_fixture(
        db,
        tmp_path,
        monkeypatch,
    )
    artifacts = list((await db.execute(select(StorageArtifact))).scalars())
    for artifact in artifacts:
        artifact.import_job_id = None
    await db.delete(await db.get(ImportJob, abandoned_child_id))
    parent = await db.get(DownloadJob, parent_id)
    parent.retry_count = 4
    record_unresolved_provider_failure(
        parent,
        kind="nonzero",
        reason="earlier provider failure",
        max_retries=4,
    )
    clear_unresolved_provider_failure(parent)
    parent.retry_count = 0
    parent.status = "downloaded"
    await db.commit()
    assert unresolved_provider_failure(parent) is None

    pending_count, paths, reconciliation = await download._successful_repository_import_plan(
        db,
        parent,
        metadata_count=0,
        metadata_paths=[],
    )
    assert pending_count == 1
    assert len(paths) == 1
    assert paths == {str(_metadata.relative_to(tmp_path / "downloads"))}
    assert reconciliation is not None
    await db.commit()

    async def retain(_download_id, import_id, *_args, **_kwargs):
        return import_id

    monkeypatch.setattr(download, "_publish_import_intent", retain)
    child_id = await download._enqueue_import(str(parent_id), new_json_paths=set(paths))
    await db.rollback()
    assigned_metadata = (await db.execute(select(StorageArtifact).where(
        StorageArtifact.artifact_type == "metadata_json"
    ).execution_options(populate_existing=True))).scalar_one()
    assert assigned_metadata.import_job_id == UUID(str(child_id))
    await import_runner.run_import_job(str(child_id))

    await db.rollback()
    parent = await db.get(DownloadJob, parent_id, populate_existing=True)
    imports = list((await db.execute(select(ImportJob).where(
        ImportJob.download_job_id == parent_id
    ))).scalars())
    assert parent.status == "complete"
    assert len(imports) == 1 and imports[0].status == "complete"
    assert len((await db.execute(select(Work))).scalars().all()) == 1
    assert len((await db.execute(select(Asset))).scalars().all()) == 1
    metadata_rows = list((await db.execute(select(StorageArtifact).where(
        StorageArtifact.artifact_type == "metadata_json"
    ))).scalars())
    assert len(metadata_rows) == 1


@pytest.mark.parametrize("missing", [False, True])
async def test_empty_or_corrupt_metadata_without_completed_checkpoint_still_fails(db, tmp_path, monkeypatch, missing):
    from app.jobs import import_runner
    from app.models import DownloadJob, ImportJob, Work
    parent_id, child_id, metadata = await _import_fixture(db, tmp_path, monkeypatch, corrupt=True)
    if missing:
        metadata.unlink()
    await import_runner.run_import_job(str(child_id))
    await db.rollback()
    child = await db.get(ImportJob, child_id, populate_existing=True)
    assert child.status == "failed"
    assert "Could not extract work IDs" in child.error_log
    assert (await db.get(DownloadJob, parent_id, populate_existing=True)).status == "failed"
    assert not (await db.execute(select(Work))).scalars().all()


async def test_done_artifact_rows_without_domain_evidence_do_not_hide_missing_input(db, tmp_path, monkeypatch):
    from app.jobs import import_runner
    from app.models import ImportJob, StorageArtifact
    _, child_id, metadata = await _import_fixture(db, tmp_path, monkeypatch)
    metadata.unlink()
    child = await db.get(ImportJob, child_id)
    child.execution_attempt = 1
    child.import_retry_count = 1
    for artifact in (await db.execute(select(StorageArtifact))).scalars():
        artifact.state = "done"
    await db.commit()
    await import_runner.run_import_job(str(child_id))
    await db.rollback()
    assert (await db.get(ImportJob, child_id, populate_existing=True)).status == "failed"


async def test_late_orm_write_cannot_reintroduce_index_to_task_lock_order(db):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from app.models import TaskRun, SearchIndexState, SearchProjectionOutbox
    from app.services.search_projection_outbox import request_search_projection
    await request_search_projection(db, subscription_ids=[uuid4()])
    def late_writer(session):
        session.add(TaskRun(kind="admin", status="complete"))
    event.listen(Session, "before_commit", late_writer)
    try:
        with pytest.raises(RuntimeError, match="Domain writes must flush before search generation locks"):
            await db.commit()
    finally:
        event.remove(Session, "before_commit", late_writer)
        await db.rollback()
    assert not (await db.execute(select(SearchIndexState))).scalars().all()
    assert not (await db.execute(select(SearchProjectionOutbox))).scalars().all()


async def test_queued_legacy_child_cannot_claim_another_imports_new_content(db, tmp_path, monkeypatch):
    from app.jobs import import_runner
    from app.models import DownloadJob, ImportJob, StorageArtifact, TaskRun, WorkSource
    from app.services.tasks import TaskService
    from app.services.import_completion_checkpoint import CHECKPOINT_KEY
    parent_id, child_id, metadata = await _import_fixture(db, tmp_path, monkeypatch)
    original_bytes = {path: path.read_bytes() for path in metadata.parent.iterdir()}
    original_parent = await db.get(DownloadJob, parent_id)
    other_parent = DownloadJob(subscription_id=original_parent.subscription_id, subscription_source_id=original_parent.subscription_source_id,
                               source="pixiv", source_url=original_parent.source_url, status="importing")
    db.add(other_parent)
    await db.flush()
    other_child = ImportJob(download_job_id=other_parent.id, status="enqueued")
    db.add(other_child)
    await db.flush()
    other_id = other_child.id
    await TaskService(db).ensure_download_task(other_parent)
    await TaskService(db).ensure_import_task(other_child)
    for row in (await db.execute(select(StorageArtifact).where(StorageArtifact.storage_root == "downloads"))).scalars():
        row.download_job_id, row.import_job_id = other_parent.id, other_id
    await db.commit()
    await import_runner.run_import_job(str(other_id))
    await db.rollback()
    assert (await db.get(ImportJob, other_id, populate_existing=True)).status == "complete"
    child = await db.get(ImportJob, child_id, populate_existing=True)
    source = (await db.execute(select(WorkSource))).scalar_one()
    assert source.created_at >= child.created_at  # Chronology is deliberately misleading.
    for row in (await db.execute(select(StorageArtifact).where(StorageArtifact.storage_root == "downloads").execution_options(populate_existing=True))).scalars():
        row.download_job_id, row.import_job_id, row.state = parent_id, child_id, "new"
    for path, contents in original_bytes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    await db.commit()
    async def abort_terminal(*args, **kwargs):
        assert kwargs["status"] == "complete"
        raise RuntimeError("terminal abort after actual existing-content path")
    monkeypatch.setattr(import_runner, "finalize_download_job", abort_terminal)
    await import_runner.run_import_job(str(child_id))
    await db.rollback()
    task = (await db.execute(select(TaskRun).where(TaskRun.subject_id == child_id))).scalar_one()
    exact = task.meta[CHECKPOINT_KEY]["stats"]
    assert exact["existing"] == 1 and exact["works"] == exact["assets"] == 0
    task.meta = {key: value for key, value in task.meta.items() if key != CHECKPOINT_KEY}
    await db.commit()
    metadata.unlink(missing_ok=True)
    # If the fallback invents a completion, this records what it tried to claim.
    claimed = []
    async def record_terminal(*args, **kwargs):
        claimed.append(kwargs)
    monkeypatch.setattr(import_runner, "_complete_import_execution", record_terminal)
    await import_runner.run_import_job(str(child_id))
    await db.rollback()
    assert not claimed, "Timestamp-only evidence must not reach the terminal success writer"
    child = await db.get(ImportJob, child_id, populate_existing=True)
    assert child.status == "failed"
    assert "import_completion_evidence_missing" in child.error_log
    task = (await db.execute(select(TaskRun).where(TaskRun.subject_id == child_id).execution_options(populate_existing=True))).scalar_one()
    assert task.reason_code == "import_completion_evidence_missing"
    assert task.result_data["status"] == "unresolved"


@pytest.mark.parametrize("mutation", ["reparent_work", "swap_assets", "assigned_media"])
async def test_checkpoint_rejects_changed_relations_with_same_identity_sets(db, tmp_path, monkeypatch, mutation):
    from app.jobs import import_runner
    from app.models import ImportJob, Work, WorkSource, Asset, AssetSource, StorageArtifact
    from app.services.import_completion_checkpoint import load_import_completion_checkpoint
    _, child_id, _ = await _import_fixture(db, tmp_path, monkeypatch, number=2)
    await import_runner.run_import_job(str(child_id))
    await db.rollback()
    child = await db.get(ImportJob, child_id, populate_existing=True)
    assert child.status == "complete"
    child.execution_attempt = 2
    await db.commit()
    assert await load_import_completion_checkpoint(db, child) is not None
    sources = list((await db.execute(select(WorkSource).order_by(WorkSource.source_work_id))).scalars())
    links = list((await db.execute(select(AssetSource).order_by(AssetSource.source_asset_id))).scalars())
    previous_ids = {"sources": {row.id for row in sources}, "assets": set((await db.execute(select(Asset.id))).scalars())}
    if mutation == "reparent_work":
        replacement = Work(title="Unrelated existing work")
        db.add(replacement)
        await db.flush()
        sources[0].work_id = replacement.id
    elif mutation == "swap_assets":
        links[0].asset_id, links[1].asset_id = links[1].asset_id, links[0].asset_id
    else:
        media = (await db.execute(select(StorageArtifact).where(StorageArtifact.storage_root == "downloads", StorageArtifact.artifact_type == "image").order_by(StorageArtifact.source_work_id))).scalars().first()
        media.file_name = "different_unassigned_media.jpg"
    await db.commit()
    assert {row.id for row in (await db.execute(select(WorkSource))).scalars()} == previous_ids["sources"]
    assert set((await db.execute(select(Asset.id))).scalars()) == previous_ids["assets"]
    with pytest.raises(RuntimeError, match="import_completion_identity_mismatch"):
        await load_import_completion_checkpoint(db, child)
