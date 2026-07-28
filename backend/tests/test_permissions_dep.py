"""Tests for RequirePermission / RequireAdminUser dependencies in app.auth.

Uses a small standalone probe FastAPI app (not app.main.app) mounting routes
behind the dependencies under test, exercised against real seeded users in
the test database so _load_active_user's DB lookup is genuinely covered.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

PREFIX = "perm_test_"


async def _clear(db):
    await db.execute(text(f"DELETE FROM users WHERE username LIKE '{PREFIX}%'"))
    await db.commit()


def _make_probe_app():
    from fastapi import FastAPI
    from app.auth import RequireAdminUser, RequirePermission

    probe_app = FastAPI()

    @probe_app.get("/probe/library")
    async def probe_library(user=RequirePermission("library")):
        return {"username": user.username}

    @probe_app.get("/probe/admin-only")
    async def probe_admin(user=RequireAdminUser):
        return {"username": user.username}

    return probe_app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_require_permission_matrix_for_library_module():
    from app.auth import create_access_token
    from app.database import async_session, engine
    from app.models.user import User

    transport = ASGITransport(app=_make_probe_app())
    try:
        async with async_session() as db:
            await _clear(db)
            db.add_all([
                User(username=f"{PREFIX}admin", password_hash="x",
                     is_admin=True, is_active=True, permissions=[]),
                User(username=f"{PREFIX}module", password_hash="x",
                     is_admin=False, is_active=True, permissions=["library"]),
                User(username=f"{PREFIX}inactive", password_hash="x",
                     is_admin=False, is_active=False, permissions=["library"]),
                User(username=f"{PREFIX}nomodule", password_hash="x",
                     is_admin=False, is_active=True, permissions=["upload"]),
            ])
            await db.commit()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cases = [
                (f"{PREFIX}admin", 200),
                (f"{PREFIX}module", 200),
                (f"{PREFIX}inactive", 401),
                (f"{PREFIX}nomodule", 403),
            ]
            for username, expected_status in cases:
                token = create_access_token(username, must_change_password=False)
                r = await client.get(
                    "/probe/library", headers={"Authorization": f"Bearer {token}"}
                )
                assert r.status_code == expected_status, f"{username}: {r.status_code} {r.text}"

            # No token at all -> 401
            r = await client.get("/probe/library")
            assert r.status_code == 401
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_require_admin_user_rejects_non_admin_and_accepts_admin():
    from app.auth import create_access_token
    from app.database import async_session, engine
    from app.models.user import User

    transport = ASGITransport(app=_make_probe_app())
    try:
        async with async_session() as db:
            await _clear(db)
            db.add_all([
                User(username=f"{PREFIX}admin2", password_hash="x",
                     is_admin=True, is_active=True, permissions=[]),
                User(username=f"{PREFIX}module2", password_hash="x",
                     is_admin=False, is_active=True, permissions=["library"]),
            ])
            await db.commit()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            admin_token = create_access_token(f"{PREFIX}admin2", must_change_password=False)
            r = await client.get(
                "/probe/admin-only", headers={"Authorization": f"Bearer {admin_token}"}
            )
            assert r.status_code == 200

            module_token = create_access_token(f"{PREFIX}module2", must_change_password=False)
            r = await client.get(
                "/probe/admin-only", headers={"Authorization": f"Bearer {module_token}"}
            )
            assert r.status_code == 403
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
