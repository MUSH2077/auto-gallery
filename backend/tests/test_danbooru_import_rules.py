"""Danbooru entrypoints share identity and persistence rules in real transactions."""
from uuid import UUID

import pytest
from sqlalchemy import select, text

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PIXIV_ONE = "https://www.pixiv.net/users/101"
PIXIV_TWO = "https://www.pixiv.net/en/users/202"


@pytest.fixture
async def db():
    from app.database import async_session, engine
    async with async_session() as session:
        await session.execute(text("TRUNCATE task_runs, creators, users RESTART IDENTITY CASCADE"))
        await session.commit()
        yield session
        await session.rollback()
    await engine.dispose()


@pytest.fixture
def artist(monkeypatch):
    from app.services import danbooru
    from app.jobs import batch_import
    record = {"id": 901, "name": "artist_tag", "other_names": ["Artist"], "urls": [
        {"url": PIXIV_ONE}, {"url": PIXIV_TWO}, {"url": PIXIV_ONE},
        {"url": "https://x.com/artist"}, {"url": "https://space.bilibili.com/303"},
        {"url": "https://www.pixiv.net/users/404", "is_active": False},
    ]}
    monkeypatch.setattr(danbooru, "search_and_extract", lambda **_: (record, danbooru.extract_creator_links(record)))
    class Redis:
        def setex(self, *_):
            pass
        def delete(self, *_):
            pass
    monkeypatch.setattr(batch_import, "get_redis", lambda: Redis())
    return record


async def run_entry(entry, db, job_id=""):
    from app.services.danbooru_import import import_all_danbooru_artist
    from app.jobs.batch_import import _batch_import, _url_batch_import
    if entry == "single":
        return await import_all_danbooru_artist({"pixiv_id": "101"}, db)
    await db.rollback()
    result = await (_batch_import(["101"], job_id) if entry == "pixiv" else _url_batch_import([PIXIV_ONE], job_id))
    assert result["error_count"] == 0, result
    assert result["imported_count"] == 1
    return result["imported"][0]


@pytest.mark.parametrize("entry", ["pixiv", "url"])
async def test_batch_producers_return_the_complete_monitor_result_contract(db, artist, entry):
    from app.jobs.batch_import import _batch_import, _url_batch_import
    await db.rollback()
    result = await (_batch_import(["101"], "") if entry == "pixiv" else _url_batch_import([PIXIV_ONE], ""))
    assert result["total"] == result["imported_count"] == 1
    assert result["error_count"] == 0
    for name in ("imported", "low_confidence", "not_found", "errors"):
        assert isinstance(result[name], list)
    assert result["low_confidence"] == []
    assert result["low_confidence_count"] == 0


@pytest.mark.parametrize("entry", ["single", "pixiv", "url"])
async def test_all_entrypoints_preserve_urls_and_resolvable_source_identities(db, artist, entry):
    from app.models import Creator, CreatorLink, SourceCreator, Subscription, SubscriptionSource
    result = await run_entry(entry, db)
    creator = (await db.execute(select(Creator))).scalar_one()
    assert creator.danbooru_artist_id == artist["id"]
    subscription = (await db.execute(select(Subscription))).scalar_one()
    assert subscription.creator_id == creator.id
    sources = list((await db.execute(select(SubscriptionSource))).scalars())
    assert {(s.source, s.source_url, s.source_creator_id, s.is_enabled) for s in sources} == {
        ("pixiv", PIXIV_ONE, "101", True), ("pixiv", PIXIV_TWO, "202", True),
        ("x", "https://x.com/artist", None, False),
        ("danbooru", "https://danbooru.donmai.us/posts?tags=artist_tag", "artist_tag", False),
    }
    identities = list((await db.execute(select(SourceCreator))).scalars())
    assert {(s.source, s.source_creator_id) for s in identities} == {
        ("pixiv", "101"), ("pixiv", "202"), ("bilibili", "303"), ("danbooru", "artist_tag"),
    }
    assert all(s.creator_id == creator.id for s in identities)
    links = list((await db.execute(select(CreatorLink))).scalars())
    assert len(links) == len({link.url for link in links})
    assert UUID(result["creator_id"]) == creator.id


