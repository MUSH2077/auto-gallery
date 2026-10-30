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
    # A shared queue may already contain work of another kind.
    unrelated = queue.enqueue("app.jobs.media_derivatives.run_media_derivative_outbox",
        job_id="empty-recheck-unrelated-"+uuid4().hex)
    queued_before = set(queue.job_ids)
    scheduled_before = set(registry.get_job_ids())
    before = queued_before | scheduled_before
    owner = Job.create("app.jobs.asset_dedup.run_asset_dedup_outbox",
        connection=w.client, origin="operations", id="empty-owner-"+uuid4().hex)
    owner.save()
    key = coordinator._wake_key("dedup")
    w.client.set(key, owner.id)
    monkeypatch.setattr("rq.get_current_job", lambda: owner)
    try:
        yield SimpleNamespace(work=w, queue=queue, registry=registry, owner=owner, key=key, event_ids=event_ids,
            queued_before=queued_before, scheduled_before=scheduled_before)
    finally:
        for job_id in (set(queue.job_ids) | set(registry.get_job_ids())) - before:
            registry.remove(job_id)
            queue.remove(job_id)
            try:
                Job.fetch(job_id, connection=w.client).delete()
            except Exception:
                pass
        queue.remove(unrelated.id)
        unrelated.delete()
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
    assert set(c.queue.job_ids) == c.queued_before
    scheduled = set(c.registry.get_job_ids())
    assert c.scheduled_before <= scheduled
    new_scheduled = scheduled - c.scheduled_before
    assert len(new_scheduled) == expected
    if expected:
        assert c.work.client.get(c.key).decode() in new_scheduled
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
    assert set(c.queue.job_ids) == c.queued_before
    scheduled = set(c.registry.get_job_ids())
    assert c.scheduled_before <= scheduled
    assert len(scheduled - c.scheduled_before) == 1
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
    assert set(c.queue.job_ids) == c.queued_before
    assert set(c.registry.get_job_ids()) == c.scheduled_before
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


from tests.test_postgres_search_integration import pgdb


@pytest.fixture
async def completed_search_recheck(pgdb, monkeypatch):
    from rq import Queue
    from rq.job import Job
    from rq.registry import ScheduledJobRegistry
    from sqlalchemy import false, update
    from app.models.repository_sync_receipt import SearchIndexState
    from app.services import outbox_coordinator as coordinator, search_rebuild
    from app.services.redis_client import get_redis
    client = get_redis()
    monkeypatch.setattr(search_rebuild, "membership_bootstrap_due_condition", lambda: false())
    await pgdb.execute(update(SearchIndexState).values(indexed_generation=SearchIndexState.database_generation))
    await pgdb.commit()
    queue = Queue("maintenance", connection=client)
    registry = ScheduledJobRegistry("maintenance", connection=client)
    before = set(queue.job_ids) | set(registry.get_job_ids())
    owner = Job.create("app.jobs.search_projection.run_search_projection_outbox",
        connection=client, origin="maintenance", id="completed-search-"+uuid4().hex)
    owner.save()
    key = coordinator._wake_key("search")
    client.set(key, owner.id)
    monkeypatch.setattr("rq.get_current_job", lambda: owner)
    try:
        yield SimpleNamespace(db=pgdb, client=client, queue=queue, registry=registry, key=key, owner=owner, before=before)
    finally:
        for identity in (set(queue.job_ids) | set(registry.get_job_ids())) - before:
            registry.remove(identity)
            queue.remove(identity)
            Job.fetch(identity, connection=client).delete()
        client.delete(key)
        owner.delete()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completed_search_clear_first_catches_concurrent_ready_without_one_second_delay(
    completed_search_recheck, monkeypatch,
):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services.search_index_definitions import WORKS_INDEX
    from app.services import outbox_coordinator as coordinator
    c = completed_search_recheck
    factory = async_sessionmaker(c.db.bind, expire_on_commit=False)
    real_clear = coordinator.clear_outbox_wake
    async def concurrent_commit():
        async with factory() as db:
            db.add(SearchProjectionOutbox(index_uid=WORKS_INDEX, entity_id=str(uuid4()),
                action="upsert", version=1, available_at=datetime.now(timezone.utc)))
            await db.commit()
    def release_then_commit(kind):
        released = real_clear(kind)
        if released:
            assert c.client.get(c.key) is None
            asyncio.run(concurrent_commit())
        return released
    monkeypatch.setattr(coordinator, "clear_outbox_wake", release_then_commit)
    result = await asyncio.to_thread(coordinator.clear_and_wake_outbox_successor,
        "search", {"status":"ok", "processed":1, "more_likely":False,
                   "database_receipt_committed":True})
    assert result == {"enqueued":1,"deferred":0}
    new = set(c.queue.job_ids) - c.before
    assert len(new) == 1
    assert not (set(c.registry.get_job_ids()) - c.before)
    assert c.client.get(c.key).decode() in new
    # The old job must not clear or duplicate the new owner's wake.
    again = await asyncio.to_thread(coordinator.clear_and_wake_outbox_successor,
        "search", {"status":"ok", "processed":1, "more_likely":False,
                   "database_receipt_committed":True})
    assert again == {"enqueued":0,"deferred":0}
    assert set(c.queue.job_ids) - c.before == new


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("future_kind", ["outbox", "receipt"])
async def test_completed_search_recheck_keeps_future_business_deadline(
    completed_search_recheck, future_kind,
):
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.services.search_index_definitions import WORKS_INDEX
    from app.services import outbox_coordinator as coordinator
    c=completed_search_recheck
    due=datetime.now(timezone.utc)+timedelta(seconds=10)
    if future_kind == "outbox":
        row=SearchProjectionOutbox(index_uid=WORKS_INDEX, entity_id=str(uuid4()),
            action="upsert", version=1, available_at=due)
    else:
        row=SearchDeliveryReceipt(backend="postgresql", index_uid=WORKS_INDEX,
            action="upsert", state="prepared", phase="documents", payload=[],
            versions=[], available_at=due)
    c.db.add(row)
    await c.db.commit()
    result=await asyncio.to_thread(coordinator.clear_and_wake_outbox_successor,
        "search", {"status":"ok", "processed":1, "more_likely":False,
                   "database_receipt_committed":True})
    assert result == {"enqueued":1,"deferred":0}
    assert not (set(c.queue.job_ids)-c.before)
    scheduled=set(c.registry.get_job_ids())-c.before
    assert len(scheduled)==1
    identity=next(iter(scheduled))
    score=c.client.zscore(c.registry.key,identity)
    assert due.timestamp()-2 <= score <= due.timestamp()+2
