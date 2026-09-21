"""Static contracts for the durable full-library dedup scan coordinator."""

import inspect
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, func, select


def test_asset_scan_rq_job_runs_only_one_nonblocking_resource_slice():
    from app.jobs.asset_dedup import _run_scan

    source = inspect.getsource(_run_scan)
    assert "while True" not in source
    assert "wait_for_capacity=False" in source
    assert "cooldown_result=cooldown" in source
    assert ".with_for_update()" in source


def test_asset_scan_successor_is_delayed_unique_and_generation_fenced():
    from app.jobs.asset_dedup import (
        _enqueue_scan_successor,
        _reserve_scan_successor,
    )

    reserve_source = inspect.getsource(_reserve_scan_successor)
    enqueue_source = inspect.getsource(_enqueue_scan_successor)
    assert 'scan_options["_rq_generation"] = next_generation' in reserve_source
    assert "asset-dedup-scan-" in reserve_source
    assert "checked_enqueue_in" in enqueue_source
    assert "ASSET_DEDUP_SCAN_QUEUE" in enqueue_source
    assert "job_id=rq_job_id" in enqueue_source


def test_asset_scan_terminal_paths_release_only_the_owned_operation_lock():
    from app.jobs.asset_dedup import (
        _complete_scan_operation,
        _fail_scan_operation,
    )

    combined = "\n".join(
        (
            inspect.getsource(_complete_scan_operation),
            inspect.getsource(_fail_scan_operation),
        )
    )
    assert "release_owned_operation_lock" in combined
    assert "ASSET_DEDUP_SCAN_OPERATION_LOCK" in combined


@pytest.mark.asyncio
async def test_latest_asset_scan_operation_uses_durable_scope(monkeypatch):
    from app.api.admin import dedup as dedup_api

    calls = []

    async def fake_latest(_db, **kwargs):
        calls.append(kwargs)
        return {"snapshot": None, "current": None}

    monkeypatch.setattr(
        "app.services.operations.latest_successful_admin_operation",
        fake_latest,
    )

    result = await dedup_api.latest_asset_dedup_scan(db=object())

    assert result == {"snapshot": None, "current": None}
    assert calls == [{
        "operation_type": "asset-dedup-scan",
        "scope_key": "lock:admin:asset-dedup-scan",
        "include_retryable": True,
    }]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_latest_asset_scan_restores_retryable_failure():
    from app.database import async_session, engine
    from app.models import TaskEvent, TaskRun
    from app.services import operations

    try:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="asset-dedup-scan",
                scope_key="lock:admin:asset-dedup-scan",
                title="Failed asset dedup scan",
                entity="assets",
                options={"scan_id": "failed"},
                queue_name="maintenance",
                job_timeout=60,
            )
            prepared.task.status = "failed"
            prepared.task.error_log = "fixture failure"
            prepared.task.finished_at = datetime.now(timezone.utc)
            await db.commit()

            state = await operations.latest_successful_admin_operation(
                db,
                operation_type="asset-dedup-scan",
                scope_key="lock:admin:asset-dedup-scan",
                include_retryable=True,
            )

            assert state["snapshot"] is None
            assert state["current"]["task_id"] == str(prepared.task.id)
            assert state["current"]["status"] == "failed"
    finally:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_asset_scan_and_prepared_task_are_one_admission_transaction():
    """A single-flight conflict cannot leave an orphaned AssetDedupScan."""
    from fastapi import HTTPException

    from app.api.admin import dedup as dedup_api
    from app.database import async_session, engine
    from app.models import AssetDedupScan, TaskEvent, TaskRun
    from app.schemas.asset_dedup import AssetDedupScanRequest
    from app.services import operations

    baseline_ids = set()
    try:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            baseline = int(
                (await db.execute(select(func.count()).select_from(AssetDedupScan)))
                .scalar_one()
            )
            baseline_ids = set(
                (await db.execute(select(AssetDedupScan.id))).scalars()
            )
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="asset-dedup-scan",
                scope_key="lock:admin:asset-dedup-scan",
                title="Existing asset dedup scan",
                entity="assets",
                options={"scan_id": "existing"},
                queue_name="maintenance",
                job_timeout=60,
            )
            await db.commit()

        async with async_session() as request_db:
            with pytest.raises(HTTPException) as raised:
                await dedup_api.start_asset_dedup_scan(
                    AssetDedupScanRequest(auto_apply=False, batch_size=100),
                    request_db,
                )
            assert raised.value.status_code == 409

        async with async_session() as verify_db:
            count = int(
                (
                    await verify_db.execute(
                        select(func.count()).select_from(AssetDedupScan)
                    )
                ).scalar_one()
            )
            assert count == baseline
            task = await verify_db.get(TaskRun, prepared.task.id)
            assert task.status == "enqueued"
    finally:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            if baseline_ids:
                await db.execute(
                    delete(AssetDedupScan).where(
                        AssetDedupScan.id.not_in(baseline_ids)
                    )
                )
            else:
                await db.execute(delete(AssetDedupScan))
            await db.commit()
        await engine.dispose()
