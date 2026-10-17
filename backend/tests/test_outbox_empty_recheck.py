"""Empty/future outboxes retain a bounded continuation after owner release."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tests.test_bulk_import_wake import bulk_environment
from tests.test_resource_release_wake import waiting_work


@pytest.fixture
async def empty_completion(waiting_work, monkeypatch):
    from rq import Queue
    from rq.job import Job
    from rq.registry import ScheduledJobRegistry
    from app.services import outbox_coordinator as coordinator

    w = waiting_work
    from app.models import AssetDedupOutbox
    await w.db.execute(delete(AssetDedupOutbox).where(
        AssetDedupOutbox.idempotency_key.startswith("empty-recheck-")))
    await w.db.commit()
    event_ids = []
    monkeypatch.setattr(coordinator, "wake_pending_outboxes", w.real_wake)
    queue = Queue("operations", connection=w.client)
    registry = ScheduledJobRegistry("operations", connection=w.client)
    before = set(queue.job_ids) | set(registry.get_job_ids())
    owner = Job.create("app.jobs.asset_dedup.run_asset_dedup_outbox",
        connection=w.client, origin="operations", id="empty-owner-"+uuid4().hex)
    owner.save()
    key = coordinator._wake_key("dedup")
    w.client.set(key, owner.id)
    monkeypatch.setattr("rq.get_current_job", lambda: owner)
    try:
        yield SimpleNamespace(work=w, queue=queue, registry=registry, owner=owner, key=key, event_ids=event_ids)
    finally:
        for job_id in (set(queue.job_ids) | set(registry.get_job_ids())) - before:
            registry.remove(job_id)
            queue.remove(job_id)
            try:
                Job.fetch(job_id, connection=w.client).delete()
            except Exception:
                pass
        w.client.delete(key)
        owner.delete()
        if event_ids:
            await w.db.execute(delete(AssetDedupOutbox).where(AssetDedupOutbox.id.in_(event_ids)))
            await w.db.commit()


async def stage_event(context, seconds, state="pending"):
    from app.models import AssetDedupOutbox
    identity = uuid4()
    context.event_ids.append(identity)
    due = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    context.work.db.add(AssetDedupOutbox(id=identity, idempotency_key="empty-recheck-"+str(identity),
        event_type="observe", payload={"asset_id":str(context.work.staged["derivative_requests"][0]["asset_id"])},
        state=state, available_at=due))
    await context.work.db.commit()
    return identity


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("seconds,state,expected", [(10,"pending",1),(-10,"pending",1),(300,"pending",0),(10,"complete",0)])
async def test_empty_slice_rechecks_after_clear_and_schedules_near_work(empty_completion, monkeypatch, seconds, state, expected):
    from app.models import AssetDedupOutbox
    from app.services import outbox_coordinator as coordinator

    c = empty_completion
    identity = await stage_event(c, seconds, state)
    real = coordinator._deferred_outbox_next_delays
    checked = []
    async def check(kinds):
        assert c.work.client.get(c.key) is None, "Read must happen after owner release"
        checked.append(True)
        return await real(kinds)
    monkeypatch.setattr(coordinator, "_deferred_outbox_next_delays", check)
    result = await asyncio.to_thread(coordinator.clear_and_wake_outbox_successor,
        "dedup", {"processed":0,"more_likely":False,"resource_deferred":False})
    assert checked
    assert result == {"enqueued":expected,"deferred":0}
    assert c.queue.count == 0
    assert len(c.registry.get_job_ids()) == expected
    if expected:
        assert c.work.client.get(c.key).decode() == c.registry.get_job_ids()[0]
    row = (await c.work.db.execute(select(AssetDedupOutbox.state, AssetDedupOutbox.attempts)
        .where(AssetDedupOutbox.id == identity))).one()
    assert tuple(row) == (state, 0), "Scheduling must not claim or finish the event"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_future_resource_wait_keeps_scheduled_wake(empty_completion, monkeypatch):
    from app.services import outbox_coordinator as coordinator

    c = empty_completion
    await stage_event(c, 10)
    c.work.client.delete(c.key)
    c.work.client.hset(coordinator.RESOURCE_DEFERRED_KEY, "dedup", "waiting")
    result = await asyncio.to_thread(coordinator.wake_resource_deferred_outboxes)
    assert result == {"enqueued":1,"deferred":0}
    assert c.queue.count == 0
    assert len(c.registry.get_job_ids()) == 1
    assert not c.work.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "dedup")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_recheck_timeout_preserves_durable_work(empty_completion, monkeypatch):
    import time
    from app.models import AssetDedupOutbox
    from app.services import outbox_coordinator as coordinator

    c = empty_completion
    identity = await stage_event(c, -10)
    async def slow(*args):
        await asyncio.sleep(1)
        pytest.fail("Timed-out lookup kept running")
    monkeypatch.setattr(coordinator, "_deferred_outbox_next_delays", slow)
    started = time.monotonic()
    result = await asyncio.to_thread(coordinator.clear_and_wake_outbox_successor,
        "dedup", {"processed":0,"more_likely":False})
    assert time.monotonic() - started < 1
    assert result == {"enqueued":0,"deferred":1}
    assert not c.work.client.get(c.key)
    assert c.queue.count == c.registry.count == 0
    assert await c.work.db.scalar(select(AssetDedupOutbox.state).where(AssetDedupOutbox.id == identity)) == "pending"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_checkpoint_keeps_its_actual_future_deadline(empty_completion, monkeypatch):
    from app.models.repository_sync_receipt import SearchIndexState
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services import outbox_coordinator as coordinator, search_rebuild
    from sqlalchemy import literal
    c=empty_completion
    monkeypatch.setattr(search_rebuild,"membership_bootstrap_due_condition",lambda:literal(False))
    await c.work.db.execute(delete(SearchProjectionOutbox))
    await c.work.db.commit()
    uid="empty-recheck-"+uuid4().hex
    now=datetime.now(timezone.utc)
    row=SearchIndexState(index_uid=uid,database_generation=2,indexed_generation=1,
        last_verified_at=now-timedelta(seconds=20))
    c.work.db.add(row)
    await c.work.db.commit()
    try:
        due=await c.work.db.scalar(coordinator._next_outbox_deadline_statement("search",now))
        assert due==now+timedelta(seconds=10)
        row.indexed_generation=2
        await c.work.db.commit()
        assert await c.work.db.scalar(coordinator._next_outbox_deadline_statement("search",now)) is None
    finally:
        await c.work.db.execute(delete(SearchIndexState).where(SearchIndexState.index_uid==uid))
        await c.work.db.commit()
