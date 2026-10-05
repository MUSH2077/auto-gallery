"""Tests for /api/v1/users CRUD API and its admin-only guards."""
import pytest
import asyncio
import time
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update

PREFIX = "api_test_"


async def _clear(db):
    await db.execute(text(f"DELETE FROM users WHERE username LIKE '{PREFIX}%'"))
    await db.commit()


async def _suspend_other_active_admins(db):
    from app.models.user import User

    admin_ids = list(
        (
            await db.execute(
                select(User.id).where(
                    User.is_admin.is_(True),
                    User.is_active.is_(True),
                    ~User.username.startswith(PREFIX),
                )
            )
        ).scalars()
    )
    if admin_ids:
        await db.execute(
            update(User).where(User.id.in_(admin_ids)).values(is_admin=False)
        )
        await db.commit()
    return admin_ids


async def _restore_admins(db, admin_ids):
    from app.models.user import User

    if admin_ids:
        await db.execute(
            update(User).where(User.id.in_(admin_ids)).values(is_admin=True)
        )
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


async def _headers(username):
    from tests.conftest import access_token_for_user

    token = await access_token_for_user(username)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_logins_leave_the_event_loop_available(monkeypatch):
    from app.api import auth_api
    from app.auth import create_access_token
    from app.database import async_session, engine
    from app.main import app
    from app.services.rate_limiter import RateLimiter

    monkeypatch.setattr(auth_api, "_login_limiter", RateLimiter(prefix="password-thread-test", max_requests=1000))
    gaps = []
    done = asyncio.Event()
    async def ticker():
        previous = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.monotonic()
            gaps.append(now - previous)
            previous = now
    tick = None
    try:
        async with async_session() as db:
            await _clear(db)
            user = await _seed_user(db, PREFIX + "cpu", permissions=["library"])
        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0.02)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            logins = [asyncio.create_task(
                client.post("/api/v1/auth/login", json={"username": user.username, "password": "hunter22"})
            )
                for _ in range(4)
            ]
            await asyncio.sleep(0.03)
            peer = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {create_access_token(user)}"})
            assert peer.status_code == 200
            assert any(not request.done() for request in logins)
            responses = await asyncio.gather(*logins)
        assert [response.status_code for response in responses] == [200] * 4
        assert max(gaps) < 0.15, gaps
    finally:
        done.set()
        if tick is not None:
            await tick
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reset", "replace", "disable"])
async def test_bearer_cannot_survive_account_security_changes(change, monkeypatch):
    from app.api import auth_api
    from app.database import async_session, engine
    from app.main import app
    from app.services.rate_limiter import RateLimiter

    monkeypatch.setattr(auth_api, "_login_limiter", RateLimiter(prefix="account-security-test", max_requests=1000, window_seconds=60))
    try:
        async with async_session() as db:
            await _clear(db)
            admin = await _seed_user(db, f"{PREFIX}security_admin", is_admin=True)
            user = await _seed_user(db, f"{PREFIX}security_user", permissions=["library", "tasks"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def login(account):
                response = await client.post("/api/v1/auth/login", json={"username": account.username, "password": "hunter22"})
                assert response.status_code == 200, response.text
                return {"Authorization": f"Bearer {response.json()['access_token']}"}

            admin_headers = await login(admin)
            old_headers = await login(user)
            assert (await client.get("/api/v1/users", headers=old_headers)).status_code == 403
            if change == "reset":
                response = await client.post(f"/api/v1/users/{user.id}/reset-password", headers=admin_headers)
                reset_login = await client.post("/api/v1/auth/login", json={"username": user.username, "password": response.json()["password"]})
                forced_headers = {"Authorization": f"Bearer {reset_login.json()['access_token']}"}
                assert (await client.get("/api/v1/auth/me", headers=forced_headers)).status_code == 200
                assert (await client.get("/api/v1/tags", headers=forced_headers)).status_code == 403
                assert (await client.post("/api/v1/auth/ws-ticket", headers=forced_headers)).status_code == 403
            elif change == "disable":
                response = await client.patch(f"/api/v1/users/{user.id}", headers=admin_headers, json={"is_active": False})
            else:
                assert (await client.delete(f"/api/v1/users/{user.id}", headers=admin_headers)).status_code == 200
                response = await client.post("/api/v1/users", headers=admin_headers,
                    json={"username": user.username, "password": "hunter22", "is_admin": True})
                assert response.json()["id"] != user.id
            assert response.status_code in (200, 201), response.text
            for path in ("/api/v1/auth/me", "/api/v1/tags", "/api/v1/users"):
                assert (await client.get(path, headers=old_headers)).status_code == 401, path
            assert (await client.post("/api/v1/auth/ws-ticket", headers=old_headers)).status_code == 401
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["bearer", "browser"])
async def test_both_password_change_entries_revoke_previous_bearer(entry, monkeypatch):
    from app.api import auth_api
    from app.config import settings
    from app.database import async_session, engine
    from app.models.user import User
    from app.main import app
    from app.services.rate_limiter import RateLimiter

    monkeypatch.setattr(settings, "browser_session_origins", "http://test")
    monkeypatch.setattr(auth_api, "_login_limiter", RateLimiter(prefix="password-version-test", max_requests=1000, window_seconds=60))
    try:
        async with async_session() as db:
            await _clear(db)
            user = await _seed_user(db, f"{PREFIX}password_version", permissions=["library"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post("/api/v1/auth/login", json={"username": user.username, "password": "hunter22"})
            old_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            if entry == "browser":
                browser_login = await client.post("/api/v1/auth/browser/login", headers={"Origin": "http://test"}, json={"username": user.username, "password": "hunter22"})
                assert browser_login.status_code == 200
                headers = {"Origin": "http://test", "X-CSRF-Token": client.cookies.get("ag_csrf")}
                path = "/api/v1/auth/browser/change-password"
            else:
                headers, path = old_headers, "/api/v1/auth/change-password"
            changed = await client.post(path, headers=headers, json={"current_password": "hunter22", "new_password": "hunter33"})
            assert changed.status_code == 200, changed.text
            assert (await client.get("/api/v1/auth/me", headers=old_headers)).status_code == 401
            if entry == "bearer":
                new_headers = {"Authorization": f"Bearer {changed.json()['access_token']}"}
            else:
                new_headers = {}
            assert (await client.get("/api/v1/auth/me", headers=new_headers)).status_code == 200
        async with async_session() as db:
            current = await db.get(User, user.id)
            assert current.credential_version == 2
            assert not current.must_change_password
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("first_field,second_action", [("is_admin", "demote"), ("is_admin", "disable"), ("is_active", "delete")])
async def test_concurrent_admin_removals_preserve_one_active_admin(first_field, second_action, monkeypatch):
    from app.database import async_session, engine
    from app.main import app
    from app.models.user import User
    from app.services.users import UserService

    checked, release = asyncio.Event(), asyncio.Event()
    original = UserService._ensure_not_last_admin
    checks = 0
    async def hold_first_check(service, excluding_user_id):
        nonlocal checks
        await original(service, excluding_user_id)
        checks += 1
        if checks == 1:
            checked.set()
            await release.wait()
    monkeypatch.setattr(UserService, "_ensure_not_last_admin", hold_first_check)
    suspended = []
    try:
        async with async_session() as db:
            await _clear(db)
            suspended = await _suspend_other_active_admins(db)
            first = await _seed_user(db, f"{PREFIX}concurrent_a", is_admin=True)
            second = await _seed_user(db, f"{PREFIX}concurrent_b", is_admin=True)
        first_headers, second_headers = await _headers(first.username), await _headers(second.username)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first_request = asyncio.create_task(client.patch(f"/api/v1/users/{first.id}", headers=first_headers, json={first_field: False}))
            await asyncio.wait_for(checked.wait(), timeout=5)
            if second_action == "delete":
                next_call = client.delete(f"/api/v1/users/{second.id}", headers=first_headers)
            else:
                field = "is_admin" if second_action == "demote" else "is_active"
                next_call = client.patch(f"/api/v1/users/{second.id}", headers=second_headers, json={field: False})
            second_request = asyncio.create_task(next_call)
            try:
                # The first mutation remains uncommitted while the competing
                # request reaches the same invariant. A correct transaction
                # owner blocks it until the first mutation is visible.
                await asyncio.wait_for(asyncio.shield(second_request), timeout=1)
            except asyncio.TimeoutError:
                pass
            finally:
                release.set()
            results = await asyncio.gather(first_request, second_request)
            assert sorted(response.status_code for response in results) == [200, 400]
        async with async_session() as db:
            active = list((await db.execute(select(User.id).where(User.is_admin.is_(True), User.is_active.is_(True)))).scalars())
            assert len(active) == 1
    finally:
        release.set()
        async with async_session() as db:
            await _clear(db)
            await _restore_admins(db, suspended)
        await engine.dispose()


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
            headers = await _headers(admin.username)

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
async def test_password_writes_reject_values_over_bcrypt_byte_limit():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    too_long_password = "界" * 25  # 75 UTF-8 bytes, despite being only 25 characters.
    try:
        async with async_session() as db:
            await _clear(db)
            admin = await _seed_user(db, f"{PREFIX}password_admin", is_admin=True)
            target = await _seed_user(db, f"{PREFIX}password_target")

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/v1/users",
                json={"username": f"{PREFIX}too_long", "password": too_long_password},
                headers=await _headers(admin.username),
            )
            assert r.status_code == 400
            assert "at most 72 bytes" in r.json()["detail"]

            r = await client.post(
                "/api/v1/auth/change-password",
                json={"current_password": "hunter22", "new_password": too_long_password},
                headers=await _headers(target.username),
            )
            assert r.status_code == 400
            assert "at most 72 bytes" in r.json()["detail"]
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

            r = await client.get("/api/v1/users", headers=await _headers(f"{PREFIX}regular"))
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
            headers = await _headers(admin.username)
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
    suspended_admin_ids = []
    try:
        async with async_session() as db:
            await _clear(db)
            suspended_admin_ids = await _suspend_other_active_admins(db)
            admin = await _seed_user(db, f"{PREFIX}last_admin", is_admin=True)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _headers(admin.username)
            r = await client.patch(
                f"/api/v1/users/{admin.id}", json={"is_admin": False}, headers=headers
            )
            assert r.status_code == 400
    finally:
        async with async_session() as db:
            await _clear(db)
            await _restore_admins(db, suspended_admin_ids)
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
            headers = await _headers(admin.username)
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_preferences_whole_replace():
    """Test that PUT /me/preferences performs whole-replace, not merge."""
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            user = await _seed_user(db, f"{PREFIX}pref_test")

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _headers(user.username)

            # PUT with theme + lang
            r = await client.put(
                "/api/v1/auth/me/preferences",
                json={"preferences": {"theme": "dark", "lang": "zh"}},
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"preferences": {"theme": "dark", "lang": "zh"}}

            # Verify via GET /me
            r = await client.get("/api/v1/auth/me", headers=headers)
            assert r.status_code == 200
            assert r.json()["preferences"] == {"theme": "dark", "lang": "zh"}

            # PUT with only theme (whole-replace: lang should be gone)
            r = await client.put(
                "/api/v1/auth/me/preferences",
                json={"preferences": {"theme": "light"}},
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json() == {"preferences": {"theme": "light"}}

            # Verify via GET /me
            r = await client.get("/api/v1/auth/me", headers=headers)
            assert r.status_code == 200
            prefs = r.json()["preferences"]
            assert prefs == {"theme": "light"}
            assert "lang" not in prefs, "lang should not exist after whole-replace"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_preferences_rejects_invalid_keys():
    """Test that PUT /me/preferences rejects invalid preference keys."""
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            user = await _seed_user(db, f"{PREFIX}pref_invalid")

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = await _headers(user.username)

            # Try to set an invalid key
            r = await client.put(
                "/api/v1/auth/me/preferences",
                json={"preferences": {"evil_key": 1}},
                headers=headers,
            )
            assert r.status_code == 400
            assert "invalid preference key" in r.json()["detail"]
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_preferences_accepts_slideshow_and_rejects_showcase_key():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}prefs_slideshow", is_admin=True)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            h = await _headers(f"{PREFIX}prefs_slideshow")
            body = {"preferences": {"theme": "dark",
                                    "slideshow": {"slideDwellMs": 6000, "slideLoop": False}}}
            r = await client.put("/api/v1/auth/me/preferences", json=body, headers=h)
            assert r.status_code == 200, r.text
            assert r.json()["preferences"]["slideshow"]["slideDwellMs"] == 6000

            me = await client.get("/api/v1/auth/me", headers=h)
            assert me.json()["preferences"]["slideshow"]["slideLoop"] is False

            rejected = await client.put(
                "/api/v1/auth/me/preferences",
                json={"preferences": {"showcase": {"landing": "showcase"}}},
                headers=h,
            )
            assert rejected.status_code == 400
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
