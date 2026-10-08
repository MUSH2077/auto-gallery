"""Recovery events reach the single coordinator even with no executing job."""
import asyncio
import time
from types import SimpleNamespace

import pytest


def test_capacity_change_is_published_without_a_pressure_state_change(monkeypatch):
    from app.services import resource_pressure_snapshot as publication

    monkeypatch.setattr(publication, "_last_published_signature", None)
    sent = []
    writes = []
    client = SimpleNamespace(set=lambda *_args, **_kwargs: writes.append(1),
                             publish=lambda *_: sent.append(1))
    snapshot = {"status": "normal", "controller_mode": "normal",
                "budget": {"profiles": {"image_derive": {"allowed": True, "memory_reservation_bytes": 768 * 1024 ** 2}}, "reservation": {"reserved_bytes": 0}},
                "memory": {"available_bytes": 656 * 1024 ** 2},
                "controller": {"hard_limits": {"memory_available_bytes": 256 * 1024 ** 2}}}
    assert publication.publish_resource_pressure_snapshot(snapshot, client)
    snapshot["memory"]["available_bytes"] = 1056 * 1024 ** 2
    assert publication.publish_resource_pressure_snapshot(snapshot, client)
    assert len(sent) == 2
    snapshot["memory"]["available_bytes"] += 1024 ** 2
    assert publication.publish_resource_pressure_snapshot(snapshot, client)
    assert len(sent) == 2
    assert len(writes) == 2


def test_empty_worker_pool_recovers_from_events_and_coalesces_them(monkeypatch):
    from app.services import outbox_coordinator, outbox_recovery_control
    messages = []
    closed = []
    class PubSub:
        def subscribe(self, *_):
            pass
        def get_message(self, **_):
            return messages.pop(0) if messages else None
        def close(self):
            closed.append("pubsub")
    client = SimpleNamespace(pubsub=lambda **_: PubSub(), close=lambda: closed.append("client"))
    monkeypatch.setattr(outbox_recovery_control.redis.Redis, "from_url", lambda *_args, **_kwargs: client)
    calls = []
    monkeypatch.setattr(outbox_coordinator, "wake_resource_deferred_outboxes", lambda: calls.append(1) or {"enqueued": 1})
    clock = [0.0]
    monkeypatch.setattr(outbox_recovery_control.time, "monotonic", lambda: clock[0])
    control = outbox_recovery_control.OutboxRecoveryControl("redis://unused")
    control.poll()  # Existing waiting flags after supervisor restart.
    assert len(calls) == 1
    clock[0] = 2.0
    messages.extend({"type": "message", "data": b"resource_budget_changed"} for _ in range(10))
    control.poll()
    assert len(calls) == 2
    control.poll()
    assert len(calls) == 2
    control.close()
    assert closed == ["pubsub", "client"]


def test_slow_database_check_is_bounded_and_keeps_waiting_flags(monkeypatch):
    from app.services import outbox_coordinator, remote_search_flight
    flags = {b"media": b"waiting"}
    client = SimpleNamespace(hgetall=lambda *_: flags, llen=lambda *_: 0,
                             zcard=lambda *_: 0, exists=lambda *_: 0)
    monkeypatch.setattr(outbox_coordinator, "get_redis", lambda: client)
    monkeypatch.setattr(remote_search_flight, "read_marker", lambda: None)
    async def slow(*_):
        await asyncio.sleep(0.75)
        pytest.fail("Timed-out database query kept running")
    monkeypatch.setattr(outbox_coordinator, "_inspect_deferred_outboxes", slow)
    started = time.monotonic()
    assert outbox_coordinator.wake_resource_deferred_outboxes(timeout_seconds=0.05) == {"enqueued": 0, "deferred": 1}
    assert time.monotonic() - started < 0.3
    assert flags == {b"media": b"waiting"}


def test_missing_or_paused_snapshot_never_admits_recovery(monkeypatch):
    from app.services import outbox_coordinator, resource_pressure, resource_pressure_snapshot
    monkeypatch.setattr(resource_pressure_snapshot, "read_shared_resource_pressure_snapshot", lambda **_: None)
    assert outbox_coordinator._recovery_pressure_snapshot(object()) is None
    monkeypatch.setattr(resource_pressure_snapshot, "read_shared_resource_pressure_snapshot", lambda **_: {"status": "normal"})
    monkeypatch.setattr(resource_pressure, "read_resource_pressure_latch", lambda **_: {"status": "paused"})
    assert outbox_coordinator._recovery_pressure_snapshot(object()) is None


@pytest.mark.integration
def test_slow_postgres_query_respects_recovery_deadline(monkeypatch):
    from sqlalchemy import text
    from app.services import outbox_coordinator, remote_search_flight

    flags = {b"media": b"waiting"}
    client = SimpleNamespace(hgetall=lambda *_: flags, llen=lambda *_: 0,
                             zcard=lambda *_: 0, exists=lambda *_: 0)
    monkeypatch.setattr(outbox_coordinator, "get_redis", lambda: client)
    monkeypatch.setattr(remote_search_flight, "read_marker", lambda: None)

    async def slow_counts(db, **_):
        # Execute on the same fresh, bounded PostgreSQL connection as recovery.
        await db.execute(text("SELECT pg_sleep(1)"))
        pytest.fail("Slow database query exceeded the recovery deadline")

    monkeypatch.setattr(outbox_coordinator, "outbox_counts", slow_counts)
    started = time.monotonic()
    assert outbox_coordinator.wake_resource_deferred_outboxes(timeout_seconds=0.1) == {"enqueued": 0, "deferred": 1}
    assert time.monotonic() - started < 0.5
    assert flags == {b"media": b"waiting"}
