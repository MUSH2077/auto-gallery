"""Module-permission enforcement matrix across real routers (Task 6 / A4).

One representative GET endpoint per module, exercised against the real
app.main.app (not a probe app) so the actual router-level dependency swap
(RequireAdmin -> RequirePermission(module)) is what's under test:

    library      -> GET /api/v1/tags
    curation     -> GET /api/v1/curation/backfill/status
    subscriptions-> GET /api/v1/subscriptions/count
    tasks        -> GET /api/v1/tasks
    system       -> GET /api/v1/admin/settings

For each module-scoped user (permissions=[<module>]), the representative
endpoint of their own module must return 200, and every other module's
endpoint must return 403. The admin user must get 200 on all five.
"""
import pytest
from httpx import ASGITransport, AsyncClient

PREFIX = "perm_matrix_"

MODULE_ENDPOINTS = {
    "library": "/api/v1/tags",
    "curation": "/api/v1/curation/backfill/status",
    "subscriptions": "/api/v1/subscriptions/count",
    "tasks": "/api/v1/tasks",
    "system": "/api/v1/admin/settings",
}


async def _clear(db):
    from sqlalchemy import text
    await db.execute(text(f"DELETE FROM users WHERE username LIKE '{PREFIX}%'"))
    await db.commit()


async def _seed_user(db, username, *, is_admin=False, permissions=None):
    from app.auth import hash_password
    from app.models.user import User

    user = User(
        username=username,
        password_hash=hash_password("hunter22"),
        is_admin=is_admin,
        is_active=True,
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
async def test_module_user_gets_own_module_and_403_on_others():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            for module in MODULE_ENDPOINTS:
                await _seed_user(db, f"{PREFIX}{module}", permissions=[module])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for own_module in MODULE_ENDPOINTS:
                headers = _headers(f"{PREFIX}{own_module}")
                for module, path in MODULE_ENDPOINTS.items():
                    r = await client.get(path, headers=headers)
                    expected = 200 if module == own_module else 403
                    assert r.status_code == expected, (
                        f"user with permissions=[{own_module}] -> {path}: "
                        f"expected {expected}, got {r.status_code} ({r.text})"
                    )
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_gets_200_on_every_module():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}admin", is_admin=True, permissions=[])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = _headers(f"{PREFIX}admin")
            for module, path in MODULE_ENDPOINTS.items():
                r = await client.get(path, headers=headers)
                assert r.status_code == 200, f"admin -> {path}: {r.status_code} {r.text}"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unauthenticated_is_401_on_every_module():
    from app.database import engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for module, path in MODULE_ENDPOINTS.items():
                r = await client.get(path)
                assert r.status_code == 401, f"anon -> {path}: {r.status_code} {r.text}"
    finally:
        await engine.dispose()
