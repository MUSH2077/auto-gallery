import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _DB:
    def __init__(self, asset):
        self.asset = asset

    async def execute(self, _statement):
        return _Result(self.asset)


def _install_repo(monkeypatch, work):
    from app.api import works

    class FakeRepository:
        def __init__(self, _db):
            pass

        async def get(self, _work_id, force_sfw=False):
            return work

    monkeypatch.setattr(works, "WorkRepository", FakeRepository)


def test_playback_ticket_is_asset_scoped_and_expiring(monkeypatch):
    from app.api import works
    from app.services.media_signing import verify_media_token

    work_id = uuid4()
    asset_id = uuid4()
    _install_repo(monkeypatch, SimpleNamespace(id=work_id))
    asset = SimpleNamespace(id=asset_id, mime_type="video/mp4", file_name="clip.mp4")
    result = asyncio.run(works.create_playback_ticket(
        work_id,
        asset_id,
        user=SimpleNamespace(nsfw_visible=True),
        db=_DB(asset),
    ))
    parsed = urlparse(result.url)
    query = parse_qs(parsed.query)
    assert parsed.path == f"/media/stream/{asset_id}"
    assert verify_media_token(
        str(asset_id),
        "stream",
        query["expires"][0],
        query["token"][0],
    )
    assert result.expires_at.isoformat()


def test_playback_ticket_rejects_non_video(monkeypatch):
    from app.api import works

    work_id = uuid4()
    asset_id = uuid4()
    _install_repo(monkeypatch, SimpleNamespace(id=work_id))
    asset = SimpleNamespace(id=asset_id, mime_type="image/png", file_name="still.png")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(works.create_playback_ticket(
            work_id,
            asset_id,
            user=SimpleNamespace(nsfw_visible=True),
            db=_DB(asset),
        ))
    assert exc.value.status_code == 422


def test_playback_ticket_rejects_video_outside_mp4_webm_scope(monkeypatch):
    from app.api import works

    work_id = uuid4()
    asset_id = uuid4()
    _install_repo(monkeypatch, SimpleNamespace(id=work_id))
    asset = SimpleNamespace(id=asset_id, mime_type="video/x-matroska", file_name="clip.mkv")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(works.create_playback_ticket(
            work_id,
            asset_id,
            user=SimpleNamespace(nsfw_visible=True),
            db=_DB(asset),
        ))
    assert exc.value.status_code == 422


def test_playback_ticket_hides_inaccessible_work(monkeypatch):
    from app.api import works

    _install_repo(monkeypatch, None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(works.create_playback_ticket(
            uuid4(),
            uuid4(),
            user=SimpleNamespace(nsfw_visible=False),
            db=_DB(None),
        ))
    assert exc.value.status_code == 404