@pytest.mark.parametrize("entry", ["pixiv", "url"])
async def test_reimport_after_single_accepts_multiple_sources_without_duplicates(db, artist, entry):
    from app.models import Creator, SubscriptionSource
    first = await run_entry("single", db)
    result = await run_entry(entry, db)
    assert result["creator_id"] == first["creator_id"]
    assert result["sources_created"] == result["links_imported"] == 0
    assert len(list((await db.execute(select(SubscriptionSource))).scalars())) == 4
    assert len(list((await db.execute(select(Creator))).scalars())) == 1


@pytest.mark.parametrize("entry", ["single", "pixiv", "url"])
async def test_existing_names_and_enablement_survive_identity_backfill(db, artist, entry):
    from app.models import Creator, SourceCreator, SubscriptionSource
    db.add(SourceCreator(source="pixiv", source_creator_id="101", display_name="Native artist"))
    await db.commit()
    result = await run_entry(entry, db)
    creator = await db.get(Creator, UUID(result["creator_id"]))
    assert creator.display_name == "Native artist"
    creator.display_name = "Curated name"
    sources = list((await db.execute(select(SubscriptionSource))).scalars())
    source = next(s for s in sources if s.source_url == PIXIV_TWO)
    source.source_creator_id, source.is_enabled = None, False
    identity = (await db.execute(select(SourceCreator).where(
        SourceCreator.source == "pixiv", SourceCreator.source_creator_id == "202"))).scalar_one()
    identity.creator_id = None
    await db.commit()
    await run_entry(entry, db)
    await db.refresh(creator)
    await db.refresh(source)
    await db.refresh(identity)
    assert creator.display_name == "Curated name"
    assert (source.source_creator_id, source.is_enabled, identity.creator_id) == ("202", False, creator.id)


async def test_explicit_creator_name_and_identity_ownership_are_preserved(db, artist):
    from app.models import Creator, SourceCreator
    from app.services.danbooru_import import import_all_danbooru_artist
    other = Creator(name="Other")
    target = Creator(name="Target", display_name="Original")
    db.add_all([other, target])
    await db.flush()
    native = SourceCreator(source="pixiv", source_creator_id="101", creator_id=other.id)
    db.add(native)
    await db.commit()
    result = await import_all_danbooru_artist({"pixiv_id": "101", "creator_id": str(target.id), "creator_name": "Chosen"}, db)
    assert result["creator_id"] == str(target.id)
    await db.refresh(target)
    await db.refresh(native)
    assert target.display_name == "Chosen"
    assert native.creator_id == other.id


@pytest.mark.parametrize("entry", ["single", "pixiv", "url"])
async def test_explicit_auto_enable_policy_is_shared(db, artist, monkeypatch, entry):
    from app.models import SubscriptionSource
    from app.services import danbooru_import
    monkeypatch.setattr(danbooru_import, "_get_auto_enable_sources", lambda: {"pixiv", "danbooru"})
    await run_entry(entry, db)
    source = (await db.execute(select(SubscriptionSource).where(SubscriptionSource.source == "danbooru"))).scalar_one()
    assert source.is_enabled is True


@pytest.mark.parametrize("entry", ["pixiv", "url"])
async def test_batch_partial_failure_retains_committed_items(db, artist, monkeypatch, entry):
    from app.models import Creator
    from app.services import danbooru
    from app.jobs.batch_import import _batch_import, _url_batch_import
    inputs = ["101", "202", "303"]
    def resolved(**kwargs):
        value = kwargs.get("pixiv_id") or kwargs["source_url"].split("/")[-1]
        record = {"id": int(value), "name": f"artist_{value}", "urls": [{"url": f"https://www.pixiv.net/users/{value}"}]}
        links = danbooru.extract_creator_links(record)
        if value == "202":
            del links[0]["confidence"]
        return record, links
    monkeypatch.setattr(danbooru, "search_and_extract", resolved)
    result = await (_batch_import(inputs, "") if entry == "pixiv" else _url_batch_import(
        [f"https://www.pixiv.net/users/{value}" for value in inputs], ""))
    assert (result["imported_count"], result["error_count"]) == (2, 1)
    assert {c.name for c in (await db.execute(select(Creator))).scalars()} == {"artist_101", "artist_303"}


