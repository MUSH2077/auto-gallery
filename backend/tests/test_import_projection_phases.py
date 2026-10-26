from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from app.jobs import import_projection as job
from app.services import import_projection as service, heavy_io, redis_client, outbox_coordinator


@pytest.mark.parametrize("allow_metadata", [True, False])
async def test_curation_commits_and_wakes_before_metadata_admission(monkeypatch, allow_metadata):
    events = []
    class Slice:
        def __init__(self, lane): self.lane = lane
        async def __aenter__(self):
            events.append(self.lane)
            return SimpleNamespace(work_units=25,slice_seconds=20) if self.lane == "ingest" or allow_metadata else None
        async def __aexit__(self, *args): events.append(self.lane+"-released")
    monkeypatch.setattr(heavy_io,"adaptive_resource_slice",lambda *a,**k:Slice(k["lane"]))
    monkeypatch.setattr(heavy_io,"background_turn_available",lambda r:True)
    monkeypatch.setattr(redis_client,"get_redis",lambda:object())
    async def process(**kwargs):
        events.append(kwargs["projection_part"]+"-committed")
        return {"claimed":1,"claimed_ids":["one"],"processed":1,"failed":0,"more_likely":False,
                "metadata_batches":1,"metadata_processed":1,"metadata_failed":0}
    monkeypatch.setattr(service,"process_import_projection_outbox",process)
    monkeypatch.setattr(outbox_coordinator,"wake_pending_outboxes",lambda c:events.append("search-wake"))
    result = await job._run_projection(25,20,{})
    assert events.index("curation-committed") < events.index("ingest-released") < events.index("search-wake") < events.index("background")
    assert result["processed"] == 1
    if not allow_metadata: assert result["resource_deferred"] is True


async def test_ingest_priority_denial_does_not_claim_curation(monkeypatch):
    monkeypatch.setattr(heavy_io,"background_turn_available",lambda r:False)
    monkeypatch.setattr(redis_client,"get_redis",lambda:object())
    process=AsyncMock();monkeypatch.setattr(service,"process_import_projection_outbox",process)
    result=await job._run_projection(25,20,{})
    assert result["resource_deferred"] is True
    process.assert_not_awaited()
