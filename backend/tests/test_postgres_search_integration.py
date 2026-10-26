"""Real PostgreSQL coverage for the native search port and cutover boundary."""
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings
from app.services import postgres_search_store as store
from app.services import postgres_search_query as query
from app.services.search_index_definitions import WORKS_INDEX, INDEX_SETTINGS


@pytest.fixture
async def pgdb(monkeypatch):
    assert settings.database_url.rsplit("/", 1)[-1].endswith("_test")
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(store, "async_session", factory)
    monkeypatch.setattr(store, "_SCHEMA_READY", False)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE search_pg_documents,search_pg_indexes,search_pg_tasks,search_delivery_receipts,search_rebuilds,search_rebuild_replay,search_projection_outbox,import_curation_outbox RESTART IDENTITY CASCADE"))
    await store._settings(WORKS_INDEX, "PATCH", INDEX_SETTINGS[WORKS_INDEX])
    async with factory() as db:
        yield db
    await engine.dispose()


def document(identity, title, score=0, nsfw=False):
    return {"id": str(identity), "title": title, "posted_ts": score, "created_ts": score,
            "is_nsfw": nsfw, "visibility": "visible", "creator_names": [], "tags": []}


async def test_batch_validation_rolls_back_documents_and_task(pgdb):
    await store._write_documents(WORKS_INDEX, [document("one", "before")])
    before = await pgdb.scalar(text("SELECT count(*) FROM search_pg_tasks"))
    await pgdb.rollback()
    with pytest.raises(ValueError):
        await store._write_documents(WORKS_INDEX, [document("one", "after"), {"title": "missing id"}])
    assert await pgdb.scalar(text("SELECT document->>'title' FROM search_pg_documents WHERE entity_id='one'")) == "before"
    assert await pgdb.scalar(text("SELECT count(*) FROM search_pg_tasks")) == before


async def test_filter_numeric_sort_pagination_exact_total(pgdb):
    await store._write_documents(WORKS_INDEX, [document("a", "blue", 2), document("b", "blue", 10), document("c", "blue", 10), document("adult", "blue", 30, True)])
    args = {"filter": "is_nsfw = false", "sort": ["posted_ts:desc"], "limit": 1}
    first = await query.search_one(pgdb, WORKS_INDEX, "blue", args)
    second = await query.search_one(pgdb, WORKS_INDEX, "blue", {**args, "offset": 1})
    beyond = await query.search_one(pgdb, WORKS_INDEX, "blue", {**args, "offset": 100})
    assert [first.hits[0]["id"], second.hits[0]["id"]] == ["b", "c"]
    assert first.total_hits == second.total_hits == beyond.total_hits == 3
    assert beyond.hits == []


async def test_typo_underscore_cjk_and_long_token_matching(pgdb):
    await store._write_documents(WORKS_INDEX, [document("one", "hoshino_ai 星空 landscape 123456 123456789"), document("two", "a" * 300)])
    for term in ("hoshino_ai", "hoshino a", "星空", "landscpae"):
        hits = await query.search_one(pgdb, WORKS_INDEX, term, {"matching_strategy": "all"})
        assert [d["id"] for d in hits.hits] == ["one"], term
    no_numeric_typo = await query.search_one(pgdb, WORKS_INDEX, "123457", {})
    assert no_numeric_typo.total_hits == 0
    assert (await query.search_one(pgdb, WORKS_INDEX, "123456788", {})).total_hits == 0
    # A realistic long word must not make fuzzystrmatch fail its 255-char limit.
    await query.search_one(pgdb, WORKS_INDEX, "a" * 300, {})
    await query.search_one(pgdb, WORKS_INDEX, "landscape", {})


async def test_atomic_generation_swap_and_delete(pgdb):
    staging = WORKS_INDEX + "__staging_test"
    await store._write_documents(WORKS_INDEX, [document("old", "old")])
    await store._settings(staging, "PATCH", INDEX_SETTINGS[WORKS_INDEX])
    await store._write_documents(staging, [document("new", "new")])
    await store._swap([{"indexes": [WORKS_INDEX, staging]}])
    assert [d["id"] for d in (await query.search_one(pgdb, WORKS_INDEX, "", {})).hits] == ["new"]
    await pgdb.rollback()
    await store._drop(staging)
    await store._delete_documents(WORKS_INDEX, ["new"])
    assert (await query.search_one(pgdb, WORKS_INDEX, "", {})).total_hits == 0


