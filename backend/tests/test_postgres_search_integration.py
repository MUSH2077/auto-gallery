"""Real PostgreSQL coverage for the native search port and cutover boundary."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
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


@pytest.mark.parametrize("action", ["upsert", "delete"])
async def test_atomic_delivery_commits_pg_task_receipt_and_outbox(pgdb, monkeypatch, action):
    from contextlib import asynccontextmanager
    from sqlalchemy import false
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services import search_delivery as delivery
    from app.services import search_rebuild

    @asynccontextmanager
    async def opened_session():
        yield pgdb

    monkeypatch.setattr(delivery, "async_session", opened_session)
    monkeypatch.setattr(search_rebuild, "membership_bootstrap_due_condition", lambda: false())
    if action == "delete":
        await store._write_documents(WORKS_INDEX, [document("atomic-one", "before")])
    token = "t20-test-token"
    before_tasks = await pgdb.scalar(text("SELECT count(*) FROM search_pg_tasks WHERE index_uid=:uid"), {"uid": WORKS_INDEX})
    outbox = SearchProjectionOutbox(
        index_uid=WORKS_INDEX, entity_id="atomic-one", action=action, version=7,
    )
    receipt = SearchDeliveryReceipt(
        backend="postgresql", index_uid=WORKS_INDEX, action=action,
        versions=[], payload=([document("atomic-one", "atomic")] if action == "upsert" else ["atomic-one"]), state="prepared",
        phase="documents", lease_token=token,
        lease_until=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    pgdb.add_all([outbox, receipt])
    await pgdb.flush()
    receipt.versions = [[str(outbox.id), outbox.version]]
    await pgdb.commit()

    result = await delivery._apply_database_receipt(
        receipt, token, time.monotonic() + 10,
    )
    assert result["status"] == "ok"
    await pgdb.rollback()
    refreshed = await pgdb.get(SearchDeliveryReceipt, receipt.id)
    pending_completed_at = await pgdb.scalar(
        select(SearchProjectionOutbox.completed_at).where(
            SearchProjectionOutbox.id == outbox.id
        )
    )
    assert refreshed.state == "complete"
    assert pending_completed_at is not None
    assert await pgdb.scalar(text(
        "SELECT document->>'title' FROM search_pg_documents WHERE entity_id='atomic-one'"
    )) == ("atomic" if action == "upsert" else None)
    assert await pgdb.scalar(text(
        "SELECT count(*) FROM search_pg_tasks WHERE index_uid=:uid"
    ), {"uid": WORKS_INDEX}) == before_tasks + 1


async def _seed_atomic_receipt(pgdb, *, token, lease_until, version=7):
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_projection_outbox import SearchProjectionOutbox

    outbox = SearchProjectionOutbox(
        index_uid=WORKS_INDEX, entity_id=f"atomic-{token}", action="upsert", version=version,
    )
    receipt = SearchDeliveryReceipt(
        backend="postgresql", index_uid=WORKS_INDEX, action="upsert",
        versions=[], payload=[document(f"atomic-{token}", "atomic")], state="prepared",
        phase="documents", lease_token=token, lease_until=lease_until,
    )
    pgdb.add_all([outbox, receipt])
    await pgdb.flush()
    receipt.versions = [[str(outbox.id), outbox.version]]
    receipt_id, outbox_id = receipt.id, outbox.id
    await pgdb.commit()
    return receipt_id, outbox_id


async def test_atomic_delivery_rolls_back_document_and_task_on_precommit_error(pgdb, monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import false
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services import postgres_search_store as store
    from app.services import search_delivery as delivery
    from app.services import search_rebuild

    token = "atomic-rollback"
    before_tasks = await pgdb.scalar(text("SELECT count(*) FROM search_pg_tasks WHERE index_uid=:uid"), {"uid": WORKS_INDEX})
    receipt_id, outbox_id = await _seed_atomic_receipt(
        pgdb, token=token, lease_until=datetime.now(timezone.utc) + timedelta(seconds=30)
    )
    monkeypatch.setattr(search_rebuild, "membership_bootstrap_due_condition", lambda: false())
    monkeypatch.setattr(
        delivery, "async_session", async_sessionmaker(pgdb.bind, expire_on_commit=False)
    )

    async def fail_task(*args, **kwargs):
        raise RuntimeError("task history failure")

    monkeypatch.setattr(store, "_task", fail_task)
    with pytest.raises(RuntimeError, match="task history failure"):
        await delivery._apply_database_receipt(
            SimpleNamespace(id=receipt_id, action="upsert"), token, time.monotonic() + 10
        )
    await pgdb.rollback()
    assert await pgdb.scalar(text(
        "SELECT count(*) FROM search_pg_documents WHERE entity_id=:id"
    ), {"id": f"atomic-{token}"}) == 0
    assert await pgdb.scalar(text(
        "SELECT count(*) FROM search_pg_tasks WHERE index_uid=:uid"
    ), {"uid": WORKS_INDEX}) == before_tasks
    assert await pgdb.scalar(
        select(SearchDeliveryReceipt.state).where(SearchDeliveryReceipt.id == receipt_id)
    ) == "prepared"
    assert await pgdb.scalar(
        select(SearchProjectionOutbox.completed_at).where(SearchProjectionOutbox.id == outbox_id)
    ) is None


async def test_atomic_delivery_does_not_ack_newer_outbox_version(pgdb, monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy import false
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services import search_delivery as delivery
    from app.services import search_rebuild

    token = "atomic-cas"
    receipt_id, outbox_id = await _seed_atomic_receipt(
        pgdb, token=token, lease_until=datetime.now(timezone.utc) + timedelta(seconds=30)
    )
    outbox = await pgdb.get(SearchProjectionOutbox, outbox_id)
    outbox.version = 8
    await pgdb.commit()
    monkeypatch.setattr(search_rebuild, "membership_bootstrap_due_condition", lambda: false())

    result = await delivery._apply_database_receipt(
        SimpleNamespace(id=receipt_id, action="upsert"), token, time.monotonic() + 10
    )
    assert result["status"] == "ok"
    await pgdb.rollback()
    version, completed_at = (await pgdb.execute(
        select(SearchProjectionOutbox.version, SearchProjectionOutbox.completed_at).where(
            SearchProjectionOutbox.id == outbox_id
        )
    )).one()
    assert version == 8 and completed_at is None
    assert await pgdb.scalar(
        select(SearchDeliveryReceipt.state).where(SearchDeliveryReceipt.id == receipt_id)
    ) == "complete"


async def test_atomic_delivery_rejects_expired_execution_lease(pgdb, monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy import false
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services import search_delivery as delivery
    from app.services import search_rebuild

    token = "atomic-expired"
    receipt_id, outbox_id = await _seed_atomic_receipt(
        pgdb, token=token, lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    monkeypatch.setattr(search_rebuild, "membership_bootstrap_due_condition", lambda: false())

    with pytest.raises(RuntimeError, match="lease expired"):
        await delivery._apply_database_receipt(
            SimpleNamespace(id=receipt_id, action="upsert"), token, time.monotonic() + 10
        )
    await pgdb.rollback()
    assert await pgdb.scalar(select(SearchDeliveryReceipt.state).where(
        SearchDeliveryReceipt.id == receipt_id
    )) == "prepared"
    assert await pgdb.scalar(select(SearchProjectionOutbox.completed_at).where(
        SearchProjectionOutbox.id == outbox_id
    )) is None
    assert await pgdb.scalar(text(
        "SELECT count(*) FROM search_pg_documents WHERE entity_id=:id"
    ), {"id": f"atomic-{token}"}) == 0


async def test_atomic_delivery_permit_recheck_defers_before_remote_advance(pgdb, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import select
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.services import heavy_io
    from app.services import search_delivery as delivery

    token = "atomic-recheck"
    receipt_id, _ = await _seed_atomic_receipt(
        pgdb, token=token, lease_until=datetime.now(timezone.utc) + timedelta(seconds=30)
    )
    factory = async_sessionmaker(pgdb.bind, expire_on_commit=False)
    monkeypatch.setattr(delivery, "async_session", factory)
    monkeypatch.setattr(
        delivery, "uses_database_permit", AsyncMock(side_effect=[True, False])
    )

    @asynccontextmanager
    async def capacity(*args, **kwargs):
        yield SimpleNamespace(work_units=kwargs["max_work_units"])

    monkeypatch.setattr(heavy_io, "adaptive_resource_slice", capacity)
    outcome = await delivery._admitted(
        1, None, time.monotonic() + 10,
        SimpleNamespace(id=receipt_id, action="upsert"), token,
    )
    assert outcome["status"] == "deferred"
    await pgdb.rollback()
    assert await pgdb.scalar(select(SearchDeliveryReceipt.state).where(
        SearchDeliveryReceipt.id == receipt_id
    )) == "prepared"


async def _seed_uuid_receipt(pgdb, *, token, action="upsert"):
    """Seed a receipt whose work/entity id is a real UUID for lookahead SQL."""
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_projection_outbox import SearchProjectionOutbox

    from app.models.repository_sync_receipt import SearchIndexState
    from sqlalchemy import update
    await pgdb.execute(update(SearchIndexState).values(indexed_generation=SearchIndexState.database_generation))
    entity_id = str(uuid4())
    outbox = SearchProjectionOutbox(
        index_uid=WORKS_INDEX, entity_id=entity_id, action=action, version=7,
    )
    payload = [document(entity_id, "atomic lookahead")] if action == "upsert" else [entity_id]
    receipt = SearchDeliveryReceipt(
        backend="postgresql", index_uid=WORKS_INDEX, action=action,
        versions=[], payload=payload, state="prepared", phase="documents",
        lease_token=token,
        lease_until=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    pgdb.add_all([outbox, receipt])
    await pgdb.flush()
    receipt.versions = [[str(outbox.id), outbox.version]]
    await pgdb.commit()
    return receipt.id, outbox.id, entity_id


async def _apply_with_pgdb(pgdb, monkeypatch, receipt_id, token):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.services import search_delivery as delivery, search_rebuild
    from sqlalchemy import false
    monkeypatch.setattr(search_rebuild, "membership_bootstrap_due_condition", lambda: false())

    @asynccontextmanager
    async def opened_session():
        yield pgdb

    monkeypatch.setattr(delivery, "async_session", opened_session)
    return await delivery._apply_database_receipt(
        SimpleNamespace(id=receipt_id, action="upsert"), token, time.monotonic() + 10
    )


async def test_database_receipt_lookahead_empty_returns_no_successor(pgdb, monkeypatch):
    """The last ready receipt reports no successor after its atomic commit."""
    from sqlalchemy import select
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_projection_outbox import SearchProjectionOutbox

    receipt_id, outbox_id, entity_id = await _seed_uuid_receipt(
        pgdb, token="lookahead-empty"
    )
    result = await _apply_with_pgdb(pgdb, monkeypatch, receipt_id, "lookahead-empty")
    assert result["status"] == "ok"
    assert result["more_likely"] is False
    assert result["database_receipt_committed"] is True
    await pgdb.rollback()
    assert await pgdb.scalar(select(SearchProjectionOutbox.completed_at).where(
        SearchProjectionOutbox.id == outbox_id
    )) is not None
    assert await pgdb.scalar(select(SearchDeliveryReceipt.state).where(
        SearchDeliveryReceipt.id == receipt_id
    )) == "complete"
    assert await pgdb.scalar(text(
        "SELECT count(*) FROM search_pg_documents WHERE entity_id=:id"
    ), {"id": entity_id}) == 1


@pytest.mark.parametrize("action", ["upsert", "delete"])
async def test_database_receipt_lookahead_ready_upsert_or_delete_keeps_successor(
    pgdb, monkeypatch, action,
):
    """A second ready action keeps the successor hint for either action type."""
    from app.models.search_projection_outbox import SearchProjectionOutbox

    receipt_id, _outbox_id, _entity_id = await _seed_uuid_receipt(
        pgdb, token="lookahead-ready-" + action
    )
    ready_id = str(uuid4())
    pgdb.add(SearchProjectionOutbox(
        index_uid=WORKS_INDEX, entity_id=ready_id, action=action, version=1,
        available_at=datetime.now(timezone.utc),
    ))
    await pgdb.commit()
    result = await _apply_with_pgdb(pgdb, monkeypatch, receipt_id, "lookahead-ready-" + action)
    assert result["status"] == "ok"
    assert result["more_likely"] is True
    assert result["database_receipt_committed"] is True


async def test_database_receipt_lookahead_excludes_uncurated_work_but_keeps_delete(
    pgdb, monkeypatch,
):
    """An uncurated work upsert is not ready, while a delete remains drainable."""
    from app.models import ImportCurationOutbox, Work
    from app.models.search_projection_outbox import SearchProjectionOutbox

    receipt_id, _outbox_id, _entity_id = await _seed_uuid_receipt(
        pgdb, token="lookahead-unrelated"
    )
    work = Work(title="not curated yet")
    pgdb.add(work)
    await pgdb.flush()
    pgdb.add(ImportCurationOutbox(
        work_id=work.id, state="pending", source=None, source_work_id=None,
        metadata_state="complete",
    ))
    pgdb.add(SearchProjectionOutbox(
        index_uid=WORKS_INDEX, entity_id=str(work.id), action="upsert", version=1,
        available_at=datetime.now(timezone.utc),
    ))
    await pgdb.commit()
    result = await _apply_with_pgdb(pgdb, monkeypatch, receipt_id, "lookahead-unrelated")
    assert result["more_likely"] is False

    # Replace only the uncurated upsert with a delete; the delete is explicitly
    # drainable even while the work curation intent remains pending.
    row = (await pgdb.execute(select(SearchProjectionOutbox).where(
        SearchProjectionOutbox.entity_id == str(work.id)
    ))).scalar_one()
    row.action = "delete"
    await pgdb.commit()
    receipt_id, _outbox_id, _entity_id = await _seed_uuid_receipt(
        pgdb, token="lookahead-delete"
    )
    result = await _apply_with_pgdb(pgdb, monkeypatch, receipt_id, "lookahead-delete")
    assert result["more_likely"] is True


async def test_database_receipt_lookahead_failure_rolls_back_everything(pgdb, monkeypatch):
    """A lookahead error cannot leave documents, task, receipt or outbox half-committed."""
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services import search_delivery as delivery

    receipt_id, outbox_id, entity_id = await _seed_uuid_receipt(
        pgdb, token="lookahead-error"
    )
    before_tasks = await pgdb.scalar(text("SELECT count(*) FROM search_pg_tasks WHERE index_uid=:uid"), {"uid": WORKS_INDEX})
    from app.services import outbox_coordinator
    def fail_lookahead(*_args, **_kwargs):
        raise RuntimeError("injected lookahead failure")
    monkeypatch.setattr(outbox_coordinator, "_search_ready", fail_lookahead)
    with pytest.raises(RuntimeError, match="injected lookahead failure"):
        await _apply_with_pgdb(pgdb, monkeypatch, receipt_id, "lookahead-error")
    await pgdb.rollback()
    assert await pgdb.scalar(text(
        "SELECT count(*) FROM search_pg_documents WHERE entity_id=:id"
    ), {"id": entity_id}) == 0
    assert await pgdb.scalar(text(
        "SELECT count(*) FROM search_pg_tasks WHERE index_uid=:uid"
    ), {"uid": WORKS_INDEX}) == before_tasks
    assert await pgdb.scalar(select(SearchDeliveryReceipt.state).where(
        SearchDeliveryReceipt.id == receipt_id
    )) == "prepared"
    assert await pgdb.scalar(select(SearchProjectionOutbox.completed_at).where(
        SearchProjectionOutbox.id == outbox_id
    )) is None


async def test_two_pg_receipts_commit_independently_when_second_fails(pgdb, monkeypatch):
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_projection_outbox import SearchProjectionOutbox
    first_id, _first_outbox, first_entity = await _seed_uuid_receipt(pgdb, token="bounded-first")
    second_entity = str(uuid4())
    second_outbox = SearchProjectionOutbox(
        index_uid=WORKS_INDEX, entity_id=second_entity, action="delete", version=1,
    )
    pgdb.add(second_outbox)
    delivery = await _pg_run_environment(pgdb, monkeypatch, receipt_id=first_id, units=2)
    second_outbox_id = second_outbox.id

    async def fail_second(*_args):
        raise RuntimeError("injected second receipt failure")

    monkeypatch.setattr(store, "_delete_documents_in_session", fail_second)
    with pytest.raises(RuntimeError, match="injected second receipt failure"):
        await delivery.run_delivery_slice(limit=2)
    await pgdb.rollback()
    assert await pgdb.scalar(select(SearchDeliveryReceipt.state).where(
        SearchDeliveryReceipt.id == first_id
    )) == "complete"
    second = (await pgdb.execute(select(SearchDeliveryReceipt).where(
        SearchDeliveryReceipt.id != first_id
    ))).scalar_one()
    assert second.state == "prepared"
    assert second.versions == [[str(second_outbox_id), 1]]
    assert await pgdb.scalar(text(
        "SELECT count(*) FROM search_pg_documents WHERE entity_id=:id"
    ), {"id": first_entity}) == 1
    assert await pgdb.scalar(select(SearchProjectionOutbox.completed_at).where(
        SearchProjectionOutbox.id == second_outbox_id
    )) is None



async def test_database_receipt_yields_to_real_pending_curation(pgdb, monkeypatch):
    from app.models import ImportCurationOutbox, Work
    from app.models.search_projection_outbox import SearchProjectionOutbox
    receipt_id, _outbox_id, _entity_id = await _seed_uuid_receipt(pgdb, token="bounded-curation")
    work = Work(title="pending curation fairness")
    pgdb.add(work)
    await pgdb.flush()
    pgdb.add(ImportCurationOutbox(
        work_id=work.id, state="pending", metadata_state="complete",
    ))
    ready = SearchProjectionOutbox(
        index_uid=WORKS_INDEX, entity_id=str(uuid4()), action="delete", version=1,
    )
    pgdb.add(ready)
    delivery = await _pg_run_environment(pgdb, monkeypatch, receipt_id=receipt_id)
    outcome = await delivery.run_delivery_slice(limit=500)
    assert outcome["database_receipt_committed"] is True
    assert outcome["curation_waiting"] is True
    assert outcome["more_likely"] is True
    assert outcome["processed"] == 1
    assert await pgdb.scalar(select(SearchProjectionOutbox.completed_at).where(
        SearchProjectionOutbox.id == ready.id
    )) is None



class _RealGrant:
    def __init__(self, units, seconds):
        self.work_units = units
        self.slice_seconds = seconds


@pytest.mark.asyncio
async def test_pg_run_entry_respects_first_real_grant_without_refilling(pgdb, monkeypatch):
    """A real adaptive grant of one item prevents a second receipt claim."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services import heavy_io, search_delivery as delivery

    first_id, _first_outbox, _first_entity = await _seed_uuid_receipt(pgdb, token="grant-one-first")
    second_entity = str(uuid4())
    pgdb.add(SearchProjectionOutbox(index_uid=WORKS_INDEX, entity_id=second_entity, action="upsert", version=1))
    await pgdb.execute(update(SearchDeliveryReceipt).where(SearchDeliveryReceipt.id == first_id).values(lease_until=None))
    await pgdb.commit()
    @asynccontextmanager
    async def opened_session():
        yield pgdb
    @asynccontextmanager
    async def grant(*_args, **_kwargs):
        yield _RealGrant(1, 20)
    monkeypatch.setattr(delivery, "async_session", opened_session)
    monkeypatch.setattr(delivery.settings, "search_backend", "postgresql")
    monkeypatch.setattr(heavy_io, "adaptive_resource_slice", grant)
    result = await delivery.run_delivery_slice(limit=500)
    await pgdb.rollback()
    assert result["claimed"] == result["processed"] == 1
    assert await pgdb.scalar(select(SearchDeliveryReceipt.state).where(SearchDeliveryReceipt.id == first_id)) == "complete"
    assert await pgdb.scalar(text("SELECT count(*) FROM search_pg_documents WHERE entity_id=:id"), {"id": second_entity}) == 0


