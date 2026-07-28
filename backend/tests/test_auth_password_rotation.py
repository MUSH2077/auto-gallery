from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import jwt
from starlette.requests import Request

from app.auth import (
    ALGORITHM,
    create_access_token,
    decode_access_token,
    decode_access_token_payload,
    ensure_admin_user,
    get_admin_key,
    verify_password,
)
from app.config import settings


def test_access_token_contains_password_rotation_flag() -> None:
    token = create_access_token("admin", must_change_password=True)
    payload = decode_access_token_payload(token)

    assert payload is not None
    assert payload.get("sub") == "admin"
    assert payload.get("pwd_chg_required") is True


def test_decode_access_token_still_returns_username() -> None:
    token = create_access_token("alice", must_change_password=False)

    assert decode_access_token(token) == "alice"


def _make_request(path: str) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_admin_dependency_rejects_missing_token() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_admin_key(_make_request("/api/v1/works"), None)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_admin_dependency_rejects_invalid_token() -> None:
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-jwt")

    with pytest.raises(HTTPException) as exc_info:
        await get_admin_key(_make_request("/api/v1/works"), credentials)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_admin_dependency_rejects_expired_token() -> None:
    expired_token = jwt.encode(
        {
            "sub": "admin",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
            "pwd_chg_required": False,
        },
        settings.secret_key,
        algorithm=ALGORITHM,
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired_token)

    with pytest.raises(HTTPException) as exc_info:
        await get_admin_key(_make_request("/api/v1/works"), credentials)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_admin_dependency_rejects_force_password_change_token() -> None:
    token = create_access_token("admin", must_change_password=True)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with pytest.raises(HTTPException) as exc_info:
        await get_admin_key(_make_request("/api/v1/works"), credentials)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_dependency_accepts_normal_token() -> None:
    token = create_access_token("admin", must_change_password=False)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    username = await get_admin_key(_make_request("/api/v1/works"), credentials)

    assert username == "admin"


@pytest.mark.asyncio
async def test_bootstrap_admin_uses_configured_default_password(monkeypatch) -> None:
    import app.database

    class _Scalars:
        def first(self):
            return None

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Session:
        def __init__(self):
            self.added = []
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, statement):
            return _Result()

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            self.commits += 1

    session = _Session()

    monkeypatch.setattr(settings, "admin_password", "change-me-admin")
    monkeypatch.setattr(app.database, "async_session", lambda: session)

    await ensure_admin_user()

    assert len(session.added) == 1
    admin = session.added[0]
    assert admin.username == "admin"
    assert verify_password("change-me-admin", admin.password_hash)
    assert admin.must_change_password is True
    assert admin.is_admin is True
    assert session.commits == 1
