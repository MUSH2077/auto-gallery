"""Media wake ownership against real Redis and RQ job metadata."""
from types import SimpleNamespace

import pytest
from rq import Queue
from rq.job import JobStatus

from app.services import outbox_coordinator as coordinator
from app.services.queue_admission import QueueAdmissionError
from app.services.redis_client import get_redis


@pytest.fixture
def wake_pool(monkeypatch):
    client = get_redis()
    queue = Queue("operations", connection=client)
    created = []
    monkeypatch.setattr(coordinator.settings, "media_worker_concurrency", 4)
    monkeypatch.setattr(coordinator, "get_redis", lambda: client)
    real_enqueue = coordinator.checked_enqueue
    real_later = coordinator.checked_enqueue_in

    def enqueue(*args, **kwargs):
        job = real_enqueue(*args, **kwargs)
        created.append(job)
        return job

    def later(*args, **kwargs):
        job = real_later(*args, **kwargs)
        created.append(job)
        return job

    monkeypatch.setattr(coordinator, "checked_enqueue", enqueue)
    monkeypatch.setattr(coordinator, "checked_enqueue_in", later)
    keys = [coordinator._wake_key("media", slot) for slot in range(8)]
    # This fixture uses the conftest-isolated Redis DB15, with no live consumers.
    assert not any(client.mget(keys))
    try:
        yield SimpleNamespace(client=client, queue=queue, created=created, keys=keys)
    finally:
        for job in created:
            queue.remove(job.id)
            job.delete(remove_from_queue=True)
        client.delete(*keys)


@pytest.mark.integration
def test_readiness_hint_fills_bounded_pool_without_duplicate_jobs(wake_pool):
    assert coordinator.wake_pending_outboxes({"media": 1}) == {"enqueued": 4, "deferred": 0}
    owners = wake_pool.client.mget(wake_pool.keys[:4])
    assert len(set(owners)) == 4 and all(owners)
    assert {job.meta["outbox_wake_slot"] for job in wake_pool.created} == {0, 1, 2, 3}
    assert coordinator.wake_pending_outboxes({"media": 1}) == {"enqueued": 0, "deferred": 0}
    assert len(wake_pool.created) == 4
    assert coordinator.wake_pending_outboxes({"media": 0}) == {"enqueued": 0, "deferred": 0}


@pytest.mark.integration
def test_clear_and_delayed_successor_only_change_their_slot(wake_pool, monkeypatch):
    coordinator.wake_pending_outboxes({"media": 1})
    before = wake_pool.client.mget(wake_pool.keys[:4])
    job = next(job for job in wake_pool.created if job.meta["outbox_wake_slot"] == 2)
    monkeypatch.setattr("rq.get_current_job", lambda: job)
    result = coordinator.clear_and_wake_outbox_successor("media", {
        "more_likely": True, "successor_delay_seconds": 5,
    })
    assert result == {"enqueued": 1, "deferred": 0}
    after = wake_pool.client.mget(wake_pool.keys[:4])
    assert [after[i] for i in (0, 1, 3)] == [before[i] for i in (0, 1, 3)]
    assert after[2] != before[2]
    successor = wake_pool.created[-1]
    assert successor.meta["outbox_wake_slot"] == 2
    assert successor.get_status(refresh=True) == JobStatus.SCHEDULED
    assert coordinator._wake_job_is_active(wake_pool.client, successor.id)
    coordinator.clear_outbox_wake("media")
    assert wake_pool.client.get(wake_pool.keys[2]) == after[2], "old job cannot clear successor"
    monkeypatch.setattr("rq.get_current_job", lambda: successor)
    coordinator.clear_and_wake_outbox_successor("media", {"more_likely": False})
    assert wake_pool.client.get(wake_pool.keys[2]) is None
    assert len(wake_pool.created) == 5, "empty runner creates no successor"


@pytest.mark.integration
def test_shrink_keeps_live_owners_but_retires_successor(wake_pool, monkeypatch):
    coordinator.wake_pending_outboxes({"media": 1})
    owners = wake_pool.client.mget(wake_pool.keys[:4])
    job = next(job for job in wake_pool.created if job.meta["outbox_wake_slot"] == 3)
    monkeypatch.setattr(coordinator.settings, "media_worker_concurrency", 1)
    assert coordinator.wake_pending_outboxes({"media": 1})["enqueued"] == 0
    assert wake_pool.client.mget(wake_pool.keys[:4]) == owners
    monkeypatch.setattr("rq.get_current_job", lambda: job)
    assert coordinator.clear_and_wake_outbox_successor("media", {"more_likely": True}) == {"enqueued": 0, "deferred": 0}
    assert wake_pool.client.get(wake_pool.keys[3]) is None
    assert len(wake_pool.created) == 4
    monkeypatch.setattr(coordinator.settings, "media_worker_concurrency", 4)
    monkeypatch.setattr("rq.get_current_job", lambda: None)
    assert coordinator.wake_pending_outboxes({"media": 1})["enqueued"] == 1
    assert wake_pool.client.get(wake_pool.keys[3])


@pytest.mark.integration
def test_terminal_retired_owner_is_removed_without_touching_live_owner(wake_pool, monkeypatch):
    coordinator.wake_pending_outboxes({"media": 1})
    terminal = next(job for job in wake_pool.created if job.meta["outbox_wake_slot"] == 1)
    terminal.set_status(JobStatus.FINISHED)
    live = wake_pool.client.get(wake_pool.keys[2])
    monkeypatch.setattr(coordinator.settings, "media_worker_concurrency", 1)
    coordinator.wake_pending_outboxes({"media": 1})
    assert wake_pool.client.get(wake_pool.keys[1]) is None
    assert wake_pool.client.get(wake_pool.keys[2]) == live


@pytest.mark.integration
def test_queue_rejection_releases_only_attempted_owners(wake_pool, monkeypatch):
    def rejected(*args, **kwargs):
        raise QueueAdmissionError("test_rejection", "full queue")

    monkeypatch.setattr(coordinator, "checked_enqueue", rejected)
    assert coordinator.wake_pending_outboxes({"media": 1}) == {"enqueued": 0, "deferred": 4}
    assert not any(wake_pool.client.mget(wake_pool.keys))


@pytest.mark.integration
def test_legacy_zero_slot_job_still_clears_its_owned_marker(wake_pool, monkeypatch):
    wake_pool.client.set(wake_pool.keys[0], "legacy-media-owner")
    monkeypatch.setattr("rq.get_current_job", lambda: SimpleNamespace(id="legacy-media-owner", meta={}))
    coordinator.clear_outbox_wake("media")
    assert wake_pool.client.get(wake_pool.keys[0]) is None


@pytest.mark.parametrize("count", [1, 4, 8])
def test_config_accepts_supported_image_worker_counts(count):
    from app.config import Settings

    assert Settings(media_worker_concurrency=count).media_worker_concurrency == count


@pytest.mark.parametrize("count", [0, 9])
def test_config_rejects_image_worker_counts_outside_the_bound(count):
    from app.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(media_worker_concurrency=count)
