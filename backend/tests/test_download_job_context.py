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


def test_download_job_text_filter_matches_enriched_creator_and_subscription_names():
    subscription_id = uuid4()
    creator_id = uuid4()
    repository_id = uuid4()
    matching = SimpleNamespace(id=uuid4(), subscription_id=subscription_id, source_url="https://example.test/a")

    class ScalarRows:
        def unique(self):
            return self

        def all(self):
            return [matching]

    class SearchResult:
        def scalars(self):
            return ScalarRows()

    class Session:
        def __init__(self):
            self.calls = 0

        async def get(self, _model, item_id):
            return SimpleNamespace(id=item_id)

        async def execute(self, _stmt):
            self.calls += 1
            if self.calls == 1:
                return SearchResult()
            return _FakeResult([
                (subscription_id, "Daily Pixiv", creator_id, "Atlas Ink", "atlas_ink"),
            ])

    service = DownloadService(Session())
    jobs = asyncio.run(service.list_jobs(subscription_source_id=str(repository_id), q="atlas"))

    assert jobs == [matching]
    assert matching.creator_name == "Atlas Ink"
