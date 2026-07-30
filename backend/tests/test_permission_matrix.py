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
from unittest.mock import AsyncMock, patch

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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_moved_endpoints_pinned_to_curation_and_tasks():
    """Regression pin (Task 6 fix round 1): dedup/merge-candidates were moved
    from `system` to `curation`, and scheduler/queue-stats operations from
    `system` to `tasks`, per spec §A2. Verify the new module boundary holds
    and the old `system` permission no longer grants access.
    """
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}curation_pin", permissions=["curation"])
            await _seed_user(db, f"{PREFIX}tasks_pin", permissions=["tasks"])
            await _seed_user(db, f"{PREFIX}system_pin", permissions=["system"])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            curation_headers = _headers(f"{PREFIX}curation_pin")
            tasks_headers = _headers(f"{PREFIX}tasks_pin")
            system_headers = _headers(f"{PREFIX}system_pin")

            r = await client.get("/api/v1/admin/dedup/duplicates", headers=curation_headers)
            assert r.status_code == 200, f"curation -> dedup/duplicates: {r.status_code} {r.text}"
            r = await client.get("/api/v1/admin/dedup/duplicates", headers=system_headers)
            assert r.status_code == 403, f"system -> dedup/duplicates: {r.status_code} {r.text}"

            r = await client.get("/api/v1/system/queue-stats", headers=tasks_headers)
            assert r.status_code == 200, f"tasks -> queue-stats: {r.status_code} {r.text}"
            r = await client.get("/api/v1/system/queue-stats", headers=system_headers)
            assert r.status_code == 403, f"system -> queue-stats: {r.status_code} {r.text}"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_library_permission_blocked_from_curation_write_routes():
    """Final-review fix (I1): write routes on works/creators/tags were moved
    from `library` to `curation` per spec §A2 (favorite/trash/restore/purge/
    dedup/merge/tag-editing and all other write operations on library
    entities belong to `curation`; `library` is read/browsing only).

    A library-only user still gets 200 on GET /works (read stays library)
    but 403 on the favorite and tag-create mutation routes. A curation user
    passes the router-level gate on the favorite route -- 404 for a
    nonexistent work id, not 403, proving the permission check itself no
    longer blocks it.
    """
    from uuid import uuid4
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}library_write", permissions=["library"])
            await _seed_user(db, f"{PREFIX}curation_write", permissions=["curation"])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            library_headers = _headers(f"{PREFIX}library_write")
            curation_headers = _headers(f"{PREFIX}curation_write")
            random_id = uuid4()

            # This assertion verifies the library permission boundary, not
            # Meilisearch availability. Keep the permission matrix isolated
            # from external search infrastructure so a missing CI service
            # cannot turn an authorized request into an unrelated 503.
            search_result = {
                "groups": {"works": {"total": 0, "items": []}},
                "total": 0,
            }
            with patch(
                "app.api.works.SearchService.search",
                new=AsyncMock(return_value=search_result),
            ):
                r = await client.get("/api/v1/works", headers=library_headers)
            assert r.status_code == 200, f"library -> GET /works: {r.status_code} {r.text}"

            r = await client.post(f"/api/v1/works/{random_id}/favorite", headers=library_headers)
            assert r.status_code == 403, (
                f"library -> POST /works/{{id}}/favorite: {r.status_code} {r.text}"
            )

            r = await client.post(
                "/api/v1/tags", json={"normalized_name": "perm_matrix_probe"}, headers=library_headers
            )
            assert r.status_code == 403, f"library -> POST /tags: {r.status_code} {r.text}"

            # curation permission clears the router-level gate; 404 (not 403)
            # confirms the request reached the handler and only the
            # not-found lookup failed.
            r = await client.post(f"/api/v1/works/{random_id}/favorite", headers=curation_headers)
            assert r.status_code == 404, (
                f"curation -> POST /works/{{id}}/favorite: {r.status_code} {r.text}"
            )
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
