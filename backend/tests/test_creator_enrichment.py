"""Tests for Danbooru-first creator enrichment (creation paths + recovery sweep)."""

import uuid

import pytest
from sqlalchemy import select, text

FAKE_ARTIST = {
    "id": 240355,
    "name": "yosei_bin",
    "other_names": ["YOSEI_BIN"],
    "urls": [
        {"url": "https://www.pixiv.net/users/75220791",
         "normalized_url": "https://www.pixiv.net/users/75220791",
         "is_active": True},
    ],
}


async def _clear(db):
    await db.execute(text(
        "TRUNCATE creator_links, subscription_sources, subscriptions, "
        "source_creators, creators RESTART IDENTITY CASCADE"))
    await db.commit()


def _fake_links():
    from app.services.danbooru import extract_creator_links
    return extract_creator_links(FAKE_ARTIST)


async def _seed_flagged_creator(db, *, source_creator_id="75220791", name_suffix=""):
    from app.models import Creator, SourceCreator

    creator = Creator(
        name=f"pixiv_{source_creator_id}{name_suffix}",
        display_name=f"yosei{name_suffix}",
        description=(
            "Imported from disk metadata.\n"
            "Source: pixiv\n"
            f"Source creator ID: {source_creator_id}\n"
            "Enrichment: danbooru_not_found"
        ),
    )
    db.add(creator)
    await db.flush()
    sc = SourceCreator(
        creator_id=creator.id,
        source="pixiv",
        source_creator_id=source_creator_id,
        source_url=f"https://www.pixiv.net/users/{source_creator_id}",
        display_name=f"yosei{name_suffix}",
        raw_metadata={"_disk_import": {
            "needs_enrichment": True,
            "enrichment_status": "danbooru_not_found",
        }},
    )
    db.add(sc)
    await db.commit()
    return creator, sc


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reenrich_pending_applies_mapping_when_found(monkeypatch):
    from app.database import async_session, engine
    from app.models import CreatorLink, SubscriptionSource
    from app.services import creator_enrichment as ce

    monkeypatch.setattr(
        ce.danbooru_svc, "search_and_extract",
        lambda **kwargs: (FAKE_ARTIST, _fake_links()))

    try:
        async with async_session() as db:
            await _clear(db)
            creator, sc = await _seed_flagged_creator(db)

            report = await ce.reenrich_pending(db)

            assert report["scanned"] == 1
            assert report["found"] == 1
            assert report["aborted"] is False
            assert report["items"][0]["status"] == ce.STATUS_FOUND

            await db.refresh(creator)
            await db.refresh(sc)
            assert creator.danbooru_artist_id == 240355
            assert "Enrichment: danbooru_found" in creator.description
            marker = sc.raw_metadata["_disk_import"]
            assert marker["needs_enrichment"] is False
            assert marker["enrichment_status"] == ce.STATUS_FOUND

            links = (await db.execute(select(CreatorLink).where(
                CreatorLink.creator_id == creator.id))).scalars().all()
            assert any(l.link_type == "danbooru" for l in links)

            danbooru_sources = (await db.execute(select(SubscriptionSource).where(
                SubscriptionSource.source == "danbooru"))).scalars().all()
            assert len(danbooru_sources) == 1
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reenrich_pending_records_not_found_and_keeps_flag(monkeypatch):
    from app.database import async_session, engine
    from app.services import creator_enrichment as ce

    monkeypatch.setattr(
        ce.danbooru_svc, "search_and_extract",
        lambda **kwargs: (None, []))

    try:
        async with async_session() as db:
            await _clear(db)
            creator, sc = await _seed_flagged_creator(db)

            report = await ce.reenrich_pending(db)

            assert report["not_found"] == 1
            await db.refresh(sc)
            marker = sc.raw_metadata["_disk_import"]
            assert marker["needs_enrichment"] is True
            assert marker["enrichment_status"] == ce.STATUS_NOT_FOUND
            assert marker["last_attempt_at"]
            await db.refresh(creator)
            assert creator.danbooru_artist_id is None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reenrich_pending_aborts_on_unavailable(monkeypatch):
    from app.database import async_session, engine
    from app.services import creator_enrichment as ce
    from app.services.danbooru import DanbooruUnavailableError

    def _raise(**kwargs):
        raise DanbooruUnavailableError("net down")

    monkeypatch.setattr(ce.danbooru_svc, "search_and_extract", _raise)

    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_flagged_creator(db, source_creator_id="111", name_suffix="a")
            _, sc2 = await _seed_flagged_creator(db, source_creator_id="222", name_suffix="b")

            report = await ce.reenrich_pending(db)

            assert report["aborted"] is True
            assert report["scanned"] == 1
            assert report["errors"] == 1
            # second row untouched — no attempt recorded
            await db.refresh(sc2)
            assert "last_attempt_at" not in sc2.raw_metadata["_disk_import"]
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrich_creator_by_id_manual_creator_searches_by_name(monkeypatch):
    from app.database import async_session, engine
    from app.models import Creator
    from app.services import creator_enrichment as ce

    seen = {}

    def _search(**kwargs):
        seen.update(kwargs)
        return FAKE_ARTIST, _fake_links()

    monkeypatch.setattr(ce.danbooru_svc, "search_and_extract", _search)

    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="yosei_manual", display_name="よせえ")
            db.add(creator)
            await db.commit()

            result = await ce.enrich_creator_by_id(db, creator.id)

            assert result["status"] == ce.STATUS_FOUND
            assert seen.get("artist_name") == "よせえ"
            await db.refresh(creator)
            assert creator.danbooru_artist_id == 240355
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrich_creator_by_id_missing_creator_raises():
    from app.database import async_session, engine
    from app.services import creator_enrichment as ce

    try:
        async with async_session() as db:
            await _clear(db)
            with pytest.raises(ValueError):
                await ce.enrich_creator_by_id(db, uuid.uuid4())
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
