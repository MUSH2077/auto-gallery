"""PostgreSQL regressions for concurrent import execution ownership."""

from __future__ import annotations

import asyncio
import multiprocessing
from datetime import datetime, timedelta, timezone
from queue import Empty
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def db():
    from app.database import async_session, engine

    async with async_session() as session:
        await session.execute(text(
            "TRUNCATE task_events, task_runs, storage_artifacts, import_jobs, "
            "download_jobs, subscription_sources, subscriptions, creators, "
            "work_sources, works, assets, import_curation_outbox, "
            "media_derivative_outbox RESTART IDENTITY CASCADE"
        ))
        await session.commit()
        yield session
        await session.rollback()
    await engine.dispose()


async def _new_import_job(db, *, source: str = "danbooru"):
    from app.models import Creator, DownloadJob, ImportJob, Subscription, SubscriptionSource

    creator = Creator(name=f"parallel-import-{uuid4()}")
    db.add(creator)
    await db.flush()
    subscription = Subscription(
        creator_id=creator.id,
        name="parallel import regression",
        schedule_mode="manual",
        sync_enabled=False,
    )
    db.add(subscription)
    await db.flush()
    source_row = SubscriptionSource(
        subscription_id=subscription.id,
        source=source,
        source_creator_id="parallel-test",
        source_url=f"https://example.test/{source}/{uuid4()}",
    )
    db.add(source_row)
    await db.flush()
    parent = DownloadJob(
        subscription_id=subscription.id,
        subscription_source_id=source_row.id,
        source=source,
        source_url=source_row.source_url,
        status="importing",
    )
    db.add(parent)
    await db.flush()
    child = ImportJob(download_job_id=parent.id, status="enqueued")
    db.add(child)
    await db.flush()
    return parent, child


async def _dispose_inherited_engine():
    from app.database import engine

    await engine.dispose()


async def _wait_for_claim_start(start_barrier):
    from app.database import async_session

    # Finish process imports and establish each process's own connection before
    # releasing the contenders. No SQL transaction is held at the barrier.
    async with async_session() as session:
        await session.execute(text("SELECT 1"))
    start_barrier.wait(timeout=10)


def _claim_import_worker(job_id: str, start_barrier, result_queue) -> None:
    async def run():
        await _dispose_inherited_engine()
        from app.jobs.import_execution import _claim_import_execution

        await _wait_for_claim_start(start_barrier)
        claimed = await _claim_import_execution(UUID(job_id))
        if claimed is None:
            return {"claimed": False}
        _, token, _queue_wait = claimed
        return {"claimed": True, "token": str(token)}

    try:
        result_queue.put({"ok": True, **asyncio.run(run())})
    except BaseException as exc:  # pragma: no cover
        result_queue.put({"ok": False, "error": repr(exc)})


def _claim_artifact_worker(
    download_job_id: str,
    import_job_id: str,
    lease_token: str,
    source: str,
    source_work_id: str,
    start_barrier,
    result_queue,
) -> None:
    async def run():
        await _dispose_inherited_engine()
        from app.database import async_session
        from app.services.artifact_ledger import ArtifactLedger

        await _wait_for_claim_start(start_barrier)
        async with async_session() as session:
            result = await ArtifactLedger(session).claim_work_batch(
                UUID(download_job_id), UUID(import_job_id), lease_token=UUID(lease_token),
                source=source, source_work_ids=[source_work_id], limit=1,
                lease_seconds=30,
            )
            await session.commit()
            return {
                "claimed": list(result.claimed),
                "contended": list(result.contended),
                "existing": list(result.existing),
            }

    try:
        result_queue.put({"ok": True, **asyncio.run(run())})
    except BaseException as exc:  # pragma: no cover
        result_queue.put({"ok": False, "error": repr(exc)})


def _stale_execution_worker(
    download_job_id: str,
    import_job_id: str,
    lease_token: str,
    source_work_id: str,
    start_barrier,
    result_queue,
) -> None:
    async def run():
        await _dispose_inherited_engine()
        from app.database import async_session
        from app.services.artifact_ledger import ArtifactLedger

        await _wait_for_claim_start(start_barrier)
        async with async_session() as session:
            ledger = ArtifactLedger(session)
            marked = await ledger.mark_work_results(
                UUID(download_job_id), {source_work_id: ("done", None)},
                expected_import_job_id=UUID(import_job_id),
                expected_lease_token=UUID(lease_token),
            )
            released = await ledger.release_owned_leases(
                UUID(import_job_id), UUID(lease_token)
            )
            await session.commit()
            return {"marked": sorted(marked), "released": sorted(released)}

    try:
        result_queue.put({"ok": True, **asyncio.run(run())})
    except BaseException as exc:  # pragma: no cover
        result_queue.put({"ok": False, "error": repr(exc)})


def _run_workers(target, args_list, *, timeout: float = 12.0):
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    start_barrier = context.Barrier(len(args_list))
    processes = [
        context.Process(target=target, args=(*args, start_barrier, result_queue))
        for args in args_list
    ]
    try:
        for process in processes:
            process.start()
        results = []
        for process in processes:
            process.join(timeout)
            assert not process.is_alive(), "import worker did not finish"
            assert process.exitcode == 0
            try:
                results.append(result_queue.get(timeout=2))
            except Empty as exc:  # pragma: no cover
                raise AssertionError("import worker returned no result") from exc
        assert all(result.get("ok") for result in results), results
        return results
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        result_queue.close()
        result_queue.join_thread()


