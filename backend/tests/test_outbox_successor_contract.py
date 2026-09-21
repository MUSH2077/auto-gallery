"""Pure contracts for bounded outbox successor scheduling."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import media_derivatives
from app.services import outbox_coordinator


def test_scheduled_wake_job_missing_from_registry_is_not_active(monkeypatch):
    """A persisted scheduled status must not strand an outbox after registry loss."""

    job = SimpleNamespace(
        origin="operations",
        get_status=lambda refresh=True: SimpleNamespace(value="scheduled"),
    )

    monkeypatch.setattr("rq.job.Job.fetch", lambda *_args, **_kwargs: job)

    class EmptyScheduledRegistry:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_job_ids(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr("rq.registry.ScheduledJobRegistry", EmptyScheduledRegistry)

    assert outbox_coordinator._wake_job_is_active(object(), "orphaned-wake") is False


def test_scheduled_wake_job_in_registry_remains_active(monkeypatch):
    job = SimpleNamespace(
        origin="operations",
        get_status=lambda refresh=True: SimpleNamespace(value="scheduled"),
    )

    monkeypatch.setattr("rq.job.Job.fetch", lambda *_args, **_kwargs: job)

    class LiveScheduledRegistry:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_job_ids(self, *_args, **_kwargs):
            return ["live-wake"]

    monkeypatch.setattr("rq.registry.ScheduledJobRegistry", LiveScheduledRegistry)

    assert outbox_coordinator._wake_job_is_active(object(), "live-wake") is True


def test_successor_clears_owned_marker_before_waking(monkeypatch):
    calls = []

    monkeypatch.setattr(
        outbox_coordinator,
        "clear_outbox_wake",
        lambda kind: calls.append(("clear", kind)),
    )
    monkeypatch.setattr(
        outbox_coordinator,
        "wake_pending_outboxes",
        lambda counts, **kwargs: calls.append(("wake", counts, kwargs))
        or {"enqueued": 1, "deferred": 0},
    )

    result = outbox_coordinator.clear_and_wake_outbox_successor(
        "media",
        {"slice_exhausted": True, "more_likely": True},
    )

    assert calls == [
        ("clear", "media"),
        (
            "wake",
            {"media": 1},
            {"delay_seconds": 0.0, "replace_owner": None},
        ),
    ]
    assert result == {"enqueued": 1, "deferred": 0}


def test_empty_or_failed_slice_only_clears_and_uses_health_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(
        outbox_coordinator,
        "clear_outbox_wake",
        lambda kind: calls.append(("clear", kind)),
    )
    monkeypatch.setattr(
        outbox_coordinator,
        "wake_pending_outboxes",
        lambda counts, **kwargs: calls.append(("wake", counts, kwargs)),
    )

    empty = outbox_coordinator.clear_and_wake_outbox_successor(
        "gitllery",
        {"slice_exhausted": False, "more_likely": False},
    )
    failed = outbox_coordinator.clear_and_wake_outbox_successor(
        "gitllery",
        None,
    )

    assert calls == [("clear", "gitllery"), ("clear", "gitllery")]
    assert empty == {"enqueued": 0, "deferred": 0}
    assert failed == {"enqueued": 0, "deferred": 0}


def test_successor_wake_error_does_not_fail_completed_domain_slice(monkeypatch):
    monkeypatch.setattr(outbox_coordinator, "clear_outbox_wake", lambda _kind: None)

    def fail(_counts, **_kwargs):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(outbox_coordinator, "wake_pending_outboxes", fail)

    assert outbox_coordinator.clear_and_wake_outbox_successor(
        "media",
        {"more_likely": True},
    ) == {"enqueued": 0, "deferred": 1}


@pytest.mark.asyncio
async def test_media_slice_counts_claims_and_reports_likely_successor(monkeypatch):
    claims = []

    async def claim_one(_asset_id=None):
        claims.append(True)
        return {"asset_id": "asset-1"}

    async def candidate():
        return "asset-1", "image_derive"

    @asynccontextmanager
    async def permit(*_args, **_kwargs):
        yield SimpleNamespace(work_units=1, slice_seconds=20.0)

    async def load_descriptor(_asset_id):
        return None

    async def complete(_request, _values):
        return True

    monkeypatch.setattr(media_derivatives, "_claim_one", claim_one)
    monkeypatch.setattr(media_derivatives, "_next_resource_candidate", candidate)
    monkeypatch.setattr(
        "app.services.heavy_io.adaptive_resource_slice",
        permit,
    )
    monkeypatch.setattr(media_derivatives, "_load_asset_descriptor", load_descriptor)
    monkeypatch.setattr(media_derivatives, "_complete", complete)

    result = await media_derivatives.process_media_derivative_outbox(limit=1)

    assert len(claims) == 1
    assert result == {
        "claimed": 1,
        "processed": 1,
        "failed": 0,
        "slice_exhausted": True,
        "more_likely": True,
    }


@pytest.mark.asyncio
async def test_media_empty_slice_does_not_create_successor_hint(monkeypatch):
    async def no_candidate():
        return None

    monkeypatch.setattr(media_derivatives, "_next_resource_candidate", no_candidate)

    result = await media_derivatives.process_media_derivative_outbox(limit=1)

    assert result["claimed"] == 0
    assert result["slice_exhausted"] is False
    assert result["more_likely"] is False


def test_search_job_preserves_delayed_poll_successor(monkeypatch):
    from app.jobs import search_projection
    from app.services import search_delivery

    async def pending(**kwargs):
        return {"status": "pending", "more_likely": True, "successor_delay_seconds": 15}

    monkeypatch.setattr(search_delivery, "run_delivery_slice", pending)
    monkeypatch.setattr(search_projection, "clear_and_wake_outbox_successor", lambda *_args: None)
    result = search_projection.run_search_projection_outbox()
    assert result["more_likely"] is True
    assert result["successor_delay_seconds"] == 15
