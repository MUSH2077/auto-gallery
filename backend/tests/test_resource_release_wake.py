"""Release retries preserve admission, ownership and delayed continuations."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import update

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
