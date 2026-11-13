"""Dedup waits use the same durable recovery mechanism as media work."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from tests.test_bulk_import_wake import bulk_environment
from tests.test_resource_release_wake import waiting_work


@pytest.mark.parametrize("denial", ["profile", "slot", "empty"])
def test_dedup_distinguishes_resource_denial_from_no_work(monkeypatch, denial):
    from app.jobs import asset_dedup
    from app.services import outbox_coordinator
    async def limits(*_args, **_kwargs):
        return SimpleNamespace(allowed=denial != "profile"), {}
    @asynccontextmanager
    async def slot(*_):
        yield None if denial == "slot" else object()
    async def claim():
        assert denial == "empty", "Blocked work must never be claimed"
        return None
    flags = {}
    monkeypatch.setattr(asset_dedup, "current_profile_slice_limits", limits)
    monkeypatch.setattr(asset_dedup, "try_heavy_io_slot", slot)
    monkeypatch.setattr(asset_dedup, "_claim_outbox_event", claim)
    monkeypatch.setattr(outbox_coordinator, "get_redis", lambda: SimpleNamespace(hset=lambda _key, kind, token: flags.update({kind: token})))
    monkeypatch.setattr(outbox_coordinator, "clear_outbox_wake", lambda *_: None)
    result = asset_dedup.run_asset_dedup_outbox(limit=1)
    assert result["processed"] == 0
    assert result["resource_deferred"] == (denial != "empty")
    assert bool(flags.get("dedup")) == (denial != "empty")


@pytest.mark.integration
def test_image_release_requeues_real_waiting_dedup_once(waiting_work, monkeypatch):
    from app.database import async_session
    from app.jobs.asset_dedup import run_asset_dedup_outbox
    from app.models import AssetDedupOutbox
    from app.services import heavy_io, outbox_coordinator
    from rq import Queue
    from rq.job import Job

    identity = uuid4()
    async def stage():
        async with async_session() as db:
            db.add(AssetDedupOutbox(id=identity, idempotency_key="recovery-test-"+str(identity),
                                   event_type="observe", payload={"asset_id":str(waiting_work.staged["derivative_requests"][0]["asset_id"])}, state="pending", available_at=datetime.now(timezone.utc)))
            await db.commit()
    async def cleanup():
        async with async_session() as db:
            await db.execute(delete(AssetDedupOutbox).where(AssetDedupOutbox.id==identity))
            await db.commit()
    asyncio.run(stage())
    # A shared media holder blocks dedup, which needs the exclusive counterpart.
    image = heavy_io.LocalHeavyIOLock(waiting_work.root.with_name("profile-media.lock"), shared=True)
    assert image.try_acquire()
    client = waiting_work.client
    monkeypatch.setattr(outbox_coordinator, "wake_pending_outboxes", waiting_work.real_wake)
    queue = Queue("operations", connection=client)
    owner = None
    try:
        result = run_asset_dedup_outbox(limit=1)
        assert result["resource_deferred"] and result["processed"] == 0
        assert client.hget(outbox_coordinator.RESOURCE_DEFERRED_KEY,"dedup")
        assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 0
        image.release()
        assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 1
        owner = client.get("outbox:wakeup:dedup").decode()
        assert queue.job_ids.count(owner)==1
        assert Job.fetch(owner,connection=client).func_name.endswith("run_asset_dedup_outbox")
        assert outbox_coordinator.wake_resource_deferred_outboxes()["enqueued"] == 0
    finally:
        image.release()
        if owner:
            queue.remove(owner);Job.fetch(owner,connection=client).delete()
        client.delete("outbox:wakeup:dedup")
        asyncio.run(cleanup())
