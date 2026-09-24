"""Behavioral contract for revocable browser sessions."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app import auth
from app.api import auth_api
from app.config import settings
from app.services import browser_sessions


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.fail = False

    async def set(self, key, value, *, ex, nx):
        if self.fail:
            raise browser_sessions.RedisError("unavailable")
        if nx and key in self.values:
            return False
        self.values[key] = value.encode()
        return True

    async def get(self, key):
        if self.fail:
            raise browser_sessions.RedisError("unavailable")
        return self.values.get(key)

    async def delete(self, key):
        if self.fail:
            raise browser_sessions.RedisError("unavailable")
        return self.values.pop(key, None) is not None

    async def aclose(self):
        pass


def _user():
    return SimpleNamespace(
        id=7, username="alice", password_hash="hash-1", is_active=True,
        is_admin=False, permissions=["tasks"], must_change_password=False,
        display_name="Alice", preferences={}, nsfw_visible=False,
        upload_quota_bytes=0, upload_used_bytes=0,
    )


def _request(method="GET", *, token=None, csrf=None, origin=None):
    headers = []
    if token:
        headers.append((b"cookie", f"ag_session={token}; ag_csrf={csrf or ''}".encode()))
    if csrf:
        headers.append((b"x-csrf-token", csrf.encode()))
    if origin:
        headers.append((b"origin", origin.encode()))
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": "/api/v1/tasks", "query_string": b"",
        "headers": headers, "client": ("testclient", 1234), "server": ("testserver", 80),
    })


@pytest.fixture
def store(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(browser_sessions, "_client", fake)
    monkeypatch.setattr(settings, "browser_session_origins", "http://test,https://test")
    return fake


@pytest.mark.asyncio
async def test_session_is_revocable_and_fails_closed(store, monkeypatch):
    user = _user()
    monkeypatch.setattr(auth, "_load_active_user_by_id", lambda user_id: _async_value(user))
    token, session = await browser_sessions.create_browser_session(user)
    assert await auth.get_admin_key(_request(token=token), None) == "alice"
    assert await auth.get_admin_key(
        _request("POST", token=token, csrf=session.csrf, origin="http://test"), None
    ) == "alice"
    for request in (
        _request("POST", token=token, origin="http://test"),
        _request("POST", token=token, csrf=session.csrf, origin="http://evil"),
        _request(token=token, origin="http://evil"),
    ):
        with pytest.raises(HTTPException) as rejected:
            await auth.get_admin_key(request, None)
        assert rejected.value.status_code == 403
    bearer = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid")
    with pytest.raises(HTTPException) as rejected:
        await auth.get_admin_key(_request(token=token), bearer)
    assert rejected.value.status_code == 401

    user.password_hash = "hash-2"
    with pytest.raises(HTTPException) as rejected:
        await auth.get_admin_key(_request(token=token), None)
    assert rejected.value.status_code == 401
    await browser_sessions.revoke_browser_session(token)
    assert await browser_sessions.load_browser_session(token) is None

    store.fail = True
    with pytest.raises(HTTPException) as unavailable:
        await auth.get_admin_key(_request(token=token), None)
    assert unavailable.value.status_code == 503


async def _async_value(value):
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,secure", [("http://test", False), ("https://test", True)])
async def test_browser_login_cookie_csrf_logout(store, monkeypatch, origin, secure):
    from app.main import app
    user = _user()

    async def fake_authenticate(*_args):
        return user

    async def fake_lookup(_id):
        return user

    monkeypatch.setattr(auth_api, "_authenticate_login", fake_authenticate)
    monkeypatch.setattr(auth, "_load_active_user_by_id", fake_lookup)
    monkeypatch.setattr(auth_api, "issue_ws_ticket", lambda username, token=None: ("one-use", 30))
    async with AsyncClient(transport=ASGITransport(app=app), base_url=origin) as client:
        login = await client.post(
            "/api/v1/auth/browser/login", json={"username": "alice", "password": "pw"},
            headers={"Origin": origin},
        )
        assert login.status_code == 200
        assert "access_token" not in login.json()
        cookies = login.headers.get_list("set-cookie")
        session_cookie = next(value for value in cookies if value.startswith("ag_session="))
        assert "HttpOnly" in session_cookie
        assert "SameSite=lax" in session_cookie
        assert "Path=/" in session_cookie
        assert ("Secure" in session_cookie) is secure
        csrf = client.cookies.get("ag_csrf")
        assert csrf
        assert (await client.get("/api/v1/auth/me")).status_code == 200
        user.is_admin = True
        assert (await client.get("/api/openapi.json")).status_code == 200
        user.is_admin = False

        missing = await client.post("/api/v1/auth/ws-ticket", headers={"Origin": origin})
        assert missing.status_code == 403
        cross_origin = await client.post(
            "/api/v1/auth/ws-ticket",
            headers={"Origin": "http://evil", "X-CSRF-Token": csrf},
        )
        assert cross_origin.status_code == 403
        granted = await client.post(
            "/api/v1/auth/ws-ticket",
            headers={"Origin": origin, "X-CSRF-Token": csrf},
        )
        assert granted.status_code == 200
        assert granted.json()["ticket"] == "one-use"
        logout = await client.post(
            "/api/v1/auth/browser/logout",
            headers={"Origin": origin, "X-CSRF-Token": csrf},
        )
        assert logout.status_code == 200
        assert (await client.get("/api/v1/auth/me")).status_code == 401



@pytest.mark.asyncio
async def test_browser_upload_requires_permission_and_session_csrf(store, monkeypatch, tmp_path):
    from app.main import app
    from app.services.manual_upload import ManualUploadService

    user = _user()
    user.permissions = ["upload"]

    async def fake_authenticate(*_args):
        return user

    async def fake_lookup(_id):
        return user

    async def fake_save_upload(_self, _db, _user, _files, _metadata):
        return SimpleNamespace(
            work_id="work-1", download_job_id="download-1", import_job_id=None,
            used_bytes=3, quota_bytes=None,
        )

    monkeypatch.setattr(auth_api, "_authenticate_login", fake_authenticate)
    monkeypatch.setattr(auth, "_load_active_user_by_id", fake_lookup)
    monkeypatch.setattr(ManualUploadService, "save_upload", fake_save_upload)
    monkeypatch.setattr(settings, "download_root", str(tmp_path))

    origin = "http://test"
    async with AsyncClient(transport=ASGITransport(app=app), base_url=origin) as client:
        login = await client.post(
            "/api/v1/auth/browser/login", json={"username": "alice", "password": "pw"},
            headers={"Origin": origin},
        )
        assert login.status_code == 200
        files = {"files": ("probe.jpg", b"abc", "image/jpeg")}
        csrf = client.cookies.get("ag_csrf")
        assert (await client.post("/api/v1/upload", files=files, headers={"Origin": origin})).status_code == 403
        assert (await client.post(
            "/api/v1/upload", files=files,
            headers={"Origin": "http://evil", "X-CSRF-Token": csrf},
        )).status_code == 403
        user.permissions = []
        assert (await client.post(
            "/api/v1/upload", files=files,
            headers={"Origin": origin, "X-CSRF-Token": csrf},
        )).status_code == 403
        user.permissions = ["upload"]
        accepted = await client.post(
            "/api/v1/upload", files=files,
            headers={"Origin": origin, "X-CSRF-Token": csrf},
        )
        assert accepted.status_code == 200
        assert accepted.json()["work_id"] == "work-1"


def test_origin_rejects_invalid_values(store):
    for origin in (None, "https://evil", "https://test/"):
        with pytest.raises(HTTPException) as rejected:
            browser_sessions.browser_origin(_request(origin=origin))
        assert rejected.value.status_code == 403


@pytest.mark.asyncio
async def test_websocket_requires_origin_and_live_single_use_session(store, monkeypatch):
    from fastapi import WebSocketDisconnect
    from app.api import ws as ws_api
    from app.services.ws_tickets import BrowserTicket

    user = _user()
    token, _ = await browser_sessions.create_browser_session(user)
    monkeypatch.setattr(ws_api, "consume_ws_ticket", lambda value: BrowserTicket("alice", token) if value == "ticket" else None)
    monkeypatch.setattr(ws_api, "_load_active_user_by_id", lambda _id: _async_value(user))

    class Manager:
        def __init__(self):
            self.connected = 0
        async def is_current_tasks_user(self, _username):
            return True
        async def connect(self, *_args, **_kwargs):
            self.connected += 1
        async def disconnect(self, *_args):
            pass

    class Socket:
        def __init__(self, origin=None, ticket=None):
            self.headers = {"origin": origin} if origin else {}
            self.query_params = {"ticket": ticket} if ticket else {}
            self.cookies = {"ag_token": "old-jwt"}
            self.closed = []
        async def close(self, *, code, reason):
            self.closed.append(code)
        async def receive_json(self):
            raise WebSocketDisconnect()

    manager = Manager()
    monkeypatch.setattr(ws_api, "manager", manager)
    missing_origin = Socket(ticket="ticket")
    await ws_api.websocket_endpoint(missing_origin)
    assert missing_origin.closed == [4003]

    cookie_only = Socket(origin="http://test")
    await ws_api.websocket_endpoint(cookie_only)
    assert cookie_only.closed == [4001]

    valid = Socket(origin="http://test", ticket="ticket")
    await ws_api.websocket_endpoint(valid)
    assert manager.connected == 1

    class PasswordChanged(Socket):
        async def receive_json(self):
            user.password_hash = "hash-changed"
            return {"action": "ping"}
        async def send_json(self, _data):
            pass

    changed = PasswordChanged(origin="http://test", ticket="ticket")
    await ws_api.websocket_endpoint(changed)
    assert changed.closed == [4001]
    user.password_hash = "hash-1"

    await browser_sessions.revoke_browser_session(token)
    revoked = Socket(origin="http://test", ticket="ticket")
    await ws_api.websocket_endpoint(revoked)
    assert revoked.closed == [4001]


@pytest.mark.asyncio
async def test_bearer_and_browser_login_share_rate_limiter(store, monkeypatch):
    from app.main import app
    calls = []

    class DenyLimiter:
        async def check(self, ip):
            calls.append(ip)
            return False, 0, 60

    monkeypatch.setattr(auth_api, "_login_limiter", DenyLimiter())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/api/v1/auth/login", "/api/v1/auth/browser/login"):
            response = await client.post(
                path, json={"username": "alice", "password": "bad"},
                headers={"Origin": "http://test"},
            )
            assert response.status_code == 429
            assert response.headers["retry-after"] == "60"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_password_change_rotates_browser_session(store, monkeypatch):
    from app.main import app
    user = _user()

    async def fake_authenticate(*_args):
        return user

    async def fake_lookup(_id):
        return user

    async def fake_change(_body, current_user, _db):
        current_user.password_hash = "hash-2"
        current_user.must_change_password = False
        return current_user

    monkeypatch.setattr(auth_api, "_authenticate_login", fake_authenticate)
    monkeypatch.setattr(auth_api, "_change_password", fake_change)
    monkeypatch.setattr(auth, "_load_active_user_by_id", fake_lookup)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/api/v1/auth/browser/login", json={"username": "alice", "password": "pw"},
            headers={"Origin": "http://test"},
        )
        assert login.status_code == 200
        old_token = client.cookies.get("ag_session")
        old_csrf = client.cookies.get("ag_csrf")
        changed = await client.post(
            "/api/v1/auth/browser/change-password",
            json={"current_password": "pw", "new_password": "new-password"},
            headers={"Origin": "http://test", "X-CSRF-Token": old_csrf},
        )
        assert changed.status_code == 200
        assert "access_token" not in changed.json()
        assert client.cookies.get("ag_session") != old_token
        assert client.cookies.get("ag_csrf") != old_csrf
        assert await browser_sessions.load_browser_session(old_token) is None
        assert (await client.get("/api/v1/auth/me")).status_code == 200
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        cookies={"ag_session": old_token, "ag_csrf": old_csrf},
    ) as stale:
        assert (await stale.get("/api/v1/auth/me")).status_code == 401
