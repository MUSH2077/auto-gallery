"""Release retries preserve admission, ownership and delayed continuations."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from tests.test_bulk_import_wake import bulk_environment, _persist, _staged_work


@pytest.fixture
async def waiting_work(bulk_environment, monkeypatch):
    from app.services import heavy_io, outbox_coordinator, resource_pressure
    from app.services.redis_client import get_redis

    db, library = bulk_environment
    staged = await _staged_work(db, library)
    with monkeypatch.context() as setup:
        setup.setattr(outbox_coordinator, "wake_pending_outboxes", lambda *_args, **_kwargs: None)
        await _persist(db, [staged])
        await db.commit()
    root = library.parent / "locks" / "heavy-io.lock"
    monkeypatch.setattr(heavy_io, "_local_lock_path", lambda: root)
    snapshot = {
        "status": "normal", "controller_mode": "normal",
        "budget": {"governance_mode": "shadow", "profiles": {}},
        "memory": {"available_bytes": 1024 ** 3},
        "controller": {"hard_limits": {"memory_available_bytes": 256 * 1024 ** 2}},
    }

    async def pressure():
        return snapshot

    monkeypatch.setattr(heavy_io, "_pressure_snapshot", pressure)
    monkeypatch.setattr(outbox_coordinator, "_recovery_pressure_snapshot", lambda _client: snapshot)
    monkeypatch.setattr(resource_pressure, "get_resource_pressure_snapshot", pressure)
    monkeypatch.setattr(resource_pressure, "current_memory_available_bytes", lambda: 1024 ** 3)
    client = get_redis()
    published = []
    real_wake = outbox_coordinator.wake_pending_outboxes
    monkeypatch.setattr(outbox_coordinator, "wake_pending_outboxes", lambda counts, **_: published.append(counts) or {"enqueued": len(counts), "deferred": 0})
    client.delete(outbox_coordinator.RESOURCE_DEFERRED_KEY)
    try:
        yield SimpleNamespace(db=db, staged=staged, client=client, snapshot=snapshot, published=published, root=root, real_wake=real_wake)
    finally:
        client.delete(outbox_coordinator.RESOURCE_DEFERRED_KEY)


def _defer(waiting, kind="media", token="waiting"):
    from app.services.outbox_coordinator import RESOURCE_DEFERRED_KEY
    waiting.client.hset(RESOURCE_DEFERRED_KEY, kind, token)


@pytest.mark.integration
def test_release_retries_real_pending_rows_after_posix_holder_exits(waiting_work):
    from app.services import heavy_io, outbox_coordinator
    _defer(waiting_work)
    lock = heavy_io.LocalHeavyIOLock(waiting_work.root.with_name("lane-background.lock"))
    assert lock.try_acquire()
    try:
        assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 0
    finally:
        lock.release()
    assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 1
    assert waiting_work.published[-1] == {"media": 1}


@pytest.mark.integration
@pytest.mark.parametrize("denial", ["paused", "memory", "aggregate"])
def test_release_does_not_loop_under_pressure_or_memory_denial(waiting_work, denial):
    import json
    from app.services import heavy_io, outbox_coordinator
    from app.jobs.import_projection import run_import_projection_outbox
    client = waiting_work.client
    if denial == "paused":
        waiting_work.snapshot["status"] = "paused"
        waiting_work.snapshot["controller_mode"] = "critical"
        waiting_work.snapshot["budget"]["profiles"]["import_db"] = {"allowed": True}
        result = run_import_projection_outbox()
        assert result["resource_deferred"]
    elif denial == "memory":
        waiting_work.snapshot["memory"]["available_bytes"] = 300 * 1024 ** 2
    else:
        client.set(heavy_io.RESOURCE_NETWORK_TOKEN_KEY, json.dumps({
            "owner": "release-test-network", "workload": "download_network",
            "reserved_bytes": 700 * 1024 ** 2,
        }))
    _defer(waiting_work, "import_projection")
    try:
        for _ in range(3):
            assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 0
        assert not waiting_work.published or all(not counts for counts in waiting_work.published)
        assert client.hget(outbox_coordinator.RESOURCE_DEFERRED_KEY, "import_projection")
    finally:
        if denial == "aggregate":
            client.delete(heavy_io.RESOURCE_NETWORK_TOKEN_KEY)
    waiting_work.snapshot["status"] = "normal"
    waiting_work.snapshot["controller_mode"] = "normal"
    waiting_work.snapshot["memory"]["available_bytes"] = 1024 ** 3
    assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 1


@pytest.mark.integration
def test_terminal_search_marker_blocks_retry_until_removed(waiting_work):
    from app.services import outbox_coordinator, remote_search_flight
    _defer(waiting_work)
    remote_search_flight.create_marker("release-test-flight", "search_index")
    try:
        assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 0
        assert not waiting_work.published
    finally:
        remote_search_flight.clear_marker("release-test-flight")
    assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_release_preserves_future_availability(waiting_work):
    from app.models import MediaDerivativeOutbox
    from app.services import outbox_coordinator
    _defer(waiting_work)
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    await waiting_work.db.execute(update(MediaDerivativeOutbox).values(available_at=future))
    await waiting_work.db.commit()
    # The production release callback is synchronous in the RQ parent.
    import asyncio
    assert (await asyncio.to_thread(outbox_coordinator.wake_resource_deferred_outboxes))["enqueued"] == 0
    assert not waiting_work.published


@pytest.mark.integration
def test_release_and_registration_order_both_observe_waiting_work(waiting_work, monkeypatch):
    from app.services import outbox_coordinator
    assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 0
    monkeypatch.setattr("rq.get_current_job", lambda: None)
    outbox_coordinator.clear_and_wake_outbox_successor("media", {"resource_deferred": True})
    # The deferred job's own parent finalizer checks again after registration.
    assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 1


@pytest.mark.integration
def test_multiple_release_notifications_keep_one_rq_owner(waiting_work, monkeypatch):
    from rq import Queue
    from rq.job import Job
    from app.services import outbox_coordinator
    monkeypatch.setattr(outbox_coordinator, "wake_pending_outboxes", waiting_work.real_wake)
    _defer(waiting_work, "import_projection")
    queue = Queue("maintenance", connection=waiting_work.client)
    key = "outbox:wakeup:import_projection"
    assert not waiting_work.client.exists(key)
    job_id = None
    try:
        assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 1
        job_id = waiting_work.client.get(key).decode()
        assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 0
        assert queue.job_ids.count(job_id) == 1
    finally:
        if job_id:
            queue.remove(job_id)
            Job.fetch(job_id, connection=waiting_work.client).delete()
        waiting_work.client.delete(key)


@pytest.mark.integration
def test_failed_release_notification_preserves_completed_job(waiting_work, monkeypatch):
    from rq import Queue, Worker
    from app.services import outbox_coordinator, resource_aware_worker

    released = []
    class Lock:
        def release(self):
            released.append("lock")
    class Lease:
        async def release(self):
            released.append("lease")

    worker = resource_aware_worker.ResourceAwareWorker([Queue("imports", connection=waiting_work.client)], connection=waiting_work.client)
    job = SimpleNamespace(meta={}, refresh=lambda: None)
    monkeypatch.setattr(worker, "_job_workload", lambda *_: "import_db")
    monkeypatch.setattr(worker, "_job_publisher_attempt", lambda *_: None)
    monkeypatch.setattr(worker, "_internal_slice_workload", lambda *_: None)
    monkeypatch.setattr(worker, "_bound_job_slice", lambda *_: None)
    monkeypatch.setattr(worker, "_raise_if_shutdown_requested", lambda: None)
    monkeypatch.setattr(worker, "set_state", lambda *_: None)
    monkeypatch.setattr(worker, "_profile_admission", lambda *_: (Lock(), Lease(), "release-parent-test"))
    monkeypatch.setattr(worker, "_set_job_resource_meta", lambda *_: None)
    monkeypatch.setattr(resource_aware_worker, "_set_resource_state_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(Worker, "execute_job", lambda *_: "domain-completed")
    def failed_notify():
        assert released == ["lock", "lease"]
        raise ConnectionError("release notification unavailable")
    monkeypatch.setattr(outbox_coordinator, "wake_resource_deferred_outboxes", failed_notify)
    assert worker.execute_job(job, worker.queues[0]) == "domain-completed"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("curation_pending", [True, False])
async def test_import_recovery_checks_the_pending_phase_lane(waiting_work, curation_pending):
    import asyncio
    import json
    from sqlalchemy import select
    from app.models import ImportCurationOutbox
    from app.services import heavy_io, outbox_coordinator

    w = waiting_work
    state = "pending" if curation_pending else "complete"
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    await w.db.execute(update(ImportCurationOutbox).values(
        state=state, available_at=due, metadata_state="pending", metadata_available_at=due))
    await w.db.commit()
    w.published.clear()
    _defer(w, "import_projection")
    background = heavy_io.LocalHeavyIOLock(w.root.with_name("lane-background.lock"))
    assert background.try_acquire()
    token = heavy_io.RESOURCE_BACKGROUND_TOKEN_KEY
    assert w.client.set(token, json.dumps({"owner":"phase-recovery-test", "workload":"image_derive",
        "reserved_bytes":32 * 1024 ** 2}), nx=True)
    try:
        result = await asyncio.to_thread(outbox_coordinator.wake_resource_deferred_outboxes)
        assert result["enqueued"] == int(curation_pending)
        assert w.client.exists(token), "Recovery must preserve the active background reservation"
        assert await w.db.scalar(select(ImportCurationOutbox.state)) == state
        if curation_pending:
            assert w.published[-1] == {"import_projection": 1}
        else:
            assert not w.published
    finally:
        w.client.delete(token)
        background.release()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("holder_profile", ["import_db", "image_derive"])
async def test_committed_work_is_curated_while_more_imports_are_queued(waiting_work, holder_profile):
    from sqlalchemy import select
    from app.jobs.import_projection import _run_projection
    from app.models import ImportCurationOutbox

    w = waiting_work
    w.client.rpush("rq:queue:imports", "next-import")
    try:
        from app.services import heavy_io
        async with heavy_io.heavy_io_slot(holder_profile, "active-ingest", lane="ingest"):
            result = await _run_projection(25, 20, {})
            assert w.client.exists(heavy_io.RESOURCE_DISK_TOKEN_KEY)
        assert result["processed"] == 1
        assert result["resource_deferred"]
        row = (await w.db.execute(select(ImportCurationOutbox))).scalar_one()
        assert row.state == "complete"
        assert row.metadata_state == "pending"
        assert w.published[-1].get("search") == 1
        assert w.client.llen("rq:queue:imports") == 1
    finally:
        w.client.delete("rq:queue:imports", "resource:lanes:last-background")


@pytest.mark.integration
def test_curation_release_recovery_uses_free_database_slot_before_queue_drains(waiting_work):
    from app.services import heavy_io, outbox_coordinator

    w = waiting_work
    w.client.rpush("rq:queue:imports", "next-import")
    _defer(w, "import_projection")
    importing = heavy_io.local_lock_for_workload("import_db", lane="completion")
    assert importing.try_acquire()
    try:
        assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 0
        importing.release()
        assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 1
        assert w.published == [{"import_projection": 1}]
        assert w.client.llen("rq:queue:imports") == 1
    finally:
        importing.release()
        w.client.delete("rq:queue:imports", "resource:lanes:last-background")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("ordinary_pg", [True, False])
async def test_search_release_recovery_classifies_pg_before_ingest_priority(waiting_work, monkeypatch, ordinary_pg):
    import asyncio
    from sqlalchemy import delete
    from app.config import settings
    from app.models import ImportCurationOutbox
    from app.models.search_rebuild import SearchRebuild
    from app.services import outbox_coordinator, postgres_search_store, search_rebuild
    from app.services.search_index_definitions import INDEX_SETTINGS, WORKS_INDEX

    w = waiting_work
    monkeypatch.setattr(settings, "search_backend", "postgresql")
    bootstrap = SearchRebuild(backend="postgresql", owner=search_rebuild.MEMBERSHIP_BOOTSTRAP_OWNER,
                              state="complete", phase="complete", progress={})
    w.db.add(bootstrap)
    await w.db.execute(update(ImportCurationOutbox).values(state="complete"))
    await w.db.commit()
    if ordinary_pg:
        await postgres_search_store._settings(WORKS_INDEX, "PATCH", INDEX_SETTINGS[WORKS_INDEX])
    else:
        await postgres_search_store._settings(WORKS_INDEX, "PATCH", {"searchableAttributes": ["different"]})
    w.client.rpush("rq:queue:imports", "next-import")
    if ordinary_pg:
        from app.services import heavy_io, search_delivery
        holder = heavy_io.local_lock_for_workload("pg_search_db")
        assert holder.try_acquire()
        try:
            denied = await search_delivery.run_delivery_slice(limit=25)
            assert denied["resource_deferred"] is True
            await asyncio.to_thread(outbox_coordinator.clear_and_wake_outbox_successor, "search", denied)
            assert w.client.hget(outbox_coordinator.RESOURCE_DEFERRED_KEY, "search")
            assert (await asyncio.to_thread(outbox_coordinator.wake_resource_deferred_outboxes))["enqueued"] == 0
        finally:
            holder.release()
    else:
        _defer(w, "search")
    try:
        result = await asyncio.to_thread(outbox_coordinator.wake_resource_deferred_outboxes)
        assert result["enqueued"] == int(ordinary_pg)
        assert w.client.llen("rq:queue:imports") == 1
        assert w.published == ([{"search": 1}] if ordinary_pg else [])
    finally:
        w.client.delete("rq:queue:imports", "resource:lanes:last-background")
        await w.db.execute(delete(SearchRebuild).where(SearchRebuild.id == bootstrap.id))
        await w.db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_release_inspection_reuses_and_closes_one_database_connection(waiting_work, monkeypatch):
    import sqlalchemy.ext.asyncio as sa_async
    from sqlalchemy import event
    from app.services import outbox_coordinator
    from app.services.redis_budget import budget_redis

    created, connected, closed = [], [], []
    real_engine = sa_async.create_async_engine
    def observed_engine(*args, **kwargs):
        engine = real_engine(*args, **kwargs)
        created.append(engine)
        event.listen(engine.sync_engine, "connect", lambda *_: connected.append(1))
        event.listen(engine.sync_engine, "close", lambda *_: closed.append(1))
        return engine
    monkeypatch.setattr(sa_async, "create_async_engine", observed_engine)
    with budget_redis(seconds=0.5, reserve_seconds=0):
        counts, ready, admitted, _future = await outbox_coordinator._inspect_deferred_outboxes(
            {"import_projection", "search", "media"}, waiting_work.client,
        )
    assert counts["import_projection"] > 0
    assert "import_projection" in ready and "import_projection" in admitted
    assert len(created) == len(connected) == len(closed) == 1


@pytest.mark.integration
@pytest.mark.parametrize("status", ["queued", "finished", "missing", "scheduled_orphan"])
def test_recovery_skips_only_active_single_owner_database_jobs(waiting_work, monkeypatch, status):
    from uuid import uuid4
    from rq import Queue
    from rq.job import JobStatus
    from app.services import outbox_coordinator

    w = waiting_work
    queue = Queue("maintenance", connection=w.client)
    job = queue.enqueue("app.jobs.import_projection.run_import_projection_outbox", job_id="recovery-owner-"+uuid4().hex)
    key = outbox_coordinator._wake_key("import_projection")
    w.client.set(key, job.id)
    _defer(w, "import_projection")
    inspections = []
    real_inspect = outbox_coordinator._inspect_deferred_outboxes
    async def inspect(kinds, client):
        inspections.append(kinds)
        return await real_inspect(kinds, client)
    monkeypatch.setattr(outbox_coordinator, "_inspect_deferred_outboxes", inspect)
    if status == "finished":job.set_status(JobStatus.FINISHED)
    elif status == "missing":job.delete()
    elif status == "scheduled_orphan":job.set_status(JobStatus.SCHEDULED)
    try:
        result = outbox_coordinator.wake_resource_deferred_outboxes()
        assert result["enqueued"] == int(status != "queued")
        assert len(inspections) == int(status != "queued")
        assert w.client.hget(outbox_coordinator.RESOURCE_DEFERRED_KEY, "import_projection")
        if status == "queued":
            job.set_status(JobStatus.FINISHED)
            outbox_coordinator._clear_owned_wake(w.client, key, job.id)
            assert not w.client.exists(key)
            assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 1
            assert len(inspections) == 1
    finally:
        queue.remove(job.id)
        if status != "missing":job.delete()
        w.client.delete(key)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("phase,new_curation,expected", [("metadata",True,1),("metadata",False,0),(None,True,0)])
async def test_metadata_deferral_rechecks_only_new_curation_after_clear(
    waiting_work, monkeypatch, phase, new_curation, expected,
):
    import asyncio
    from uuid import uuid4
    from rq import Queue
    from rq.job import Job, JobStatus
    from app.models import ImportCurationOutbox
    from app.services import outbox_coordinator as coordinator
    w=waiting_work
    await w.db.execute(update(ImportCurationOutbox).values(state="complete"))
    await w.db.commit()
    q=Queue("maintenance",connection=w.client)
    before=set(q.job_ids)
    owner=Job.create("app.jobs.import_projection.run_import_projection_outbox",
        connection=w.client,origin="maintenance",id="metadata-owner-"+uuid4().hex)
    owner.save();owner.set_status(JobStatus.STARTED)
    key=coordinator._wake_key("import_projection")
    w.client.set(key,owner.id)
    monkeypatch.setattr("rq.get_current_job",lambda:owner)
    monkeypatch.setattr(coordinator,"wake_pending_outboxes",w.real_wake)
    if phase == "metadata":
        # Exercise the liveness race while background work is unavailable.
        monkeypatch.setattr("app.services.heavy_io.background_turn_available", lambda _redis: False)
    w.client.rpush("rq:queue:imports","metadata-deferral-ingest")
    try:
        if new_curation:
            # A real commit after the old job's final claim merges into its
            # still-live owner; clearing that owner must not lose the wake.
            await w.db.execute(update(ImportCurationOutbox).values(state="pending",available_at=datetime.now(timezone.utc)))
            coordinator.mark_outbox_wake_pending(w.db,"import_projection")
            await w.db.commit()
            assert set(q.job_ids)==before
            assert w.client.get(key).decode()==owner.id
        outcome={"claimed":0,"processed":1 if phase == "metadata" else 0,"more_likely":False,"resource_deferred":True}
        if phase:outcome["deferred_projection_part"]=phase
        result=await asyncio.to_thread(coordinator.clear_and_wake_outbox_successor,"import_projection",outcome)
        assert result["enqueued"]==expected
        assert len(set(q.job_ids)-before)==expected
        assert w.client.hget(coordinator.RESOURCE_DEFERRED_KEY,"import_projection")
    finally:
        for identity in set(q.job_ids)-before:
            q.remove(identity);Job.fetch(identity,connection=w.client).delete()
        w.client.delete(key);owner.delete()
        w.client.lrem("rq:queue:imports",0,"metadata-deferral-ingest")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_metadata_deferral_posix_lock_blocks_then_releases_one_wake(waiting_work, monkeypatch):
    """A ready curation cannot bypass its real completion POSIX lock."""
    from app.models import ImportCurationOutbox
    from app.services import heavy_io, outbox_coordinator as coordinator

    import asyncio
    w = waiting_work
    await w.db.execute(update(ImportCurationOutbox).values(
        state="pending", available_at=datetime.now(timezone.utc)
    ))
    await w.db.commit()
    _defer(w, "import_projection")
    holder = heavy_io.local_lock_for_workload("import_db", lane="completion")
    assert holder.try_acquire()
    monkeypatch.setattr(coordinator, "wake_pending_outboxes", w.real_wake)
    try:
        blocked = await asyncio.to_thread(coordinator._wake_curation_after_metadata_deferral)
        assert blocked["enqueued"] == 0
        assert w.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "import_projection")
    finally:
        holder.release()
    from rq import Queue
    from rq.job import Job
    q=Queue("maintenance",connection=w.client)
    before=set(q.job_ids)
    try:
        assert (await asyncio.to_thread(coordinator._wake_curation_after_metadata_deferral))["enqueued"] == 1
        assert len(set(q.job_ids)-before) == 1
        assert (await asyncio.to_thread(coordinator._wake_curation_after_metadata_deferral))["enqueued"] == 0
    finally:
        for identity in set(q.job_ids)-before:
            q.remove(identity);Job.fetch(identity,connection=w.client).delete()
        w.client.delete(coordinator._wake_key("import_projection"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_metadata_deferral_recheck_timeout_keeps_completed_curation_and_token(
    waiting_work, monkeypatch,
):
    from app.models import ImportCurationOutbox
    from app.services import outbox_coordinator as coordinator

    import asyncio
    w = waiting_work
    await w.db.execute(update(ImportCurationOutbox).values(state="complete"))
    await w.db.commit()
    _defer(w, "import_projection")

    async def timeout(_db):
        raise TimeoutError("injected curation readiness timeout")

    monkeypatch.setattr(coordinator, "_curation_projection_ready", timeout)
    result = await asyncio.to_thread(coordinator._wake_curation_after_metadata_deferral)
    assert result == {"enqueued": 0, "deferred": 1}
    assert w.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "import_projection")
    assert await w.db.scalar(
        select(ImportCurationOutbox.state).where(ImportCurationOutbox.state == "complete")
    ) == "complete"


@pytest.mark.integration
def test_metadata_deferral_redis_failure_keeps_token(waiting_work, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError
    from app.services import outbox_coordinator as coordinator

    w = waiting_work
    _defer(w, "import_projection")
    monkeypatch.setattr(
        coordinator, "get_redis",
        lambda: (_ for _ in ()).throw(RedisConnectionError("injected recovery Redis failure")),
    )
    result = coordinator._wake_curation_after_metadata_deferral()
    assert result == {"enqueued": 0, "deferred": 1}
    assert w.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "import_projection")


@pytest.mark.integration
def test_metadata_only_deferred_marker_skips_background_probe_until_available(
    waiting_work, monkeypatch,
):
    from app.services import outbox_coordinator as coordinator

    w = waiting_work
    token = coordinator._resource_deferred_token(metadata_only=True)
    w.client.hset(coordinator.RESOURCE_DEFERRED_KEY, "import_projection", token)
    calls = []

    async def unexpected_probe(*_args):
        calls.append(True)
        raise AssertionError("metadata-only recovery must not inspect without background time")

    monkeypatch.setattr("app.services.heavy_io.background_turn_available", lambda _redis: False)
    monkeypatch.setattr(coordinator, "_inspect_deferred_outboxes", unexpected_probe)
    assert coordinator.wake_resource_deferred_outboxes()["enqueued"] == 0
    assert not calls
    assert w.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "import_projection").decode() == token

    async def inspect(_kinds, _redis):
        assert _kinds == {"import_projection"}
        return ({"import_projection": 1}, {"import_projection"}, {"import_projection"}, {})

    monkeypatch.setattr("app.services.heavy_io.background_turn_available", lambda _redis: True)
    monkeypatch.setattr(coordinator, "_inspect_deferred_outboxes", inspect)
    assert coordinator.wake_resource_deferred_outboxes()["enqueued"] == 1
    assert w.published[-1] == {"import_projection": 1}


@pytest.mark.integration
def test_deferred_marker_hint_does_not_reclassify_curation_denial(waiting_work, monkeypatch):
    from app.services import outbox_coordinator as coordinator

    monkeypatch.setattr("rq.get_current_job", lambda: None)
    coordinator.clear_and_wake_outbox_successor(
        "import_projection",
        {"resource_deferred": True, "processed": 0},
    )
    token = waiting_work.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "import_projection")
    assert token
    assert not coordinator._is_metadata_only_deferred_token(token)

    coordinator.clear_and_wake_outbox_successor(
        "import_projection",
        {"resource_deferred": True, "deferred_projection_part": "metadata", "processed": 1},
    )
    token = waiting_work.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "import_projection")
    assert token
    assert coordinator._is_metadata_only_deferred_token(token)


@pytest.mark.integration
def test_stale_deferred_token_cannot_clear_a_new_marker(waiting_work):
    from app.services import outbox_coordinator as coordinator

    w = waiting_work
    old = coordinator._resource_deferred_token(metadata_only=True)
    new = coordinator._resource_deferred_token(metadata_only=True)
    w.client.hset(coordinator.RESOURCE_DEFERRED_KEY, "import_projection", new)
    assert w.client.eval(
        coordinator._CLEAR_DEFERRED_SCRIPT,
        1,
        coordinator.RESOURCE_DEFERRED_KEY,
        "import_projection",
        old,
    ) == 0
    assert w.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "import_projection").decode() == new


@pytest.mark.integration
def test_repeated_media_commit_notifications_refresh_token_until_release(
    waiting_work, monkeypatch,
):
    """Repeated commits keep one durable marker and release wakes it once."""
    from app.services import outbox_coordinator as coordinator

    w = waiting_work
    published = w.published
    _defer(w)
    tokens = []
    monkeypatch.setattr(
        "app.services.heavy_io.background_turn_available",
        lambda _redis: False,
    )
    for _ in range(3):
        session = SimpleNamespace(
            info={coordinator._OUTBOX_WAKE_COMMITTING: {"media"}},
            get_nested_transaction=lambda: None,
        )
        coordinator._publish_outbox_wakes_after_commit(session)
        token = w.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "media")
        assert token
        tokens.append(token)
    assert len(set(tokens)) == 3
    assert published == []

    monkeypatch.setattr(
        "app.services.heavy_io.background_turn_available",
        lambda _redis: True,
    )
    assert coordinator.wake_resource_deferred_outboxes()["enqueued"] == 1
    assert published[-1] == {"media": 1}
    # Recovery leaves the token while work is ready; a later recovery pass
    # clears it after the durable outbox is drained.
    assert w.client.hexists(coordinator.RESOURCE_DEFERRED_KEY, "media")


@pytest.mark.integration
def test_released_media_token_cannot_be_cleared_by_previous_commit(
    waiting_work,
):
    """A stale commit/recovery CAS cannot delete the current release marker."""
    from app.services import outbox_coordinator as coordinator

    w = waiting_work
    old = coordinator._resource_deferred_token(metadata_only=False)
    current = coordinator._resource_deferred_token(metadata_only=False)
    w.client.hset(coordinator.RESOURCE_DEFERRED_KEY, "media", current)
    assert w.client.eval(
        coordinator._CLEAR_DEFERRED_SCRIPT,
        1,
        coordinator.RESOURCE_DEFERRED_KEY,
        "media",
        old,
    ) == 0
    assert w.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "media").decode() == current


@pytest.mark.integration
@pytest.mark.asyncio
async def test_media_token_clears_only_after_recovered_outbox_drains(
    waiting_work, monkeypatch,
):
    """Recovery publishes first, then clears its token after media completion."""
    from app.models import MediaDerivativeOutbox
    from app.services import outbox_coordinator as coordinator

    w = waiting_work
    _defer(w)
    monkeypatch.setattr(
        "app.services.heavy_io.background_turn_available",
        lambda _redis: True,
    )
    first = await asyncio.to_thread(
        coordinator.wake_resource_deferred_outboxes,
    )
    assert first["enqueued"] == 1
    token = w.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "media")
    assert token

    await w.db.execute(
        update(MediaDerivativeOutbox).where(
            MediaDerivativeOutbox.asset_id == w.staged["derivative_requests"][0]["asset_id"]
        ).values(state="complete")
    )
    await w.db.commit()

    # An older recovery pass cannot clear a renewed marker while the outbox is
    # still authoritative; the current token is cleared only after the row drains.
    renewed = coordinator._resource_deferred_token(metadata_only=False)
    w.client.hset(coordinator.RESOURCE_DEFERRED_KEY, "media", renewed)
    assert w.client.eval(
        coordinator._CLEAR_DEFERRED_SCRIPT,
        1,
        coordinator.RESOURCE_DEFERRED_KEY,
        "media",
        token,
    ) == 0
    assert w.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "media").decode() == renewed

    second = await asyncio.to_thread(
        coordinator.wake_resource_deferred_outboxes,
    )
    assert second["enqueued"] == 0
    assert not w.client.hexists(coordinator.RESOURCE_DEFERRED_KEY, "media")


@pytest.mark.integration
def test_metadata_only_filter_does_not_remove_other_recovery_kinds(waiting_work, monkeypatch):
    from app.services import outbox_coordinator as coordinator

    w = waiting_work
    metadata_token = coordinator._resource_deferred_token(metadata_only=True)
    w.client.hset(coordinator.RESOURCE_DEFERRED_KEY, "import_projection", metadata_token)
    w.client.hset(coordinator.RESOURCE_DEFERRED_KEY, "search", "legacy-search")
    seen = []

    async def inspect(kinds, _redis):
        seen.append(set(kinds))
        return (
            {"search": 1}, {"search"}, {"search"}, {},
        )

    monkeypatch.setattr("app.services.heavy_io.background_turn_available", lambda _redis: False)
    monkeypatch.setattr(coordinator, "_inspect_deferred_outboxes", inspect)
    assert coordinator.wake_resource_deferred_outboxes()["enqueued"] == 1
    assert seen == [{"search"}]
    assert w.published[-1] == {"search": 1}
    assert w.client.hget(coordinator.RESOURCE_DEFERRED_KEY, "import_projection").decode() == metadata_token


@pytest.mark.integration
@pytest.mark.parametrize("other_kind", ["media", "search"])
def test_metadata_only_filter_keeps_mixed_ready_kind_when_background_is_available(
    waiting_work, monkeypatch, other_kind,
):
    from app.services import outbox_coordinator as coordinator

    w = waiting_work
    metadata_token = coordinator._resource_deferred_token(metadata_only=True)
    w.client.hset(coordinator.RESOURCE_DEFERRED_KEY, "import_projection", metadata_token)
    w.client.hset(coordinator.RESOURCE_DEFERRED_KEY, other_kind, "legacy-" + other_kind)
    seen = []

    async def inspect(kinds, _redis):
        seen.append(set(kinds))
        return (
            {"import_projection": 1, other_kind: 1},
            {"import_projection", other_kind},
            {"import_projection", other_kind},
            {},
        )

    monkeypatch.setattr("app.services.heavy_io.background_turn_available", lambda _redis: True)
    monkeypatch.setattr(coordinator, "_inspect_deferred_outboxes", inspect)
    assert coordinator.wake_resource_deferred_outboxes()["enqueued"] == 2
    assert seen == [{"import_projection", other_kind}]
