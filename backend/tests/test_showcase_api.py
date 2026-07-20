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


async def _seed_works(db):
    """One SFW work with a sized thumbnail asset, one NSFW work, one favorite
    (also given a thumbnail — the endpoint skips works with no thumbnail
    asset, matching real import behavior where every work gets one)."""
    from app.models import Work
    from app.models.asset import Asset

    sfw = Work(title="sfw showcase", is_nsfw=False, is_favorite=False)
    nsfw = Work(title="nsfw showcase", is_nsfw=True, is_favorite=False)
    fav = Work(title="fav showcase", is_nsfw=False, is_favorite=True)
    db.add_all([sfw, nsfw, fav])
    await db.commit()
    for w in (sfw, nsfw, fav):
        await db.refresh(w)

    asset = Asset(file_path=f"/x/{sfw.id}.jpg", file_name="a.jpg",
                  width=1200, height=1600, mime_type="image/jpeg")
    fav_asset = Asset(file_path=f"/x/{fav.id}.jpg", file_name="b.jpg",
                      width=800, height=600, mime_type="image/jpeg")
    db.add_all([asset, fav_asset])
    await db.commit()
    await db.refresh(asset)
    await db.refresh(fav_asset)
    sfw.thumbnail_asset_id = asset.id
    fav.thumbnail_asset_id = fav_asset.id
    await db.commit()
    return sfw, nsfw, fav, asset


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
