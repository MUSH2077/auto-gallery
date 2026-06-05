import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.services.download import DownloadService


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


def test_download_job_context_enrichment_adds_creator_and_subscription_fields():
    subscription_id = uuid4()
    creator_id = uuid4()
    job = SimpleNamespace(subscription_id=subscription_id)
    service = DownloadService(_FakeSession([
        (subscription_id, "Daily Pixiv", creator_id, "Atlas Ink", "atlas_ink"),
    ]))

    asyncio.run(service._enrich_job_context([job]))

    assert job.subscription_name == "Daily Pixiv"
    assert job.creator_id == creator_id
    assert job.creator_name == "Atlas Ink"
