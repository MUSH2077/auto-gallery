from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt
from starlette.requests import Request

from app.auth import (
    ALGORITHM,
    create_access_token,
    decode_access_token,
    decode_access_token_payload,
    get_admin_key,
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
