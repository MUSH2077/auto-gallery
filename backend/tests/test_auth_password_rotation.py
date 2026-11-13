from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio
import threading
import time

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
    hash_password,
    verify_password,
)
from app.config import settings


def _account(username="admin", must_change_password=False):
    return SimpleNamespace(id=7, credential_version=1, username=username, must_change_password=must_change_password)


def test_access_token_binds_account_identity_and_version() -> None:
    token = create_access_token(_account(must_change_password=True))
    payload = decode_access_token_payload(token)

    assert payload is not None
    assert payload.get("sub") == "admin"
    assert payload.get("uid") == 7
    assert payload.get("cv") == 1
    assert "password_hash" not in payload


@pytest.mark.asyncio
async def test_password_calculation_uses_at_most_two_workers(monkeypatch):
    from app import auth
    active = peak = 0
    lock = threading.Lock()
    loop_thread = threading.get_ident()
    def expensive_hash(_password):
        nonlocal active, peak
        assert threading.get_ident() != loop_thread
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.04)
            return "hashed"
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(auth, "hash_password", expensive_hash)
    assert await asyncio.gather(*[auth.async_hash_password("fixture") for _ in range(6)]) == ["hashed"] * 6
    assert peak == 2


def test_decode_access_token_still_returns_username() -> None:
    token = create_access_token(_account("alice"))

    assert decode_access_token(token) == "alice"


def test_hash_password_enforces_bcrypt_utf8_byte_limit() -> None:
    assert verify_password("a" * 72, hash_password("a" * 72))

    with pytest.raises(ValueError, match="at most 72 bytes"):
        hash_password("a" * 73)

    with pytest.raises(ValueError, match="at most 72 bytes"):
        hash_password("界" * 25)


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
async def test_admin_dependency_rejects_current_forced_password_change(monkeypatch) -> None:
    from app import auth
    user = _account()
    token = create_access_token(user)
    user.must_change_password = True
    async def load(_id):
        return user
    monkeypatch.setattr(auth, "_load_active_user_by_id", load)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with pytest.raises(HTTPException) as exc_info:
        await get_admin_key(_make_request("/api/v1/works"), credentials)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_dependency_accepts_normal_token(monkeypatch) -> None:
    from app import auth
    user = _account()
    async def load(_id):
        return user
    monkeypatch.setattr(auth, "_load_active_user_by_id", load)
    token = create_access_token(user)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    username = await get_admin_key(_make_request("/api/v1/works"), credentials)

    assert username == "admin"


@pytest.mark.asyncio
async def test_username_only_credentials_require_a_new_login():
    token = jwt.encode({"sub": "admin", "exp": datetime.now(timezone.utc) + timedelta(minutes=1)}, settings.secret_key, algorithm=ALGORITHM)
    with pytest.raises(HTTPException) as rejected:
        await get_admin_key(_make_request("/api/v1/auth/me"), HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
    assert rejected.value.status_code == 401


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
