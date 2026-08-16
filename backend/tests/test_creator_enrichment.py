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
            # enrichment never rewrites the admin-owned description
            assert "Enrichment: danbooru_not_found" in creator.description
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
async def test_create_creator_attempts_danbooru_mapping(monkeypatch):
    from app.database import async_session, engine
    from app.services import creator_enrichment as ce
    from app.services.creator import CreatorService

    monkeypatch.setattr(
        ce.danbooru_svc, "search_and_extract",
        lambda **kwargs: (FAKE_ARTIST, _fake_links()))

    try:
        async with async_session() as db:
            await _clear(db)
            creator = await CreatorService(db).create_creator(
                {"name": "yosei_bin", "display_name": "よせえ"})
            assert creator.danbooru_artist_id == 240355
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_creator_survives_danbooru_outage(monkeypatch):
    from app.database import async_session, engine
    from app.services import creator_enrichment as ce
    from app.services.creator import CreatorService
    from app.services.danbooru import DanbooruUnavailableError

    def _raise(**kwargs):
        raise DanbooruUnavailableError("net down")

    monkeypatch.setattr(ce.danbooru_svc, "search_and_extract", _raise)

    try:
        async with async_session() as db:
            await _clear(db)
            creator = await CreatorService(db).create_creator(
                {"name": "offline_creator", "display_name": "离线画师"})
            assert creator.id is not None
            assert creator.danbooru_artist_id is None
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_refresh_all_creator_mappings_is_incremental_and_includes_inactive(monkeypatch):
    from app.database import async_session, engine
    from app.models import Creator, CreatorLink, SourceCreator, Subscription, SubscriptionSource
    from app.services import creator_enrichment as ce

    fallback_artist = {
        **FAKE_ARTIST,
        "id": 240356,
        "name": "fallback_artist",
        "urls": [{
            "url": "https://www.pixiv.net/users/888",
            "normalized_url": "https://www.pixiv.net/users/888",
            "is_active": True,
        }],
    }
    other_source_artist = {
        **FAKE_ARTIST,
        "id": 240357,
        "name": "other_source_artist",
        "urls": [{
            "url": "https://x.com/multi_artist",
            "normalized_url": "https://x.com/multi_artist",
            "is_active": True,
        }],
    }
    exact_ids = []
    searches = []

    def _get_artist(artist_id):
        exact_ids.append(artist_id)
        if artist_id == 240355:
            return FAKE_ARTIST
        if artist_id == 240356:
            return fallback_artist
        return other_source_artist

    def _search(**kwargs):
        searches.append(kwargs)
        if kwargs.get("source_url") == "https://www.pixiv.net/users/111":
            return None, []
        if kwargs.get("source_url") == "https://x.com/multi_artist":
            return other_source_artist, ce.danbooru_svc.extract_creator_links(other_source_artist)
        return fallback_artist, ce.danbooru_svc.extract_creator_links(fallback_artist)

    monkeypatch.setattr(ce.danbooru_svc, "get_artist", _get_artist)
    monkeypatch.setattr(ce.danbooru_svc, "search_and_extract", _search)

    try:
        async with async_session() as db:
            await _clear(db)
            mapped = Creator(
                name="mapped_inactive",
                display_name="Mapped inactive",
                is_active=False,
                danbooru_artist_id=240355,
            )
            fallback = Creator(name="fallback", display_name="Fallback display")
            multi_identity = Creator(name="multi_identity", display_name="Multi identity")
            db.add_all([mapped, fallback, multi_identity])
            await db.flush()
            db.add_all([
                SourceCreator(
                    creator_id=mapped.id,
                    source="pixiv",
                    source_creator_id="75220791",
                    source_url="https://www.pixiv.net/users/75220791",
                    display_name="Mapped inactive",
                ),
                SourceCreator(
                    creator_id=multi_identity.id,
                    source="pixiv",
                    source_creator_id="111",
                    source_url="https://www.pixiv.net/users/111",
                    display_name="Multi identity",
                ),
                SourceCreator(
                    creator_id=multi_identity.id,
                    source="x",
                    source_creator_id="multi_artist",
                    source_url="https://x.com/multi_artist",
                    display_name="Multi identity",
                ),
            ])
            subscription = Subscription(
                creator_id=mapped.id,
                name="manual mapping",
                is_active=True,
                sync_enabled=False,
                schedule_mode="manual",
                sync_interval_hours=12,
                scheduled_times="03:00",
            )
            db.add(subscription)
            await db.flush()
            db.add_all([
                SubscriptionSource(
                    subscription_id=subscription.id,
                    source="x",
                    source_url="https://x.com/mapped_inactive",
                    source_creator_id="mapped_inactive",
                    is_enabled=True,
                ),
                CreatorLink(
                    creator_id=mapped.id,
                    url="https://example.com/curated",
                    link_type="website",
                    source="manual",
                    confidence=1.0,
                    is_verified=True,
                    notes="keep me",
                ),
            ])
            await db.commit()

            first = await ce.refresh_all_creator_mappings(db)
            first_link_count = len((await db.execute(select(CreatorLink))).scalars().all())
            first_source_creator_count = len(
                (await db.execute(select(SourceCreator))).scalars().all()
            )
            first_subscription_source_count = len(
                (await db.execute(select(SubscriptionSource))).scalars().all()
            )
            second = await ce.refresh_all_creator_mappings(db)
            second_link_count = len((await db.execute(select(CreatorLink))).scalars().all())
            second_source_creator_count = len(
                (await db.execute(select(SourceCreator))).scalars().all()
            )
            second_subscription_source_count = len(
                (await db.execute(select(SubscriptionSource))).scalars().all()
            )

            assert first["total"] == 3
            assert first["found"] == 3
            assert first["aborted"] is False
            assert second["found"] == 3
            assert exact_ids.count(240355) == 2
            assert exact_ids.count(240356) == 1
            assert exact_ids.count(240357) == 1
            assert {item["artist_name"] for item in first["items"]} == {
                "yosei_bin",
                "fallback_artist",
                "other_source_artist",
            }
            assert any(
                item["artist_name"] == "Fallback display"
                for item in searches
            )
            pixiv_attempt = next(
                index
                for index, item in enumerate(searches)
                if item["source_url"] == "https://www.pixiv.net/users/111"
            )
            other_source_attempt = next(
                index
                for index, item in enumerate(searches)
                if item["source_url"] == "https://x.com/multi_artist"
            )
            assert pixiv_attempt < other_source_attempt
            assert first_link_count == second_link_count
            assert first_source_creator_count == second_source_creator_count
            assert first_subscription_source_count == second_subscription_source_count

            await db.refresh(subscription)
            assert subscription.schedule_mode == "manual"
            assert subscription.sync_enabled is False
            assert subscription.sync_interval_hours == 12
            assert subscription.scheduled_times == "03:00"
            curated = (await db.execute(select(CreatorLink).where(
                CreatorLink.url == "https://example.com/curated",
            ))).scalar_one()
            assert curated.source == "manual"
            assert curated.is_verified is True
            assert curated.notes == "keep me"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_refresh_all_creator_mappings_aborts_after_danbooru_outage(monkeypatch):
    from app.database import async_session, engine
    from app.models import Creator
    from app.services import creator_enrichment as ce
    from app.services.danbooru import DanbooruUnavailableError

    calls = []

    def _raise(artist_id):
        calls.append(artist_id)
        raise DanbooruUnavailableError("net down")

    monkeypatch.setattr(ce.danbooru_svc, "get_artist", _raise)

    try:
        async with async_session() as db:
            await _clear(db)
            db.add_all([
                Creator(name="first", danbooru_artist_id=1),
                Creator(name="second", danbooru_artist_id=2),
            ])
            await db.commit()

            report = await ce.refresh_all_creator_mappings(db)

            assert report["total"] == 2
            assert report["scanned"] == 1
            assert report["errors"] == 1
            assert report["aborted"] is True
            assert len(calls) == 1
            assert calls[0] in {1, 2}
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