async def test_old_receipt_is_rejected_without_rotating_lease(pgdb, monkeypatch):
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.services import search_delivery
    from app.services.search_backend import SearchBackendMismatch, compatible_commands
    item = SearchDeliveryReceipt(index_uid=WORKS_INDEX, action="upsert", payload=[], versions=[],
                                 state="prepared", backend="meilisearch", lease_token="old-owner")
    pgdb.add(item)
    await pgdb.commit()
    monkeypatch.setattr(search_delivery, "async_session", store.async_session)
    with pytest.raises(SearchBackendMismatch):
        await search_delivery._claim(time.monotonic()+5)
    assert not await compatible_commands(pgdb)
    await pgdb.refresh(item)
    assert item.state == "prepared" and item.lease_token == "old-owner"


async def test_task_retention_is_bounded_and_preserves_active_receipt(pgdb):
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    await store._write_documents(WORKS_INDEX, [document("one", "blue")])
    protected = int(await pgdb.scalar(text("SELECT max(uid) FROM search_pg_tasks")))
    pgdb.add(SearchDeliveryReceipt(index_uid=WORKS_INDEX, backend="postgresql", action="upsert", payload=[], versions=[], state="pending", task_uid=protected))
    await pgdb.execute(text("UPDATE search_pg_tasks SET finished_at=now()-interval '40 days'"))
    await pgdb.commit()
    count = await store.cleanup_tasks(pgdb, datetime.now(timezone.utc)-timedelta(days=30), limit=1)
    assert count == 1
    assert await pgdb.scalar(text("SELECT count(*) FROM search_pg_tasks WHERE uid=:uid"), {"uid": protected}) == 1
    await pgdb.commit()


async def test_native_client_uses_explicit_async_boundary(pgdb):
    from app.services.postgres_search_client import PGClient, run_search_call
    await store._write_documents(WORKS_INDEX, [document("one", "blue")])
    result = await run_search_call(PGClient().index(WORKS_INDEX).search, "blue", db=pgdb)
    assert result.total_hits == 1 and result.hits[0]["id"] == "one"

async def test_sql_timeout_rolls_back_document_and_command(pgdb, monkeypatch):
    import asyncio
    from sqlalchemy.exc import DBAPIError
    await store._write_documents(WORKS_INDEX, [document("one", "before")])
    original = store._task
    async def slow_task(db, *args, **kwargs):
        await db.execute(text("SELECT pg_sleep(0.2)"))
        return await original(db,*args,**kwargs)
    monkeypatch.setattr(store,"_task",slow_task)
    with pytest.raises((TimeoutError, DBAPIError)):
        await store.request(None,"POST",f"/indexes/{WORKS_INDEX}/documents",time.monotonic()+0.03,[document("one","after")])
    assert await pgdb.scalar(text("SELECT document->>'title' FROM search_pg_documents WHERE entity_id='one'")) == "before"


async def test_expired_native_request_never_mutates(pgdb):
    with pytest.raises(TimeoutError):
        await store.request(None,"POST",f"/indexes/{WORKS_INDEX}/documents",time.monotonic()-1,[document("expired","bad")])
    assert not await pgdb.scalar(text("SELECT count(*) FROM search_pg_documents WHERE entity_id='expired'"))

async def test_cancelled_sdk_query_stops_before_session_is_released(pgdb, monkeypatch):
    import asyncio
    from app.services import postgres_search_query
    from app.services.postgres_search_client import PGClient,run_search_call
    started=asyncio.Event()
    async def blocked(db,*args,**kwargs):
        started.set()
        await db.execute(text("SELECT pg_sleep(10)"))
    monkeypatch.setattr(postgres_search_query,"search_one",blocked)
    task=asyncio.create_task(run_search_call(PGClient().index(WORKS_INDEX).search,"blue",db=pgdb))
    await asyncio.wait_for(started.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task,2)
    await pgdb.rollback()
    assert await pgdb.scalar(text("SELECT 1"))==1
    assert not await pgdb.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND query='SELECT pg_sleep(10)' AND state='active'"))
