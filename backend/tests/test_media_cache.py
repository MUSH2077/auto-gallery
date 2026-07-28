import pytest


@pytest.mark.asyncio
async def test_thumb_sets_cache_control(tmp_path, monkeypatch):
    from fastapi.responses import FileResponse
    from app.api import media

    f = tmp_path / "t.webp"
    f.write_bytes(b"fake-webp")

    async def fake_serve(asset_id, size):
        assert size == "thumb"
        return FileResponse(str(f), media_type="image/webp")

    monkeypatch.setattr(media, "_serve", fake_serve)

    resp = await media.thumb("any-asset-id")
    assert resp.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.asyncio
async def test_serve_does_not_add_cache_control():
    # Guards that preview/original (which call _serve directly) stay uncached.
    from app.api import media
    import inspect

    src = inspect.getsource(media._serve)
    assert "Cache-Control" not in src and "cache-control" not in src