@pytest.mark.asyncio
async def test_pg_run_entry_soft_grant_deadline_stops_next_receipt_but_finishes_current(pgdb, monkeypatch):
    import asyncio
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services.search_index_definitions import TAGS_INDEX

    await store._settings(TAGS_INDEX, "PATCH", INDEX_SETTINGS[TAGS_INDEX])
    first = SearchProjectionOutbox(
        index_uid=WORKS_INDEX, entity_id=str(uuid4()), action="delete", version=1,
        available_at=datetime.now(timezone.utc)-timedelta(seconds=2),
    )
    second = SearchProjectionOutbox(
        index_uid=TAGS_INDEX, entity_id=str(uuid4()), action="delete", version=1,
        available_at=datetime.now(timezone.utc)-timedelta(seconds=1),
    )
    pgdb.add_all([first, second])
    delivery = await _pg_run_environment(pgdb, monkeypatch, seconds=0.001)
    original_prepare = delivery._prepare
    prepared_ids = []

    async def slow_prepare(limit, deadline):
        prepared = await original_prepare(limit, deadline)
        assert prepared is not None
        prepared_ids.append(prepared.id)
        await asyncio.sleep(0.01)
        return prepared

    monkeypatch.setattr(delivery, "_prepare", slow_prepare)
    outcome = await delivery.run_delivery_slice(limit=500)
    assert outcome["claimed"] == outcome["processed"] == 1
    assert len(prepared_ids) == 1
    receipt = await pgdb.get(SearchDeliveryReceipt, prepared_ids[0])
    assert receipt.state == "complete" and receipt.lease_until is None
    assert await pgdb.scalar(select(SearchProjectionOutbox.completed_at).where(
        SearchProjectionOutbox.id == first.id
    )) is not None
    assert await pgdb.scalar(select(SearchProjectionOutbox.completed_at).where(
        SearchProjectionOutbox.id == second.id
    )) is None



