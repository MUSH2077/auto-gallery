"""Tests for /api/v1/users CRUD API and its admin-only guards."""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

PREFIX = "api_test_"


async def _clear(db):
    await db.execute(text(f"DELETE FROM users WHERE username LIKE '{PREFIX}%'"))
    await db.commit()


async def _seed_user(db, username, *, is_admin=False, is_active=True, permissions=None, password="hunter22"):
    from app.auth import hash_password
    from app.models.user import User

    user = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=is_admin,
        is_active=is_active,
        permissions=permissions or [],
        must_change_password=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _headers(username):
    from app.auth import create_access_token

    token = create_access_token(username, must_change_password=False)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_can_list_create_get_and_patch_users():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            admin = await _seed_user(db, f"{PREFIX}admin", is_admin=True)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = _headers(admin.username)

            r = await client.get("/api/v1/users", headers=headers)
            assert r.status_code == 200
            assert any(u["username"] == admin.username for u in r.json())

            r = await client.post(
                "/api/v1/users",
                json={"username": f"{PREFIX}newuser", "password": "hunter22", "permissions": ["library"]},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            created = r.json()
            assert created["username"] == f"{PREFIX}newuser"
            assert created["permissions"] == ["library"]
            assert created["must_change_password"] is True
            user_id = created["id"]

            # Duplicate username -> 409
            r = await client.post(
                "/api/v1/users",
                json={"username": f"{PREFIX}newuser", "password": "hunter22"},
                headers=headers,
            )
            assert r.status_code == 409

            r = await client.get(f"/api/v1/users/{user_id}", headers=headers)
            assert r.status_code == 200
            assert r.json()["username"] == f"{PREFIX}newuser"

            r = await client.get("/api/v1/users/999999999", headers=headers)
            assert r.status_code == 404

            r = await client.patch(
                f"/api/v1/users/{user_id}",
                json={"display_name": "New Name", "nsfw_visible": False},
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json()["display_name"] == "New Name"
            assert r.json()["nsfw_visible"] is False

            r = await client.patch(
                f"/api/v1/users/{user_id}",
                json={"permissions": ["not_a_real_module"]},
                headers=headers,
            )
            assert r.status_code == 400
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_non_admin_and_unauthenticated_are_rejected():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}regular", is_admin=False, permissions=["library"])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/users")
            assert r.status_code == 401

            r = await client.get("/api/v1/users", headers=_headers(f"{PREFIX}regular"))
            assert r.status_code == 403
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_self_returns_400():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            admin = await _seed_user(db, f"{PREFIX}admin_del", is_admin=True)
            # Second admin so the last-admin guard doesn't also fire, isolating
            # the "delete self" check as the actual source of the 400.
            await _seed_user(db, f"{PREFIX}admin_del2", is_admin=True)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = _headers(admin.username)
            r = await client.delete(f"/api/v1/users/{admin.id}", headers=headers)
            assert r.status_code == 400
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_demote_last_admin_returns_400():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            admin = await _seed_user(db, f"{PREFIX}last_admin", is_admin=True)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = _headers(admin.username)
            r = await client.patch(
                f"/api/v1/users/{admin.id}", json={"is_admin": False}, headers=headers
            )
            assert r.status_code == 400
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reset_password_round_trip_logs_in_with_new_password():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            admin = await _seed_user(db, f"{PREFIX}admin_reset", is_admin=True)
            target = await _seed_user(db, f"{PREFIX}target", password="old-password")

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = _headers(admin.username)
            r = await client.post(f"/api/v1/users/{target.id}/reset-password", headers=headers)
            assert r.status_code == 200
            new_password = r.json()["password"]
            assert len(new_password) >= 12

            r = await client.post(
                "/api/v1/auth/login",
                json={"username": target.username, "password": new_password},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["access_token"]
            assert body["must_change_password"] is True
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
