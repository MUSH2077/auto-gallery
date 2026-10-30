from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from app.jobs import import_projection as job
from app.services import import_projection as service, heavy_io, redis_client, outbox_coordinator
from tests.test_bulk_import_wake import bulk_environment


async def test_completion_curation_runs_before_background_turn(monkeypatch):
    monkeypatch.setattr(heavy_io,"background_turn_available",lambda r:False)
    monkeypatch.setattr(redis_client,"get_redis",lambda:object())
    class Slice:
        async def __aenter__(self):
            return SimpleNamespace(work_units=25, slice_seconds=20)
        async def __aexit__(self, *args):
            return None
    seen_lanes=[]
    monkeypatch.setattr(heavy_io,"adaptive_resource_slice",lambda *a,**k:(seen_lanes.append(k["lane"]) or Slice()))
    process=AsyncMock(return_value={
        "claimed":1,"claimed_ids":["one"],"processed":1,"failed":0,
        "more_likely":False,"metadata_batches":0,"metadata_processed":0,
        "metadata_failed":0,
    })
    monkeypatch.setattr(service,"process_import_projection_outbox",process)
    monkeypatch.setattr(outbox_coordinator,"wake_pending_outboxes",lambda _:None)
    result=await job._run_projection(25,20,{})
    assert result["resource_deferred"] is True
    process.assert_awaited_once()
    assert process.await_args.kwargs["projection_part"] == "curation"
    assert seen_lanes == ["completion"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_projection_commit_has_one_search_wake_and_metadata_can_defer(
    bulk_environment, monkeypatch,
):
    """A committed curation wakes search once even when metadata must wait.

    The assertion is intentionally one wake: while the old runner still has
    its explicit post-curation wake, this test fails with two calls; the
    production simplification should leave only the transaction after_commit
    publication.
    """
    from sqlalchemy import select

    from app.jobs import import_projection as projection_job
    from app.models import ImportCurationOutbox
    from app.services import heavy_io, outbox_coordinator
    from tests.test_bulk_import_wake import _persist, _staged_work

    db, library = bulk_environment
    staged = await _staged_work(db, library)
    await db.commit()
    setup_wakes = []
    monkeypatch.setattr(
        outbox_coordinator,
        "wake_pending_outboxes",
        lambda counts, **_: setup_wakes.append(dict(counts)),
    )
    # Persist the real ImportCurationOutbox row, then ignore its setup wake.
    await _persist(db, [staged])
    await db.commit()
    setup_wakes.clear()
    search_wakes = []
    monkeypatch.setattr(
        outbox_coordinator,
        "wake_pending_outboxes",
        lambda counts, **_: search_wakes.append(dict(counts)),
    )

    class Slice:
        def __init__(self, lane): self.lane = lane
        async def __aenter__(self):
            if self.lane == "completion":
                return SimpleNamespace(work_units=25, slice_seconds=20)
            return None
        async def __aexit__(self, *args): return None

    monkeypatch.setattr(
        heavy_io, "adaptive_resource_slice",
        lambda *args, **kwargs: Slice(kwargs["lane"]),
    )
    monkeypatch.setattr(heavy_io, "background_turn_available", lambda _redis: True)
    result = await projection_job._run_projection(25, 20, {})

    assert result["processed"] == 1
    assert result["resource_deferred"] is True
    assert result["deferred_projection_part"] == "metadata"
    assert sum(int(counts.get("search") or 0) for counts in search_wakes) == 1
    assert any("gitllery" in counts for counts in search_wakes), "preserve unrelated commit wake"
    state = await db.scalar(
        select(ImportCurationOutbox.state).where(
            ImportCurationOutbox.work_id == staged["work_id"]
        )
    )
    assert state == "complete"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_projection_rollback_does_not_wake_search(
    bulk_environment, monkeypatch,
):
    """A failed real curation transaction publishes no search wake."""
    from sqlalchemy import select

    from app.models import ImportCurationOutbox
    from app.services import curation, outbox_coordinator
    from app.services.import_projection import _claim_batch, _project
    from tests.test_bulk_import_wake import _persist, _staged_work

    db, library = bulk_environment
    staged = await _staged_work(db, library)
    await db.commit()
    monkeypatch.setattr(outbox_coordinator, "wake_pending_outboxes", lambda *_a, **_k: None)
    await _persist(db, [staged])
    await db.commit()
    payload = await _claim_batch(25)
    assert payload is not None
    search_wakes = []
    monkeypatch.setattr(
        outbox_coordinator,
        "wake_pending_outboxes",
        lambda counts, **_: search_wakes.append(dict(counts)),
    )

    async def fail_record(*_args, **_kwargs):
        raise RuntimeError("injected curation transaction failure")

    monkeypatch.setattr(curation.CurationService, "record_imported_works", fail_record)
    with pytest.raises(RuntimeError, match="injected curation transaction failure"):
        await _project(payload)
    assert search_wakes == []
    state = await db.scalar(
        select(ImportCurationOutbox.state).where(
            ImportCurationOutbox.work_id == staged["work_id"]
        )
    )
    assert state == "processing"
