"""Showcase sampling endpoint: auth gate, NSFW double-gate, filters, signed URLs."""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

PREFIX = "showcase_api_"


async def _clear(db):
    from app.services.cache import cache_delete_pattern

    await db.execute(text(f"DELETE FROM users WHERE username LIKE '{PREFIX}%'"))
    await db.execute(text(
        "TRUNCATE work_sources, works, assets, source_creators, creators RESTART IDENTITY CASCADE"))
    await db.commit()
    cache_delete_pattern("works:*")
    cache_delete_pattern("showcase:*")


async def _seed_user(db, username, *, nsfw_visible=True, permissions=None):
    from app.auth import hash_password
    from app.models.user import User

    user = User(
        username=username,
        password_hash=hash_password("hunter22"),
        is_admin=False,
        is_active=True,
        permissions=permissions if permissions is not None else ["library"],
        nsfw_visible=nsfw_visible,
        must_change_password=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_thumbnail_asset(db, work_id, suffix, *, width=1200, height=1600):
    """Create and persist a thumbnail-eligible Asset. Shared by every seeding
    helper below so works consistently get a real candidate thumbnail rather
    than each test hand-rolling its own Asset() call."""
    from app.models.asset import Asset

    asset = Asset(file_path=f"/x/{work_id}-{suffix}.jpg", file_name=f"{suffix}.jpg",
                  width=width, height=height, mime_type="image/jpeg")
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


async def _seed_works(db):
    """One SFW work, one NSFW work, one favorite — each with its own
    thumbnail asset. The NSFW work getting a real thumbnail matters: the
    route skips any work with no thumbnail_asset_id, so without one the NSFW
    work would be excluded from every response regardless of the NSFW
    filter, and a test asserting its absence would pass for the wrong
    reason. Giving it a thumbnail makes it a genuine candidate that only the
    NSFW filter (force_sfw) can exclude."""
    from app.models import Work

    sfw = Work(title="sfw showcase", is_nsfw=False, is_favorite=False)
    nsfw = Work(title="nsfw showcase", is_nsfw=True, is_favorite=False)
    fav = Work(title="fav showcase", is_nsfw=False, is_favorite=True)
    db.add_all([sfw, nsfw, fav])
    await db.commit()
    for w in (sfw, nsfw, fav):
        await db.refresh(w)

    asset = await _make_thumbnail_asset(db, sfw.id, "a", width=1200, height=1600)
    nsfw_asset = await _make_thumbnail_asset(db, nsfw.id, "n", width=1000, height=1000)
    fav_asset = await _make_thumbnail_asset(db, fav.id, "b", width=800, height=600)
    sfw.thumbnail_asset_id = asset.id
    nsfw.thumbnail_asset_id = nsfw_asset.id
    fav.thumbnail_asset_id = fav_asset.id
    await db.commit()
    return sfw, nsfw, fav, asset


async def _seed_many_works(db, n, *, prefix="bulk"):
    """Seed `n` plain SFW works, each with its own thumbnail asset. Used to
    exercise the random-offset windowing branch in the route (`total >
    count`), which needs a filtered set bigger than any single page to run
    at all — the three-work `_seed_works` fixture above never triggers it."""
    from app.models import Work

    works = [Work(title=f"{prefix} {i}", is_nsfw=False, is_favorite=False) for i in range(n)]
    db.add_all(works)
    await db.commit()
    for w in works:
        await db.refresh(w)
    for i, w in enumerate(works):
        asset = await _make_thumbnail_asset(db, w.id, i)
        w.thumbnail_asset_id = asset.id
    await db.commit()
    return works


def _headers(username):
    from app.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token(username, must_change_password=False)}"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_hides_nsfw_from_restricted_user_and_signs_preview_urls():
    from urllib.parse import parse_qs, urlparse

    from app.database import async_session, engine
    from app.main import app
    from app.services.media_signing import verify_media_token

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}restricted", nsfw_visible=False)
            sfw, nsfw, fav, asset = await _seed_works(db)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/showcase/sample?count=10",
                                 headers=_headers(f"{PREFIX}restricted"))
            assert r.status_code == 200, r.text
            items = r.json()["items"]
            ids = {i["work_id"] for i in items}
            assert str(sfw.id) in ids
            assert str(nsfw.id) not in ids

            item = next(i for i in items if i["work_id"] == str(sfw.id))
            assert item["width"] == 1200 and item["height"] == 1600
            assert item["thumb_url"] == f"/media/thumb/{asset.id}"

            parsed = urlparse(item["preview_url"])
            q = parse_qs(parsed.query)
            assert parsed.path == f"/media/preview/{asset.id}"
            assert verify_media_token(str(asset.id), "preview",
                                      q["expires"][0], q["token"][0]) is True

            # Finding 2(a) — security-critical divergent branch: the account
            # setting must win even when the request explicitly asks for
            # NSFW. force_sfw = (not nsfw_visible) or (not include_nsfw); for
            # a restricted user this is True regardless of include_nsfw. If
            # `or` were mistyped as `and`, force_sfw would flip to False here
            # ((not False) and (not True) == False) and the NSFW work would
            # leak through.
            r_forced = await client.get(
                "/api/v1/showcase/sample?count=10&include_nsfw=true",
                headers=_headers(f"{PREFIX}restricted"))
            assert r_forced.status_code == 200, r_forced.text
            forced_ids = {i["work_id"] for i in r_forced.json()["items"]}
            assert str(nsfw.id) not in forced_ids
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_include_nsfw_param_toggles_for_permissive_user():
    """Finding 2(b): the other divergent branch. For a user with
    nsfw_visible=True, force_sfw = (not True) or (not include_nsfw) —
    i.e. it collapses to `not include_nsfw`, so the NSFW work must be hidden
    by default and appear only when include_nsfw=true is explicitly passed.

    Combined with the restricted-user case in the test above, this pins down
    both branches where `(not nsfw_visible) or (not include_nsfw)` diverges
    from an `and`-typo'd version (they differ exactly when one flag is
    true and the other is false). If `or` were `and` here, force_sfw would
    be False for the default request too ((not True) and (not False) ==
    False), and the "hidden by default" assertion below would fail.
    """
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}permissive", nsfw_visible=True)
            sfw, nsfw, fav, _asset = await _seed_works(db)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r_default = await client.get("/api/v1/showcase/sample?count=10",
                                         headers=_headers(f"{PREFIX}permissive"))
            assert r_default.status_code == 200, r_default.text
            default_ids = {i["work_id"] for i in r_default.json()["items"]}
            assert str(nsfw.id) not in default_ids

            r_included = await client.get(
                "/api/v1/showcase/sample?count=10&include_nsfw=true",
                headers=_headers(f"{PREFIX}permissive"))
            assert r_included.status_code == 200, r_included.text
            included_ids = {i["work_id"] for i in r_included.json()["items"]}
            assert str(nsfw.id) in included_ids
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_respects_favorites_scope_and_count_cap():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}user2", nsfw_visible=True)
            sfw, nsfw, fav, _asset = await _seed_works(db)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/showcase/sample?count=10&scope=favorites",
                                 headers=_headers(f"{PREFIX}user2"))
            assert r.status_code == 200, r.text
            ids = {i["work_id"] for i in r.json()["items"]}
            assert ids == {str(fav.id)}

            r2 = await client.get("/api/v1/showcase/sample?count=2",
                                  headers=_headers(f"{PREFIX}user2"))
            assert r2.status_code == 200, r2.text
            assert len(r2.json()["items"]) <= 2
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_empty_library_returns_empty_list_and_requires_permission():
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}empty", nsfw_visible=True)
            await _seed_user(db, f"{PREFIX}nolib", permissions=["tasks"])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/showcase/sample?count=5",
                                 headers=_headers(f"{PREFIX}empty"))
            assert r.status_code == 200, r.text
            assert r.json()["items"] == []

            r2 = await client.get("/api/v1/showcase/sample?count=5",
                                  headers=_headers(f"{PREFIX}nolib"))
            assert r2.status_code == 403

            r3 = await client.get("/api/v1/showcase/sample?count=5")
            assert r3.status_code == 401
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_windows_over_a_large_filtered_set_without_duplicates():
    """Finding 3: exercise the random-offset windowing branch (`total >
    count`) — the reason this endpoint exists — which every other test in
    this file sidesteps by seeding at most 3 works (always <= count, so
    `total > count` is never true and the window query never runs). With 12
    seeded works and count=3, total (12) > count (3) is guaranteed, so the
    route must pick a random offset and pull a real window rather than just
    returning everything from offset 0.
    """
    from app.database import async_session, engine
    from app.main import app

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}windowed", nsfw_visible=True)
            works = await _seed_many_works(db, 12)
        seeded_ids = {str(w.id) for w in works}

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/showcase/sample?count=3",
                                 headers=_headers(f"{PREFIX}windowed"))
            assert r.status_code == 200, r.text
            ids = [i["work_id"] for i in r.json()["items"]]
            assert len(ids) == 3
            assert set(ids) <= seeded_ids
            assert len(set(ids)) == len(ids)  # no repeats within one response
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_self_heals_from_a_stale_cached_total(monkeypatch):
    """Finding 4: the filtered total is cached for 300s and fed back in as
    `precomputed_total`, bypassing the fresh COUNT. If works are deleted or
    hidden inside that window, the cached total is now larger than reality,
    `random.randint(0, total - count)` can pick an offset past the real end
    of the table, and the windowed query returns 0 rows even though matching
    works still exist. The route must notice the empty window and retry once
    at offset 0 with a fresh COUNT, refreshing the cache in the process.

    Simulated deterministically: seed 5 real works, manually poison the
    cache with an inflated total (1000), and pin random.randint to always
    return its upper bound so the first windowed query is guaranteed to land
    past the real data and come back empty — forcing the self-heal path.
    """
    import app.api.showcase as showcase_module
    from app.database import async_session, engine
    from app.main import app
    from app.services.cache import cache_get, cache_key, cache_set

    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_user(db, f"{PREFIX}stale", nsfw_visible=True)
            works = await _seed_many_works(db, 5, prefix="stale")
        seeded_ids = {str(w.id) for w in works}

        # A default request (no include_nsfw, nsfw_visible=True) resolves to
        # force_sfw=True — match that when poisoning the cache key so the
        # route actually reads back our stale value.
        ck = cache_key("showcase:count", source=None, tag=None,
                       is_favorite=None, force_sfw=True)
        cache_set(ck, 1000, 300)
        monkeypatch.setattr(showcase_module.random, "randint", lambda a, b: b)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/showcase/sample?count=3",
                                 headers=_headers(f"{PREFIX}stale"))
            assert r.status_code == 200, r.text
            items = r.json()["items"]
            ids = [i["work_id"] for i in items]
            assert len(ids) == 3
            assert set(ids) <= seeded_ids
            assert len(set(ids)) == len(ids)

        # the stale entry must have been replaced by the fresh COUNT (5),
        # not left at the poisoned 1000
        assert cache_get(ck) == 5
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