async def test_pixiv_confidence_gate_stays_in_entrypoint(db, artist, monkeypatch):
    from app.models import Creator, SourceCreator, SubscriptionSource
    from app.services import danbooru
    from app.jobs.batch_import import _batch_import
    record = {"id": 901, "name": "artist_tag", "urls": []}
    monkeypatch.setattr(danbooru, "search_and_extract", lambda **_: (record, danbooru.extract_creator_links(record)))
    result = await _batch_import(["101"], "")
    assert (result["imported_count"], result["low_confidence_count"], result["error_count"]) == (0, 1, 0)
    assert not list((await db.execute(select(Creator))).scalars())
    # URL and explicit single imports retain their existing ability to create
    # an artist with only the Danbooru tag source, without guessing Pixiv IDs.
    result = await run_entry("url", db)
    assert result["created_new"] is True
    assert [(s.source, s.source_creator_id) for s in (await db.execute(select(SubscriptionSource))).scalars()] == [("danbooru", "artist_tag")]
    assert [(s.source, s.source_creator_id) for s in (await db.execute(select(SourceCreator))).scalars()] == [("danbooru", "artist_tag")]


@pytest.mark.parametrize("entry", ["single", "pixiv", "url"])
async def test_attempt_superseded_after_application_rolls_back_domain_and_projection(db, artist, monkeypatch, entry):
    from app.database import async_session
    from app.models import Creator, CreatorAlias, CreatorLink, SourceCreator, Subscription, SubscriptionSource, TaskRun, SearchIndexState
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services import danbooru_import, operations
    operation = {"single": "danbooru-import-all", "pixiv": "admin-danbooru-batch-import", "url": "admin-danbooru-url-batch-import"}[entry]
    scope = {"single": "danbooru:import-all:", "pixiv": "danbooru:batch-import:", "url": "danbooru:url-batch-import:"}[entry]
    prepared = await operations.prepare_admin_operation(db, operation_type=operation, scope_key=f"{scope}r3",
        title="Import", entity="creators", options={}, queue_name="imports")
    task_id, attempt = prepared.task.id, prepared.task.attempts
    await db.commit()
    projection_before = list((await db.execute(select(SearchProjectionOutbox.__table__).order_by(SearchProjectionOutbox.id))).mappings())
    index_before = list((await db.execute(select(SearchIndexState.__table__).order_by(SearchIndexState.id))).mappings())
    await db.rollback()
    applied = danbooru_import.persist_danbooru_artist
    invalidations = []
    monkeypatch.setattr(danbooru_import, "invalidate_creator_subscription_caches", lambda: invalidations.append("single"))
    from app.jobs import batch_import
    monkeypatch.setattr(batch_import, "invalidate_creator_subscription_caches", lambda: invalidations.append("batch"))

    async def apply_then_supersede(*args, **kwargs):
        result = await applied(*args, **kwargs)
        async with async_session() as replacement:
            current = await replacement.get(TaskRun, task_id, with_for_update=True)
            current.attempts = attempt + 1
            current.meta = {**current.meta, "admin_dispatch": {**current.meta["admin_dispatch"], "attempt": attempt + 1}}
            await replacement.commit()
        return result

    monkeypatch.setattr(danbooru_import, "persist_danbooru_artist", apply_then_supersede)
    with operations.admin_operation_attempt_context(task_id, attempt), pytest.raises(operations.AdminOperationAttemptRejected):
        async with async_session() as worker:
            await run_entry(entry, worker, job_id=str(task_id))
    for model in (Creator, CreatorAlias, CreatorLink, SourceCreator, Subscription, SubscriptionSource):
        assert not list((await db.execute(select(model))).scalars())
    assert list((await db.execute(select(SearchProjectionOutbox.__table__).order_by(SearchProjectionOutbox.id))).mappings()) == projection_before
    assert list((await db.execute(select(SearchIndexState.__table__).order_by(SearchIndexState.id))).mappings()) == index_before
    assert invalidations == []
    assert (await db.get(TaskRun, task_id)).attempts == attempt + 1


@pytest.mark.parametrize("entry", ["pixiv", "url"])
async def test_batch_commit_failure_rolls_back_without_cache_invalidation(db, artist, monkeypatch, entry):
    from app.database import async_session
    from app.models import Creator
    from app.jobs import batch_import
    events = []
    def sessions():
        session = async_session()
        async def commit():
            raise RuntimeError("injected commit failure")
        monkeypatch.setattr(session, "commit", commit)
        return session
    monkeypatch.setattr(batch_import, "async_session", sessions)
    monkeypatch.setattr(batch_import, "invalidate_creator_subscription_caches", lambda: events.append("cache"))
    result = await (batch_import._batch_import(["101"], "") if entry == "pixiv" else batch_import._url_batch_import([PIXIV_ONE], ""))
    assert result["imported_count"] == 0 and result["error_count"] == 1
    assert result["errors"][0]["error"] == "injected commit failure"
    assert not list((await db.execute(select(Creator))).scalars())
    assert events == []