async def _add_metadata_artifact(
    db, parent, child, *, source, source_work_id, state="new", expires=None, token=None
):
    from app.models import StorageArtifact

    artifact = StorageArtifact(
        storage_root="downloads",
        file_path=f"{source}/parallel/{source_work_id}/{source_work_id}.json",
        source=source,
        creator_dir="parallel",
        source_work_id=source_work_id,
        file_name=f"{source_work_id}.json",
        artifact_type="metadata_json",
        file_size=128,
        mtime_ns=1,
        download_job_id=parent.id,
        import_job_id=child.id,
        state=state,
        lease_token=token,
        lease_expires_at=expires,
    )
    db.add(artifact)
    await db.flush()
    return artifact


async def test_two_deliveries_claim_one_import_execution(db):
    from app.models import ImportJob

    _parent, child = await _new_import_job(db)
    await db.commit()
    results = _run_workers(
        _claim_import_worker, [(str(child.id),), (str(child.id),)]
    )
    assert [result["claimed"] for result in results].count(True) == 1
    assert [result["claimed"] for result in results].count(False) == 1
    current = await db.get(ImportJob, child.id, populate_existing=True)
    assert current.status == "running"
    assert current.execution_token is not None
    await db.rollback()


async def test_same_source_work_is_contended_then_existing(db):
    from app.models import Work, WorkSource

    source = "danbooru"
    source_work_id = "parallel-work-1"
    parent_a, child_a = await _new_import_job(db, source=source)
    parent_b, child_b = await _new_import_job(db, source=source)
    token_a, token_b = uuid4(), uuid4()
    child_a.status = child_b.status = "running"
    child_a.execution_token, child_b.execution_token = token_a, token_b
    await _add_metadata_artifact(
        db, parent_a, child_a, source=source, source_work_id=source_work_id
    )
    await db.commit()
    results = _run_workers(
        _claim_artifact_worker,
        [
            (str(parent_a.id), str(child_a.id), str(token_a), source, source_work_id),
            (str(parent_b.id), str(child_b.id), str(token_b), source, source_work_id),
        ],
    )
    assert sum(bool(result["claimed"]) for result in results) == 1
    assert sum(bool(result["contended"]) for result in results) == 1
    assert all(not result["existing"] for result in results)

    work = Work(title="completed concurrent work")
    db.add(work)
    await db.flush()
    db.add(WorkSource(source=source, source_work_id=source_work_id, work_id=work.id))
    await db.commit()
    retry = _run_workers(
        _claim_artifact_worker,
        [(str(parent_b.id), str(child_b.id), str(token_b), source, source_work_id)],
    )[0]
    assert retry["claimed"] == []
    assert retry["contended"] == []
    assert retry["existing"] == [source_work_id]


async def test_expired_lease_takeover_fences_stale_checkpoint_and_release(db):
    from app.models import StorageArtifact

    parent, child = await _new_import_job(db)
    old_token, new_token = uuid4(), uuid4()
    child.status = "running"
    child.execution_token = new_token
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    await _add_metadata_artifact(
        db, parent, child, source="danbooru", source_work_id="expired-work",
        state="importing", expires=expired, token=old_token,
    )
    await db.commit()
    claimed = _run_workers(
        _claim_artifact_worker,
        [(str(parent.id), str(child.id), str(new_token), "danbooru", "expired-work")],
    )[0]
    assert claimed["claimed"] == ["expired-work"]
    stale = _run_workers(
        _stale_execution_worker,
        [(str(parent.id), str(child.id), str(old_token), "expired-work")],
    )[0]
    assert stale == {"ok": True, "marked": [], "released": []}
    artifact = (
        await db.execute(select(StorageArtifact).where(
            StorageArtifact.source_work_id == "expired-work"
        ))
    ).scalar_one()
    assert artifact.state == "importing"
    assert artifact.lease_token == new_token
    await db.rollback()


async def test_claim_rollback_does_not_leave_a_second_executable_lease(db):
    from app.models import StorageArtifact
    from app.services.artifact_ledger import ArtifactLedger

    parent, child = await _new_import_job(db)
    parent_id, child_id = parent.id, child.id
    token = uuid4()
    await _add_metadata_artifact(
        db, parent, child, source="danbooru", source_work_id="rollback-work"
    )
    child.status = "running"
    child.execution_token = token
    await db.commit()
    first = await ArtifactLedger(db).claim_work_batch(
        parent_id, child_id, lease_token=token, source="danbooru",
        source_work_ids=["rollback-work"], limit=1,
    )
    assert first.claimed == ("rollback-work",)
    await db.rollback()
    row = (
        await db.execute(select(StorageArtifact).where(
            StorageArtifact.source_work_id == "rollback-work"
        ))
    ).scalar_one()
    assert row.state == "new"
    second = await ArtifactLedger(db).claim_work_batch(
        parent_id, child_id, lease_token=token, source="danbooru",
        source_work_ids=["rollback-work"], limit=1,
    )
    assert second.claimed == ("rollback-work",)
    await db.rollback()
