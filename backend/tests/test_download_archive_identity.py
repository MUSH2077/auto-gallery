"""The download admission must fail closed when archive identity is unknown."""

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_archive_identity_resolution_does_not_fallback_to_job_id(monkeypatch):
    from app.jobs import download

    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *_args):
            return None

    from app.services.heavy_io import HeavyIOUnavailable

    monkeypatch.setattr(download, "async_session", lambda: BrokenSession())
    job_id = str(uuid4())
    with pytest.raises(HeavyIOUnavailable, match="download_archive_identity_unavailable"):
        await download._download_source_identity(job_id)