@pytest.mark.asyncio
async def test_pg_run_entry_resource_denial_preserves_deferred_result(pgdb, monkeypatch):
    from contextlib import asynccontextmanager
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.services import heavy_io
    receipt_id, _outbox_id, _entity_id = await _seed_uuid_receipt(pgdb, token="resource-denied")
    delivery = await _pg_run_environment(pgdb, monkeypatch, receipt_id=receipt_id)

    @asynccontextmanager
    async def denied(*_args, **_kwargs):
        yield None

    monkeypatch.setattr(heavy_io, "adaptive_resource_slice", denied)
    outcome = await delivery.run_delivery_slice(limit=500)
    assert outcome["resource_deferred"] is True
    assert outcome["more_likely"] is False
    receipt = await pgdb.get(SearchDeliveryReceipt, receipt_id)
    assert receipt.state == "prepared" and receipt.lease_until is None



async def _pg_run_environment(pgdb, monkeypatch, *, receipt_id=None, units=500, seconds=20):
    from contextlib import asynccontextmanager
    from sqlalchemy import update
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    from app.models.search_rebuild import SearchRebuild
    from app.services import heavy_io, search_delivery, search_rebuild

    pgdb.add(SearchRebuild(
        backend="postgresql", owner=search_rebuild.MEMBERSHIP_BOOTSTRAP_OWNER,
        state="complete", phase="complete", progress={},
    ))
    if receipt_id is not None:
        await pgdb.execute(update(SearchDeliveryReceipt).where(
            SearchDeliveryReceipt.id == receipt_id
        ).values(lease_until=None, lease_token=None))
    await pgdb.commit()

    @asynccontextmanager
    async def opened_session():
        yield pgdb

    @asynccontextmanager
    async def granted(*_args, **_kwargs):
        yield _RealGrant(units, seconds)

    monkeypatch.setattr(search_delivery, "async_session", opened_session)
    monkeypatch.setattr(search_delivery.settings, "search_backend", "postgresql")
    monkeypatch.setattr(heavy_io, "adaptive_resource_slice", granted)
    return search_delivery
