"""Task 7 (A5): server-side NSFW visibility filtering.

Seeds one NSFW work + one SFW work. A user with nsfw_visible=false must see
only the SFW work in the works list and get 404 on the NSFW work's detail.
An admin (nsfw_visible=true, the default) sees both. Also verifies
SearchService composes the Meilisearch `is_nsfw = false` filter when
force_sfw is requested, and omits it otherwise.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

PREFIX = "nsfw_filter_"


async def _clear(db):
    from app.services.cache import cache_delete_pattern

    await db.execute(text(f"DELETE FROM users WHERE username LIKE '{PREFIX}%'"))
    await db.execute(text(
        "TRUNCATE work_sources, works, source_creators, creators RESTART IDENTITY CASCADE"))
    await db.commit()
    # The works list/count response cache is keyed on filters only (not on
    # actual row IDs), so a prior test run's cached page would otherwise leak
    # stale work IDs across TRUNCATEs — invalidate it explicitly.
    cache_delete_pattern("works:*")


async def _seed_user(db, username, *, is_admin=False, nsfw_visible=True):
    from app.auth import hash_password
    from app.models.user import User

    user = User(
        username=username,
        password_hash=hash_password("hunter22"),
        is_admin=is_admin,
        is_active=True,
        permissions=["library"],
        nsfw_visible=nsfw_visible,
        must_change_password=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _seed_works(db):
    from app.models import Work

    sfw = Work(title="sfw work", is_nsfw=False)
    nsfw = Work(title="nsfw work", is_nsfw=True)
    db.add(sfw)
    db.add(nsfw)
    await db.commit()
    await db.refresh(sfw)
    await db.refresh(nsfw)
    return sfw, nsfw


def _headers(username):
    from app.auth import create_access_token
    token = create_access_token(username, must_change_password=False)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restricted_user_sees_only_sfw_work_in_list():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}restricted", nsfw_visible=False)
            sfw, nsfw = await _seed_works(db)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/works", headers=_headers(f"{PREFIX}restricted"))
            assert r.status_code == 200, r.text
            body = r.json()
            ids = {item["id"] for item in body["items"]}
            assert str(sfw.id) in ids
            assert str(nsfw.id) not in ids
            assert body["total"] == 1
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restricted_user_gets_404_on_nsfw_work_detail():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}restricted2", nsfw_visible=False)
            sfw, nsfw = await _seed_works(db)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = _headers(f"{PREFIX}restricted2")
            r = await client.get(f"/api/v1/works/{nsfw.id}", headers=headers)
            assert r.status_code == 404, r.text
            r = await client.get(f"/api/v1/works/{sfw.id}", headers=headers)
            assert r.status_code == 200, r.text
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_sees_both_works_and_can_open_nsfw_detail():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}admin", is_admin=True, nsfw_visible=True)
            sfw, nsfw = await _seed_works(db)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = _headers(f"{PREFIX}admin")
            r = await client.get("/api/v1/works", headers=headers)
            assert r.status_code == 200, r.text
            ids = {item["id"] for item in r.json()["items"]}
            assert str(sfw.id) in ids
            assert str(nsfw.id) in ids

            r = await client.get(f"/api/v1/works/{nsfw.id}", headers=headers)
            assert r.status_code == 200, r.text
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


class _FakeIndex:
    def __init__(self, name, calls_by_index):
        self._name = name
        self._calls_by_index = calls_by_index

    def search(self, query, **kwargs):
        self._calls_by_index.setdefault(self._name, []).append(kwargs)

        class _Result:
            hits = []
            estimated_total_hits = 0

        return _Result()


class _FakeClient:
    def __init__(self):
        self.calls_by_index: dict[str, list[dict]] = {}

    def index(self, name):
        return _FakeIndex(name, self.calls_by_index)


@pytest.mark.asyncio
async def test_search_service_adds_nsfw_filter_when_force_sfw(monkeypatch):
    from app.services import search as search_module

    fake = _FakeClient()
    monkeypatch.setattr(search_module, "_client", lambda: fake)

    svc = search_module.SearchService(db=None)
    await svc.search("cat", kind="works", force_sfw=True)

    works_calls = fake.calls_by_index.get(search_module.WORKS_INDEX)
    assert works_calls, "expected the works index to be searched"
    assert works_calls[-1].get("filter") == "is_nsfw = false"


@pytest.mark.asyncio
async def test_search_service_omits_nsfw_filter_by_default(monkeypatch):
    from app.services import search as search_module

    fake = _FakeClient()
    monkeypatch.setattr(search_module, "_client", lambda: fake)

    svc = search_module.SearchService(db=None)
    await svc.search("cat", kind="works")

    works_calls = fake.calls_by_index.get(search_module.WORKS_INDEX)
    assert works_calls, "expected the works index to be searched"
    assert works_calls[-1].get("filter") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restricted_user_gets_404_on_nsfw_work_assets():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}restricted_assets", nsfw_visible=False)
            sfw, nsfw = await _seed_works(db)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = _headers(f"{PREFIX}restricted_assets")
            r = await client.get(f"/api/v1/works/{nsfw.id}/assets", headers=headers)
            assert r.status_code == 404, r.text
            r = await client.get(f"/api/v1/works/{sfw.id}/assets", headers=headers)
            assert r.status_code == 200, r.text
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restricted_user_gets_404_on_nsfw_work_sources():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}restricted_sources", nsfw_visible=False)
            sfw, nsfw = await _seed_works(db)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = _headers(f"{PREFIX}restricted_sources")
            r = await client.get(f"/api/v1/works/{nsfw.id}/sources", headers=headers)
            assert r.status_code == 404, r.text
            r = await client.get(f"/api/v1/works/{sfw.id}/sources", headers=headers)
            assert r.status_code == 200, r.text
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restricted_user_gets_404_on_nsfw_work_tags():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}restricted_tags", nsfw_visible=False)
            sfw, nsfw = await _seed_works(db)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = _headers(f"{PREFIX}restricted_tags")
            r = await client.get(f"/api/v1/works/{nsfw.id}/tags", headers=headers)
            assert r.status_code == 404, r.text
            r = await client.get(f"/api/v1/works/{sfw.id}/tags", headers=headers)
            assert r.status_code == 200, r.text
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
