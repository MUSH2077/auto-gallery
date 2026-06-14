import asyncio
from uuid import uuid4


class _CountResult:
    def scalar_one(self):
        return 0


class _Rows:
    def all(self):
        return []


class _RowsResult:
    def scalars(self):
        return _Rows()


def test_import_jobs_list_supports_download_job_and_text_filters():
    from app.api.import_jobs import list_import_jobs

    download_job_id = uuid4()
    seen_params = []

    class FakeDB:
        async def execute(self, stmt):
            seen_params.append(stmt.compile().params)
            if len(seen_params) == 1:
                return _CountResult()
            return _RowsResult()

    payload = asyncio.run(list_import_jobs(
        status="failed",
        download_job_id=download_job_id,
        q="boom",
        db=FakeDB(),
    ))

    assert payload == {"total": 0, "items": []}
    assert all("failed" in params.values() for params in seen_params)
    assert all(download_job_id in params.values() for params in seen_params)
    assert all("%boom%" in params.values() for params in seen_params)