@pytest.mark.parametrize("entry", ["single", "pixiv", "url"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_creator_matching_checks_later_identities_regardless_of_url_order(db, artist, monkeypatch, entry, reverse):
    from app.models import Creator, SourceCreator
    from app.services import danbooru
    existing = Creator(name="Existing", display_name="Curated")
    db.add(existing)
    await db.flush()
    db.add(SourceCreator(source="pixiv", source_creator_id="101", creator_id=existing.id))
    await db.commit()
    record = {"id": 901, "name": "artist_tag", "urls": [
        {"url": "https://space.bilibili.com/303"}, {"url": PIXIV_ONE},
    ]}
    if reverse:
        record["urls"].reverse()
    monkeypatch.setattr(danbooru, "search_and_extract", lambda **_: (record, danbooru.extract_creator_links(record)))
    result = await run_entry(entry, db)
    assert result["creator_id"] == str(existing.id)
    creators = list((await db.execute(select(Creator))).scalars())
    assert len(creators) == 1
    identities = list((await db.execute(select(SourceCreator))).scalars())
    assert {(row.source, row.source_creator_id, row.creator_id) for row in identities} == {
        ("pixiv", "101", existing.id), ("bilibili", "303", existing.id), ("danbooru", "artist_tag", existing.id),
    }


@pytest.mark.parametrize("entry", ["single", "pixiv", "url"])
@pytest.mark.parametrize("conflict", ["native", "danbooru", "link", "tag", "duplicate_danbooru"])
async def test_conflicting_artist_matches_fail_before_mutations(db, artist, monkeypatch, entry, conflict):
    from fastapi import HTTPException
    from app.models import Creator, CreatorAlias, CreatorLink, SourceCreator, Subscription, SubscriptionSource, SearchIndexState
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services import danbooru
    from app.jobs.batch_import import _batch_import, _url_batch_import
    from app.services.danbooru_import import import_all_danbooru_artist
    first, second = Creator(name="First"), Creator(name="Second")
    db.add_all([first, second])
    await db.flush()
    db.add(SourceCreator(source="pixiv", source_creator_id="101", creator_id=first.id))
    if conflict == "native":
        db.add(SourceCreator(source="bilibili", source_creator_id="303", creator_id=second.id))
    elif conflict in {"danbooru", "duplicate_danbooru"}:
        second.danbooru_artist_id = 901
        if conflict == "duplicate_danbooru":
            first.danbooru_artist_id = 901
    elif conflict == "link":
        db.add(CreatorLink(creator_id=second.id, url="https://x.com/artist", link_type="x"))
    else:
        db.add(SourceCreator(source="danbooru", source_creator_id="artist_tag", creator_id=second.id))
    await db.commit()
    models = (Creator, CreatorAlias, CreatorLink, SourceCreator, Subscription, SubscriptionSource, SearchProjectionOutbox, SearchIndexState)
    before = [list((await db.execute(select(model.__table__).order_by(model.id))).mappings()) for model in models]
    record = {"id": 901, "name": "artist_tag", "urls": [
        {"url": PIXIV_ONE}, {"url": "https://space.bilibili.com/303"}, {"url": "https://x.com/artist"},
    ]}
    monkeypatch.setattr(danbooru, "search_and_extract", lambda **_: (record, danbooru.extract_creator_links(record)))
    if entry == "single":
        with pytest.raises(HTTPException) as error:
            await import_all_danbooru_artist({"pixiv_id": "101"}, db)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "danbooru_creator_conflict"
        await db.rollback()
    else:
        await db.rollback()
        result = await (_batch_import(["101"], "") if entry == "pixiv" else _url_batch_import([PIXIV_ONE], ""))
        assert (result["imported_count"], result["error_count"]) == (0, 1)
        assert "danbooru_creator_conflict" in result["errors"][0]["error"]
    await db.refresh(first)
    assert first.danbooru_artist_id == (901 if conflict == "duplicate_danbooru" else None)
    assert len(list((await db.execute(select(Creator))).scalars())) == 2
    assert not list((await db.execute(select(Subscription))).scalars())
    original = (await db.execute(select(SourceCreator).where(SourceCreator.source == "pixiv"))).scalar_one()
    assert original.creator_id == first.id
    after = [list((await db.execute(select(model.__table__).order_by(model.id))).mappings()) for model in models]
    assert after == before


@pytest.mark.parametrize("entry", ["pixiv", "url"])
async def test_batch_identity_conflict_preserves_other_successful_items(db, artist, monkeypatch, entry):
    from app.models import Creator, SourceCreator, Subscription
    from app.services import danbooru
    from app.jobs.batch_import import _batch_import, _url_batch_import
    first, second = Creator(name="First"), Creator(name="Second")
    db.add_all([first, second])
    await db.flush()
    db.add_all([
        SourceCreator(source="pixiv", source_creator_id="101", creator_id=first.id),
        SourceCreator(source="bilibili", source_creator_id="303", creator_id=second.id),
    ])
    await db.commit()
    def resolved(**kwargs):
        value = kwargs.get("pixiv_id") or kwargs["source_url"].split("/")[-1]
        record = {"id": int(value), "name": f"artist_{value}", "urls": [{"url": f"https://www.pixiv.net/users/{value}"}]}
        if value == "101":
            record["urls"].append({"url": "https://space.bilibili.com/303"})
        return record, danbooru.extract_creator_links(record)
    monkeypatch.setattr(danbooru, "search_and_extract", resolved)
    result = await (_batch_import(["404", "101"], "") if entry == "pixiv" else _url_batch_import(
        ["https://www.pixiv.net/users/404", PIXIV_ONE], ""))
    assert (result["imported_count"], result["error_count"]) == (1, 1)
    assert "danbooru_creator_conflict" in result["errors"][0]["error"]
    assert len(list((await db.execute(select(Creator))).scalars())) == 3
    assert len(list((await db.execute(select(Subscription))).scalars())) == 1


@pytest.mark.parametrize("entry", ["single", "pixiv", "url"])
async def test_consistent_native_danbooru_and_link_matches_reuse_one_creator(db, artist, entry):
    from app.models import Creator, CreatorLink, SourceCreator
    existing = Creator(name="Existing", danbooru_artist_id=artist["id"])
    db.add(existing)
    await db.flush()
    db.add_all([
        SourceCreator(source="pixiv", source_creator_id="101", creator_id=existing.id),
        SourceCreator(source="bilibili", source_creator_id="303", creator_id=existing.id),
        CreatorLink(creator_id=existing.id, url="https://x.com/artist", link_type="x"),
    ])
    await db.commit()
    assert (await run_entry(entry, db))["creator_id"] == str(existing.id)
    assert len(list((await db.execute(select(Creator))).scalars())) == 1


@pytest.mark.parametrize("entry", ["single", "pixiv", "url"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_new_creator_name_and_native_url_have_stable_priority(db, artist, monkeypatch, entry, reverse):
    from app.models import Creator, SourceCreator
    from app.services import danbooru
    db.add_all([
        SourceCreator(source="bilibili", source_creator_id="303", display_name="Bilibili name"),
        SourceCreator(source="pixiv", source_creator_id="101", display_name="Pixiv name"),
    ])
    await db.commit()
    alternate = "https://www.pixiv.net/en/users/101"
    record = {"id": 901, "name": "artist_tag", "urls": [
        {"url": "https://space.bilibili.com/303"}, {"url": alternate}, {"url": PIXIV_ONE},
    ]}
    if reverse:
        record["urls"].reverse()
    monkeypatch.setattr(danbooru, "search_and_extract", lambda **_: (record, danbooru.extract_creator_links(record)))
    result = await run_entry(entry, db)
    assert (await db.get(Creator, UUID(result["creator_id"]))).display_name == "Pixiv name"
    native = (await db.execute(select(SourceCreator).where(SourceCreator.source == "pixiv"))).scalar_one()
    assert native.source_url is None  # Existing metadata is preserved, not rewritten.
    await db.delete(native)
    await db.commit()
    await run_entry(entry, db)
    created = (await db.execute(select(SourceCreator).where(SourceCreator.source == "pixiv"))).scalar_one()
    assert created.source_url == min(PIXIV_ONE, alternate)
