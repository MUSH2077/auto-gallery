"""Low-load regression tests for the bounded search projection algorithms."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable


async def _record_index_change(target: list, db, index_uid: str) -> None:
    target.append((db, index_uid))


def test_work_sorts_have_a_uuid_tie_breaker_in_sql_and_meili():
    from app.models import Work
    from app.services.search import _apply_sql_sort, _meili_sort
    from app.services.search_language import parse_search_query

    query = parse_search_query("sort:updated-desc", "works")
    sql = str(_apply_sql_sort(select(Work.id), query, Work).compile(
        dialect=postgresql.dialect()
    ))
    assert "works.updated_at DESC NULLS LAST" in sql
    assert "works.id DESC" in sql
    assert _meili_sort(query, "works") == ["updated_ts:desc", "id:desc"]


def test_default_visibility_is_a_partial_index_friendly_anti_exists():
    from app.models import Work
    from app.services.search import SearchService

    visible, trashed = SearchService._work_visibility_expressions()
    visible_sql = str(select(Work.id).where(visible).compile(
        dialect=postgresql.dialect()
    ))
    trashed_sql = str(select(Work.id).where(trashed).compile(
        dialect=postgresql.dialect()
    ))
    assert "NOT (EXISTS" in visible_sql
    assert "visibility !=" in visible_sql
    assert "EXISTS" in trashed_sql


def test_nullable_title_cursor_round_trips_and_compiles_seek_condition():
    from app.models import Work
    from app.services.search import (
        _decode_work_cursor,
        _encode_work_cursor,
        _work_seek_expression,
    )
    from app.services.search_language import parse_search_query

    query = parse_search_query("sort:title-asc", "works")
    work = Work(id=uuid4(), title=None)
    cursor = _encode_work_cursor(query, work, seek="after", force_sfw=True)
    seek, value, identity = _decode_work_cursor(cursor, query, force_sfw=True)
    assert (seek, value, identity) == ("after", None, work.id)
    expression = _work_seek_expression(
        query,
        seek=seek,
        value=value,
        identity=identity,
    )
    sql = str(expression.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))
    assert "works.title IS NULL" in sql
    assert "works.id >" in sql


def test_work_list_media_hydration_is_one_bounded_aggregate_query():
    from app.services.search import SearchService

    source = inspect.getsource(SearchService._search_works_db)
    # Page + source + combined media query. Exact count lives in the cached
    # helper, for four SQL round trips on a cold count and three when warm.
    assert source.count("self.db.execute") == 3
    assert "preview_position <= 10" in source
    assert "array_agg" in source
    assert "MATERIALIZED" in source


def test_full_rebuild_streams_work_and_reference_batches_by_keyset():
    from app.services.search import SearchService

    work_source = inspect.getsource(SearchService._stream_works_to_index)
    reference_source = inspect.getsource(SearchService._stream_reference_to_index)
    rebuild_source = inspect.getsource(SearchService._rebuild_selected_indexes)
    assert "model.id > last_id" in reference_source
    assert "Work.id > last_id" in work_source
    assert "documents[start:" not in rebuild_source
    assert "_stream_reference_to_index" in rebuild_source
    assert "_run_profiled_search_slice" in work_source
    assert "_run_profiled_search_slice" in reference_source
    assert 'workload="maintenance"' in rebuild_source


def test_full_reindex_coordinator_does_not_hold_one_global_operation_lock():
    from app.jobs.admin_operations import run_search_reindex_operation
    from app.services.resource_aware_worker import ResourceAwareWorker
    from app.services.search import SearchService

    entrypoint_source = inspect.getsource(run_search_reindex_operation)
    replay_source = inspect.getsource(
        SearchService._replay_work_events_to_staging
    )
    classifier_source = inspect.getsource(ResourceAwareWorker._internal_slice_workload)
    assert "run_heavy_io_operation" not in entrypoint_source
    assert "_run_profiled_search_slice" in replay_source
    assert "run_search_reindex_operation" in classifier_source


def test_projection_audit_checks_every_work_hash_in_bounded_batches():
    from app.services.search import SearchService

    source = inspect.getsource(SearchService.audit_projection)
    assert "range(0, len(work_ids), WORK_DOCUMENT_BATCH_SIZE)" in source
    assert "field_audit_count += len(batch_ids)" in source
    assert "field_drift_ids_truncated" in source
    assert "sorted(expected)[:100]" not in source


def test_projection_hash_is_stable_and_sensitive_to_fields():
    from app.services.search import _with_projection_hash

    first = _with_projection_hash({"id": "1", "title": "a"}, version=2)
    same = _with_projection_hash({"title": "a", "id": "1"}, version=2)
    changed = _with_projection_hash({"id": "1", "title": "b"}, version=2)
    assert first["projection_hash"] == same["projection_hash"]
    assert first["projection_hash"] != changed["projection_hash"]
    assert first["projection_version"] == 2


def test_meili_document_batches_obey_count_and_wire_size():
    from app.services.search import _document_batches

    documents = [{"id": str(index), "title": "x" * 40} for index in range(9)]
    batches = list(_document_batches(documents, max_documents=3, max_bytes=180))
    assert [len(batch) for batch in batches] == [2, 2, 2, 2, 1]
    assert [document["id"] for batch in batches for document in batch] == [
        str(index) for index in range(9)
    ]


def test_projection_drain_defers_events_beyond_one_wire_slice():
    from app.services.search import _select_projection_event_slice
    from app.services.search_projection_outbox import ProjectionEvent

    now = datetime.now(timezone.utc)
    events = [
        ProjectionEvent(
            id=uuid4(),
            index_uid="works",
            entity_id=str(index),
            action="upsert",
            version=1,
            attempts=0,
            updated_at=now,
        )
        for index in range(4)
    ]
    documents = {
        "works": [
            {"id": str(index), "description": "x" * 120}
            for index in range(4)
        ]
    }
    selected, deferred = _select_projection_event_slice(
        events,
        documents,
        max_bytes=500,
    )
    assert 0 < len(selected) < len(events)
    assert selected + deferred == events


def test_deferred_projection_release_has_no_retry_penalty():
    from app.services.search_projection_outbox import release_projection_events

    source = inspect.getsource(release_projection_events)
    assert "lease_until=None" in source
    assert "updated_at=SearchProjectionOutbox.updated_at" in source
    assert "attempts=" not in source


def test_meili_client_socket_timeout_and_settings_hash(monkeypatch):
    import app.services.search as search

    constructed = []

    class Constructor:
        def __init__(self, url, key, *, timeout):
            constructed.append((url, key, timeout))

    monkeypatch.setattr(search, "MeiliClient", Constructor)
    search._client(timeout_seconds=2.2)
    assert constructed[-1][2] == 3

    updates = []

    class Index:
        primary_key = "id"

        def update_settings(self, value):
            updates.append(value)
            return object()

    class Client:
        @staticmethod
        def get_index(_uid):
            return Index()

        @staticmethod
        def index(_uid):
            return Index()

    monkeypatch.setattr(search, "cache_get", lambda _key: None)
    monkeypatch.setattr(search, "cache_set", lambda *_args: None)
    monkeypatch.setattr(search, "_wait_for_task", lambda _client, task, **_kwargs: task)
    search._ensured_settings_hashes.clear()
    selected = {search.WORKS_INDEX: search.INDEX_SETTINGS[search.WORKS_INDEX]}
    search._ensure_indexes(Client(), selected)
    search._ensure_indexes(Client(), selected)
    assert len(updates) == 1


def test_meili_search_is_bounded_batched_and_circuit_broken():
    import app.services.search as search

    source = inspect.getsource(search.SearchService._search_meili)
    assert "_meili_search_semaphore" in source
    assert "multi_search" in source
    assert "SearchParams" in source
    assert search.MEILI_SEARCH_CONCURRENCY == 4

    search._meili_breaker_success()
    try:
        for _ in range(search.MEILI_BREAKER_FAILURE_THRESHOLD):
            search._meili_breaker_failure()
        assert search.meili_search_circuit_status()["status"] == "open"
        with pytest.raises(search.SearchBackendUnavailable, match="circuit breaker"):
            search._meili_breaker_before_request()
    finally:
        search._meili_breaker_success()


@pytest.mark.asyncio
async def test_unbounded_work_document_build_is_forbidden():
    from app.services.search import SearchService

    with pytest.raises(ValueError, match="keyset streaming rebuild"):
        await SearchService(object())._build_work_documents(None)


@pytest.mark.asyncio
async def test_count_cache_is_singleflight_within_a_process(monkeypatch):
    import app.services.search as search_module
    from app.models import Work
    from app.services.search import SearchService
    from app.services.search_language import parse_search_query

    values: dict[str, int] = {}
    written_ttls: list[int] = []
    calls = 0

    class Result:
        @staticmethod
        def scalar():
            return 42

    class Database:
        async def execute(self, _statement):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return Result()

    monkeypatch.setattr(search_module, "cache_generation", lambda _domain: 1)
    monkeypatch.setattr(search_module, "cache_get", values.get)
    monkeypatch.setattr(
        search_module,
        "cache_get_with_status",
        lambda key: (True, values.get(key)),
    )
    monkeypatch.setattr(
        search_module,
        "cache_set",
        lambda key, value, ttl: (
            values.__setitem__(key, value),
            written_ttls.append(ttl),
        )[0],
    )
    monkeypatch.setattr(search_module, "cache_try_lock", lambda *_args: "lease")
    monkeypatch.setattr(search_module, "cache_release_lock", lambda *_args: True)
    search_module._count_locks.clear()

    service = SearchService(Database())
    query = parse_search_query("sort:created-desc", "works")
    base = select(Work)
    totals = await asyncio.gather(*(
        service._cached_work_total(base, query, force_sfw=False)
        for _ in range(5)
    ))
    assert totals == [42] * 5
    assert calls == 1
    assert written_ttls == [search_module.INTERACTIVE_LIST_TTL]


@pytest.mark.asyncio
async def test_count_loser_waits_for_the_same_generation(monkeypatch):
    import app.services.search as search_module
    from app.models import Work
    from app.services.cache import cache_key
    from app.services.search import SearchService
    from app.services.search_language import parse_search_query

    query = parse_search_query("sort:created-desc", "works")
    total_key = cache_key(
        "works:count",
        query=query.canonical,
        force_sfw=False,
        generation=9,
    )
    stale_key = cache_key(
        "works:count-stale",
        query=query.canonical,
        force_sfw=False,
    )
    values = {stale_key: 12}
    reads = 0

    class Database:
        async def execute(self, _statement):
            raise AssertionError("a contended count must not hit PostgreSQL")

    monkeypatch.setattr(search_module, "cache_generation", lambda _domain: 9)
    monkeypatch.setattr(search_module, "cache_get", values.get)
    def cache_read(key):
        nonlocal reads
        if key == total_key:
            reads += 1
            if reads >= 3:
                return True, 73
        return True, values.get(key)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(search_module, "cache_get_with_status", cache_read)
    monkeypatch.setattr(search_module, "cache_try_lock", lambda *_args: None)
    monkeypatch.setattr(search_module.asyncio, "sleep", no_wait)
    search_module._count_locks.clear()

    total = await SearchService(Database())._cached_work_total(
        select(Work),
        query,
        force_sfw=False,
    )
    assert total == 73
    assert values[stale_key] == 12


@pytest.mark.asyncio
async def test_transactional_projection_request_uses_callers_session(monkeypatch):
    import app.services.search_projection_outbox as outbox

    calls = []
    generation_bumps = []
    index_changes = []

    async def fake_enqueue(db, index_uid, identities, *, action):
        calls.append((db, index_uid, identities, action))
        return len(identities)

    monkeypatch.setattr(outbox, "_enqueue_projection_action", fake_enqueue)
    monkeypatch.setattr(
        outbox,
        "_mark_index_changed",
        lambda db, index_uid: _record_index_change(index_changes, db, index_uid),
    )
    monkeypatch.setattr(
        outbox,
        "cache_bump_generation",
        lambda domain: generation_bumps.append(domain) or 1,
    )
    class Database:
        def __init__(self):
            self.info = {}

        @staticmethod
        def in_nested_transaction():
            return False

    db = Database()
    work_id = uuid4()
    requested = await outbox.request_search_projection(
        db,
        [work_id, work_id],
        deleted_work_ids=[work_id],
    )
    assert requested == 2
    assert [call[0] for call in calls] == [db, db]
    assert [call[3] for call in calls] == ["upsert", "delete"]
    assert index_changes == [(db, outbox.DEFAULT_WORKS_INDEX_UID)]
    assert generation_bumps == []
    assert db.info[outbox._WORKS_GENERATION_PENDING] is True
    outbox._arm_works_generation_after_outer_commit(db)
    outbox._bump_works_generation_after_commit(db)
    assert generation_bumps == ["works"]


@pytest.mark.asyncio
async def test_transactional_projection_request_covers_all_five_indexes(monkeypatch):
    import app.services.search_projection_outbox as outbox

    calls = []
    index_changes = []

    async def fake_enqueue(db, index_uid, identities, *, action):
        calls.append((db, index_uid, identities, action))
        return len(identities)

    monkeypatch.setattr(outbox, "_enqueue_projection_action", fake_enqueue)
    monkeypatch.setattr(
        outbox,
        "_mark_index_changed",
        lambda db, index_uid: _record_index_change(index_changes, db, index_uid),
    )
    monkeypatch.setattr(outbox, "cache_bump_generation", lambda _domain: 1)
    db = SimpleNamespace(info={})
    identities = [uuid4() for _ in range(5)]
    await outbox.request_search_projection(
        db,
        [identities[0]],
        creator_ids=[identities[1]],
        tag_ids=[identities[2]],
        repository_ids=[identities[3]],
        deleted_subscription_ids=[identities[4]],
    )

    assert [call[1] for call in calls] == [
        outbox.DEFAULT_WORKS_INDEX_UID,
        outbox.DEFAULT_CREATORS_INDEX_UID,
        outbox.DEFAULT_TAGS_INDEX_UID,
        outbox.DEFAULT_REPOSITORIES_INDEX_UID,
        outbox.DEFAULT_SUBSCRIPTIONS_INDEX_UID,
    ]
    assert [call[3] for call in calls] == [
        "upsert",
        "upsert",
        "upsert",
        "upsert",
        "delete",
    ]
    assert {index_uid for _db, index_uid in index_changes} == {
        outbox.DEFAULT_WORKS_INDEX_UID,
        outbox.DEFAULT_CREATORS_INDEX_UID,
        outbox.DEFAULT_TAGS_INDEX_UID,
        outbox.DEFAULT_REPOSITORIES_INDEX_UID,
        outbox.DEFAULT_SUBSCRIPTIONS_INDEX_UID,
    }


def test_exact_timestamp_filter_is_equality_not_less_than():
    from app.models import Work
    from app.services.search import _sql_date_expression

    expression = _sql_date_expression(
        Work.posted_at,
        "2026-08-09T12:34:56+00:00",
    )
    sql = str(
        expression.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "works.posted_at =" in sql
    assert "<=" not in sql


def test_reference_crud_never_triggers_a_full_index_rebuild():
    import app.api.creators as creators
    import app.api.subscriptions as subscriptions

    assert "refresh_reference_indexes" not in inspect.getsource(creators)
    assert "refresh_reference_indexes" not in inspect.getsource(subscriptions)


def test_compatibility_projection_mutations_never_wait_for_meili():
    from app.services.search import SearchService

    for method_name in (
        "index_works",
        "delete_work",
        "delete_creator",
        "delete_tag",
        "delete_repository",
        "delete_subscription",
    ):
        source = inspect.getsource(getattr(SearchService, method_name))
        assert ".drain_search_projection_outbox" not in source
        assert "_direct_index_work_ids" not in source
        assert "_delete_document" not in source


def test_search_outbox_model_is_registered_with_bounded_queue_indexes():
    from app.models import Base, SearchProjectionOutbox

    assert Base.metadata.tables["search_projection_outbox"] is SearchProjectionOutbox.__table__
    table_sql = str(CreateTable(SearchProjectionOutbox.__table__).compile(
        dialect=postgresql.dialect()
    ))
    assert "UNIQUE (index_uid, entity_id)" in table_sql
    assert "action IN ('upsert', 'delete')" in table_sql
    index_sql = "\n".join(
        str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        for index in SearchProjectionOutbox.__table__.indexes
    )
    assert "WHERE completed_at IS NULL" in index_sql


def test_cache_pattern_invalidation_uses_scan_and_generation(monkeypatch):
    import app.services.cache as cache

    class Redis:
        def __init__(self):
            self.generations = []
            self.deleted = []

        def incr(self, key):
            self.generations.append(key)
            return 1

        @staticmethod
        def scan_iter(*, match, count):
            assert match == "cache:api:works:*"
            assert count == 100
            return iter((b"cache:api:works:a", b"cache:api:works:b"))

        def delete(self, *keys):
            self.deleted.extend(keys)
            return len(keys)

    redis = Redis()
    monkeypatch.setattr(cache, "_client", lambda: redis)
    assert cache.cache_delete_pattern("works:*") == 2
    assert redis.generations == ["cache:generation:works"]
    assert len(redis.deleted) == 2


def _ready_search_gate(*, generation: int = 4) -> dict:
    return {
        "consistent": True,
        "status": "ready",
        "generation_match": True,
        "count_match": True,
        "outbox_pending": False,
        "generation_lag": 0,
        "database_generation": generation,
        "indexed_generation": generation,
        "database_document_count": 12,
        "index_document_count": 12,
        "last_verified_at": "2026-08-12T00:00:00+00:00",
        "pressure_mode": "normal",
    }


@pytest.mark.asyncio
async def test_cursor_pages_never_consult_or_race_the_index(monkeypatch):
    import app.services.search as search_module
    from app.services.search import SearchService
    from app.services.search_language import parse_search_query

    service = SearchService(object())

    async def database(*_args, **_kwargs):
        return {"items": ["database"], "total": 1}

    async def forbidden_gate(_index):
        raise AssertionError("cursor pages must not consult the search index gate")

    monkeypatch.setattr(service, "_search_works_db", database)
    monkeypatch.setattr(search_module, "search_index_consistency", forbidden_gate)
    result, execution = await service._hedged_structured_works_search(
        parse_search_query("sort:created-desc", "works"),
        {},
        0,
        20,
        force_sfw=False,
        cursor="opaque-cursor",
    )
    assert result["items"] == ["database"]
    assert execution["winner"] == "postgresql"
    assert execution["hedged"] is False


@pytest.mark.asyncio
async def test_equivalent_first_page_can_choose_validated_meili_and_cancel_database(
    monkeypatch,
):
    import app.services.search as search_module
    from app.services.search import SearchService
    from app.services.search_language import parse_search_query

    service = SearchService(object())
    database_cancelled = asyncio.Event()

    async def database(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            database_cancelled.set()
            raise

    async def meili(*_args, **_kwargs):
        return {"works": {"items": ["meili"], "total": 1}}

    async def gate(_index):
        return _ready_search_gate()

    monkeypatch.setattr(service, "_search_works_db", database)
    monkeypatch.setattr(service, "_search_meili", meili)
    monkeypatch.setattr(search_module, "search_index_consistency", gate)
    result, execution = await service._hedged_structured_works_search(
        parse_search_query("sort:created-desc", "works"),
        {},
        0,
        20,
        force_sfw=False,
        cursor=None,
    )
    assert result["items"] == ["meili"]
    assert execution["winner"] == "meilisearch"
    assert execution["hedged"] is True
    assert database_cancelled.is_set()


@pytest.mark.asyncio
async def test_index_change_during_race_rejects_meili_result(monkeypatch):
    import app.services.search as search_module
    from app.services.search import SearchService
    from app.services.search_language import parse_search_query

    service = SearchService(object())
    gate_calls = 0

    async def database(*_args, **_kwargs):
        await asyncio.sleep(0.1)
        return {"items": ["database"], "total": 1}

    async def meili(*_args, **_kwargs):
        return {"works": {"items": ["meili"], "total": 1}}

    async def gate(_index):
        nonlocal gate_calls
        gate_calls += 1
        return _ready_search_gate(generation=4 if gate_calls == 1 else 5)

    monkeypatch.setattr(service, "_search_works_db", database)
    monkeypatch.setattr(service, "_search_meili", meili)
    monkeypatch.setattr(search_module, "search_index_consistency", gate)
    result, execution = await service._hedged_structured_works_search(
        parse_search_query("sort:created-desc", "works"),
        {},
        0,
        20,
        force_sfw=False,
        cursor=None,
    )
    assert result["items"] == ["database"]
    assert execution["winner"] == "postgresql"
    assert execution["index_status"] == "changed_during_race"
    assert gate_calls == 2
