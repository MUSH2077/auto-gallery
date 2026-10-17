"""Atomic owner/publication ordering against real Redis and RQ registries."""
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture
def wake_queue(monkeypatch):
    from rq import Queue
    from rq.registry import ScheduledJobRegistry
    from app.services import outbox_coordinator
    from app.services.redis_client import get_redis

    client = get_redis()
    name = "publication-test-" + uuid4().hex
    queue = outbox_coordinator._AtomicOutboxQueue(name, connection=client)
    registry = ScheduledJobRegistry(name, connection=client)
    key = name + ":wake"
    monkeypatch.setattr(outbox_coordinator, "_AtomicOutboxQueue", lambda **_: queue)
    monkeypatch.setattr(outbox_coordinator, "get_redis", lambda: client)
    monkeypatch.setattr(outbox_coordinator, "_wake_key", lambda *_: key)
    try:
        yield SimpleNamespace(client=client, queue=queue, registry=registry, key=key)
    finally:
        for job_id in registry.get_job_ids():
            registry.remove(job_id, delete_job=True)
        queue.delete(delete_jobs=True)
        client.delete(key)


@pytest.mark.integration
@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("delay", [0.0, 5.0])
def test_concurrent_wake_preserves_inflight_publication(wake_queue, monkeypatch, handoff, delay):
    from app.services import outbox_coordinator

    w = wake_queue
    entered, release = threading.Event(), threading.Event()
    failures, outcomes = [], []
    method = "checked_enqueue_in" if delay else "checked_enqueue"
    real_enqueue = getattr(outbox_coordinator, method)
    if handoff:
        w.client.set(w.key, "previous-owner")

    def enqueue(*args, **kwargs):
        if threading.current_thread() is publisher:
            entered.set()
            assert release.wait(5), "Concurrent caller did not release publisher"
        return real_enqueue(*args, **kwargs)

    def first():
        try:
            outcomes.append(outbox_coordinator.wake_pending_outboxes(
                {"dedup": 1}, delay_seconds=delay,
                replace_owner="previous-owner" if handoff else None,
            ))
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(outbox_coordinator, method, enqueue)
    publisher = threading.Thread(target=first)
    try:
        publisher.start()
        assert entered.wait(5), "First caller never reached publication"
        second = outbox_coordinator.wake_pending_outboxes({"dedup": 1}, delay_seconds=delay)
        release.set()
        publisher.join(5)
        assert not publisher.is_alive()
        assert not failures
        assert outcomes[0]["enqueued"] + second["enqueued"] == 1
        assert outcomes[0]["deferred"] == second["deferred"] == 0
        ids = w.registry.get_job_ids() if delay else w.queue.job_ids
        assert len(ids) == 1
        assert w.queue.count == (0 if delay else 1)
        assert w.client.get(w.key).decode() == ids[0]
    finally:
        release.set()
        publisher.join(5)


@pytest.mark.integration
def test_lost_exec_response_keeps_committed_owner(wake_queue, monkeypatch):
    from redis.exceptions import ConnectionError
    from app.services import outbox_coordinator

    w = wake_queue
    real_pipeline = w.client.pipeline
    lost = []
    class LostResponse:
        def __init__(self):
            self.pipe = real_pipeline()
        def __enter__(self):
            self.pipe.__enter__()
            return self
        def __exit__(self, *args):
            return self.pipe.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.pipe, name)
        def execute(self, *args, **kwargs):
            value = self.pipe.execute(*args, **kwargs)
            if not lost:
                lost.append(True)
                raise ConnectionError("EXEC committed but its reply was lost")
            return value

    monkeypatch.setattr(w.client, "pipeline", lambda *args, **kwargs: LostResponse())
    assert outbox_coordinator.wake_pending_outboxes({"dedup": 1}) == {"enqueued": 0, "deferred": 1}
    owner = w.client.get(w.key).decode()
    assert w.queue.job_ids == [owner]
    assert outbox_coordinator.wake_pending_outboxes({"dedup": 1}) == {"enqueued": 0, "deferred": 0}
    assert w.client.get(w.key).decode() == owner


@pytest.mark.integration
def test_rejected_handoff_keeps_previous_owner(wake_queue, monkeypatch):
    from app.services import outbox_coordinator
    from app.services.queue_admission import QueueAdmissionError

    w = wake_queue
    w.client.set(w.key, "previous-owner")
    def rejected(*args, **kwargs):
        raise QueueAdmissionError("redis_capacity", "Injected capacity refusal")
    monkeypatch.setattr(outbox_coordinator, "checked_enqueue", rejected)
    assert outbox_coordinator.wake_pending_outboxes(
        {"dedup": 1}, replace_owner="previous-owner"
    ) == {"enqueued": 0, "deferred": 1}
    assert w.client.get(w.key) == b"previous-owner"
    assert w.queue.count == 0


@pytest.mark.integration
def test_queue_hint_follows_committed_job(wake_queue, monkeypatch):
    from app.services import outbox_coordinator

    w = wake_queue
    seen = []
    def hint(*args):
        seen.append((w.client.get(w.key).decode(), list(w.queue.job_ids)))
    monkeypatch.setattr(outbox_coordinator, "notify_queue_worker", hint)
    assert outbox_coordinator.wake_pending_outboxes({"dedup": 1})["enqueued"] == 1
    assert seen == [(w.queue.job_ids[0], w.queue.job_ids)]
