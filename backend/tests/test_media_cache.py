import pytest

from app.services.media_signing import sign_media_token


@pytest.mark.parametrize("credential", ["anonymous", "expired_session", "invalid", "expired", "wrong_asset", "wrong_purpose", "signed", "session"])
def test_thumb_authenticates_before_reading_or_requesting_derivatives(tmp_path, monkeypatch, credential):
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.testclient import TestClient
    from app.api import media
    from app import auth
    from app.services import browser_sessions
    from types import SimpleNamespace
    import time
    user = SimpleNamespace(id=1, username="fixture", password_hash="fixture", is_active=True, must_change_password=False)
    async def load_user(_id):
        return user
    async def session(token):
        if credential == "session" and token == "fixture":
            return SimpleNamespace(user_id=1, password_fingerprint=browser_sessions.password_fingerprint(user.password_hash))
        return None
    monkeypatch.setattr(auth, "load_browser_session", session)
    monkeypatch.setattr(auth, "_load_active_user_by_id", load_user)
    calls = []
    image = tmp_path / "fixture.webp"
    image.write_bytes(b"fixture image")
    async def serve(asset, size):
        calls.append((asset, size))
        return FileResponse(image, media_type="image/webp")
    monkeypatch.setattr(media, "_serve", serve)
    app = FastAPI()
    app.include_router(media.router)
    expires = int(time.time()) + 60 if credential != "expired" else int(time.time()) - 1
    params = {}
    if credential not in {"anonymous", "session", "expired_session"}:
        params = {"expires": expires, "token": "invalid" if credential == "invalid" else sign_media_token(
            "other" if credential == "wrong_asset" else "fixture", "preview" if credential == "wrong_purpose" else "thumb", expires)}
    client = TestClient(app)
    if credential in {"session", "expired_session"}:
        client.cookies.set("ag_session", "fixture")
    result = client.get("/media/thumb/fixture", params=params)
    if credential in {"signed", "session"}:
        assert result.status_code == 200
        assert result.content == b"fixture image"
        assert calls == [("fixture", "thumb")]
    else:
        assert result.status_code == 401
        assert calls == []


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
    resp = await media.thumb("any-asset-id", request, "4102444800", sign_media_token("any-asset-id", "thumb", 4102444800), None)
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
        "4102444800", sign_media_token("any-asset-id", "thumb", 4102444800), None,
    )
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"if-none-match", first.headers["etag"].encode())],
    })

    resp = await media.thumb("any-asset-id", request, "4102444800", sign_media_token("any-asset-id", "thumb", 4102444800), None)

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
        await media.thumb("missing", request, "4102444800", sign_media_token("missing", "thumb", 4102444800), None)
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
