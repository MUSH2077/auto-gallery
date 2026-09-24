import pytest


@pytest.mark.asyncio
async def test_thumb_sets_cache_control(tmp_path, monkeypatch):
    from fastapi import Request
    from fastapi.responses import FileResponse
    from app.api import media

    f = tmp_path / "t.webp"
    f.write_bytes(b"fake-webp")

    async def fake_serve(asset_id, size):
        assert size == "thumb"
        return FileResponse(str(f), media_type="image/webp")

    monkeypatch.setattr(media, "_serve", fake_serve)

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    resp = await media.thumb("any-asset-id", request)
    assert resp.headers["cache-control"] == "private, max-age=86400"
    assert resp.headers["etag"].startswith('"any-asset-id-thumb-')


@pytest.mark.asyncio
async def test_thumb_returns_not_modified_for_matching_etag(tmp_path, monkeypatch):
    from fastapi import Request
    from fastapi.responses import FileResponse
    from app.api import media

    f = tmp_path / "t.webp"
    f.write_bytes(b"fake-webp")

    async def fake_serve(asset_id, size):
        return FileResponse(str(f), media_type="image/webp")

    monkeypatch.setattr(media, "_serve", fake_serve)
    first = await media.thumb(
        "any-asset-id",
        Request({"type": "http", "method": "GET", "path": "/", "headers": []}),
    )
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"if-none-match", first.headers["etag"].encode())],
    })

    resp = await media.thumb("any-asset-id", request)

    assert resp.status_code == 304
    assert resp.headers["cache-control"] == "private, max-age=86400"
    assert resp.headers["etag"] == first.headers["etag"]


@pytest.mark.asyncio
async def test_thumb_conditional_request_still_verifies_resource(monkeypatch):
    from fastapi import HTTPException, Request
    from app.api import media

    async def missing(_asset_id, _size):
        raise HTTPException(status_code=404, detail="Asset not found")

    monkeypatch.setattr(media, "_serve", missing)
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"if-none-match", b'"missing-thumb"')],
    })

    with pytest.raises(HTTPException) as error:
        await media.thumb("missing", request)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_serve_does_not_add_cache_control():
    # Guards that preview/original (which call _serve directly) stay uncached.
    from app.api import media
    import inspect

    src = inspect.getsource(media._serve)
    assert "Cache-Control" not in src and "cache-control" not in src


def test_stream_supports_http_ranges(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.testclient import TestClient

    from app.api import media
    from app.services.media_signing import sign_media_token

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"0123456789" * 100)

    async def fake_serve(asset_id, size):
        assert asset_id == "asset-video"
        assert size == "original"
        return FileResponse(source, media_type="video/mp4")

    monkeypatch.setattr(media, "_serve", fake_serve)
    expires = 4_102_444_800
    token = sign_media_token("asset-video", "stream", expires)
    app = FastAPI()
    app.include_router(media.router)
    response = TestClient(app).get(
        f"/media/stream/asset-video?expires={expires}&token={token}",
        headers={"Range": "bytes=10-19"},
    )
    assert response.status_code == 206
    assert response.content == b"0123456789"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == "bytes 10-19/1000"
    assert response.headers["cache-control"] == "private, no-store"


def test_stream_rejects_expired_and_tampered_tickets():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media
    from app.services.media_signing import sign_media_token

    app = FastAPI()
    app.include_router(media.router)
    client = TestClient(app)
    expired = 1
    expired_token = sign_media_token("asset-video", "stream", expired)
    assert client.get(
        f"/media/stream/asset-video?expires={expired}&token={expired_token}",
    ).status_code == 401
    assert client.get(
        "/media/stream/asset-video?expires=4102444800&token=tampered",
    ).status_code == 401
