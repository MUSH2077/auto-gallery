"""Exercise the real media chooser and runner under changing resource limits."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture
def media_candidates(monkeypatch):
    from app.services import heavy_io, media_derivatives, resource_pressure

    image, video = uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(asset_id=image, available_at=now - timedelta(seconds=2),
                        priority_at=now - timedelta(seconds=2), id=uuid4()),
        SimpleNamespace(asset_id=video, available_at=now - timedelta(seconds=1),
                        priority_at=now - timedelta(seconds=1), id=uuid4()),
    ]
    class Session:
        def __init__(self):
            self.rows = iter(rows)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def execute(self, _):
            row = next(self.rows)
            return SimpleNamespace(first=lambda: row)
    snapshot = {"status": "normal", "controller_mode": "normal",
                "budget": {"profiles": {"image_derive": {"allowed": True, "memory_reservation_bytes": 768 * 1024 ** 2}}},
                "memory": {"available_bytes": 656 * 1024 ** 2},
                "controller": {"hard_limits": {"memory_available_bytes": 256 * 1024 ** 2}}}
    async def pressure():
        return snapshot
    monkeypatch.setattr(media_derivatives, "async_session", Session)
    monkeypatch.setattr(resource_pressure, "get_resource_pressure_snapshot", pressure)
    monkeypatch.setattr(resource_pressure, "current_memory_available_bytes", lambda: snapshot["memory"]["available_bytes"])
    monkeypatch.setattr(heavy_io, "collect_active_resource_leases", lambda **_: {"active": [], "reserved_bytes": 0})
    return SimpleNamespace(image=image, video=video, rows=rows, snapshot=snapshot)


@pytest.mark.asyncio
async def test_older_image_does_not_hide_a_video_that_fits(media_candidates):
    from app.services.media_derivatives import _next_resource_candidate
    for _ in range(3):
        assert await _next_resource_candidate() == (media_candidates.video, "video_derive")


@pytest.mark.asyncio
async def test_real_runner_claims_fitting_video(media_candidates, monkeypatch):
    from app.services import heavy_io, media_derivatives
    claimed = []
    @asynccontextmanager
    async def slot(workload, *_args, **_kwargs):
        assert workload == "video_derive"
        yield object()
    async def claim(identity):
        claimed.append(identity)
        return {"asset_id": identity}
    async def process(_):
        return "processed"
    monkeypatch.setattr(heavy_io, "adaptive_resource_slice", slot)
    monkeypatch.setattr(media_derivatives, "_claim_one", claim)
    monkeypatch.setattr(media_derivatives, "_process_claimed_request", process)
    result = await media_derivatives.process_media_derivative_outbox(limit=1)
    assert claimed == [media_candidates.video]
    assert result["processed"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["memory", "critical"])
async def test_real_runner_registers_waiting_instead_of_empty(media_candidates, monkeypatch, denial):
    from app.services import media_derivatives, outbox_coordinator
    if denial == "memory":
        media_candidates.snapshot["memory"]["available_bytes"] = 556 * 1024 ** 2
    else:
        media_candidates.snapshot.update(status="paused", controller_mode="critical")
    flags = {}
    monkeypatch.setattr(outbox_coordinator, "get_redis", lambda: SimpleNamespace(hset=lambda _key, kind, token: flags.update({kind: token})))
    monkeypatch.setattr(outbox_coordinator, "clear_outbox_wake", lambda *_: None)
    monkeypatch.setattr(media_derivatives, "_claim_one", lambda *_: pytest.fail("Unadmitted work was claimed"))
    result = await media_derivatives.process_media_derivative_outbox(limit=1)
    outbox_coordinator.clear_and_wake_outbox_successor("media", result)
    assert result["resource_deferred"]
    assert result["claimed"] == 0
    assert flags.get("media")


@pytest.mark.asyncio
async def test_memory_recovery_makes_the_older_image_eligible(media_candidates):
    from app.services.media_derivatives import _next_resource_candidate
    assert await _next_resource_candidate() == (media_candidates.video, "video_derive")
    media_candidates.snapshot["memory"]["available_bytes"] = 1056 * 1024 ** 2
    assert await _next_resource_candidate() == (media_candidates.image, "image_derive")


@pytest.mark.asyncio
async def test_truly_empty_queue_does_not_register_waiting(media_candidates):
    from app.services import media_derivatives
    media_candidates.rows[:] = [None, None]
    result = await media_derivatives.process_media_derivative_outbox(limit=1)
    assert not result.get("resource_deferred")
    assert result["claimed"] == 0
