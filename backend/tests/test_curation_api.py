import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4


def test_delete_work_moves_to_trash_instead_of_hard_delete(monkeypatch):
    from app.api import works

    work_id = uuid4()
    calls = {}

    class FakeDB:
        async def get(self, model, obj_id):
            calls["get"] = (model, obj_id)
            return SimpleNamespace(id=obj_id, title="sample")

        async def delete(self, _obj):
            calls["delete"] = True

    class FakeCurationService:
        def __init__(self, db):
            calls["service_db"] = db

        async def trash_works(self, ids, message=None):
            calls["trash"] = (ids, message)
            return SimpleNamespace(id=uuid4(), stats={"work_count": 1})

    monkeypatch.setattr(works, "CurationService", FakeCurationService)

    asyncio.run(works.delete_work(work_id, db=FakeDB()))

    assert calls["trash"][0] == [work_id]
    assert "delete" not in calls


def test_list_works_uses_canonical_search_query(monkeypatch):
    from app.api import works

    captured = {}

    class FakeSearchService:
        def __init__(self, db):
            captured["db"] = db

        async def search(self, query, offset, limit, **kwargs):
            captured.update(query=query, offset=offset, limit=limit, kwargs=kwargs)
            return {"groups": {"works": {"total": 0, "items": []}}}

    monkeypatch.setattr(works, "SearchService", FakeSearchService)

    result = asyncio.run(works.list_works(
        q="is:trashed",
        user=SimpleNamespace(nsfw_visible=True, is_admin=True, permissions=[]),
        db=object(),
    ))

    assert result == {"total": 0, "items": []}
    assert captured["query"] == "is:trashed"
    assert captured["kwargs"]["scope"] == "works"


def test_batch_curate_restore_uses_curation_service(monkeypatch):
    from app.api import works
    from app.schemas.curation import BatchCurateRequest

    work_id = uuid4()
    commit_id = uuid4()
    calls = {}

    class FakeCurationService:
        def __init__(self, _db):
            pass

        async def restore_works(self, ids, reason=None, message=None):
            calls["restore"] = (ids, reason, message)
            return SimpleNamespace(id=commit_id)

        async def commit_payload(self, obj_id):
            return {"id": str(obj_id), "changes": []}

    monkeypatch.setattr(works, "CurationService", FakeCurationService)

    result = asyncio.run(works.batch_curate_works(
        BatchCurateRequest(ids=[work_id], action="restore", reason="mistake", message="restore it"),
        db=object(),
    ))

    assert calls["restore"] == ([work_id], "mistake", "restore it")
    assert result == {"id": str(commit_id), "changes": []}


def test_creator_curation_endpoint_uses_curation_service(monkeypatch):
    from app.api import creators
    from app.schemas.curation import CreatorCurationRequest

    creator_id = uuid4()
    commit_id = uuid4()
    calls = {}

    class FakeCurationService:
        def __init__(self, _db):
            pass

        async def curate_creator(self, cid, action=None, reason=None, message=None):
            calls["curate"] = (cid, action, reason, message)
            return SimpleNamespace(id=commit_id)

        async def commit_payload(self, obj_id):
            return {"id": str(obj_id), "changes": []}

    monkeypatch.setattr(creators, "CurationService", FakeCurationService)
    monkeypatch.setattr(creators, "invalidate_api_caches", lambda *_domains: None)

    result = asyncio.run(creators.curate_creator(
        creator_id,
        CreatorCurationRequest(action="archive", reason="taste"),
        db=object(),
    ))

    assert calls["curate"] == (creator_id, "archive", "taste", None)
    assert result == {"id": str(commit_id), "changes": []}


def test_list_curation_commits_passes_include_baseline(monkeypatch):
    from app.api import curation

    calls = {}

    class FakeCurationService:
        def __init__(self, _db):
            pass

        async def list_commits(self, **kwargs):
            calls["kwargs"] = kwargs
            return {"items": [], "total": 0}

    monkeypatch.setattr(curation, "CurationService", FakeCurationService)

    result = asyncio.run(curation.list_curation_commits(include_baseline=False, db=object()))

    assert result == {"items": [], "total": 0}
    assert calls["kwargs"]["include_baseline"] is False


def test_backfill_status_and_run_use_curation_service(monkeypatch):
    from app.api import curation

    calls = []

    class FakeCurationService:
        def __init__(self, _db):
            pass

        async def backfill_status(self):
            calls.append("status")
            return {"is_complete": False, "expected": {}, "existing": {}, "missing": {}}

    monkeypatch.setattr(curation, "CurationService", FakeCurationService)

    # run_backfill is queued work now — the endpoint only enqueues.
    async def fake_enqueue(**kwargs):
        calls.append(("enqueue", kwargs["operation_type"]))
        return {"status": "enqueued", "job_id": "job-1"}

    monkeypatch.setattr("app.services.operations.enqueue_admin_operation", fake_enqueue)

    status = asyncio.run(curation.curation_backfill_status(db=object()))
    run = asyncio.run(curation.run_curation_backfill())

    assert status["is_complete"] is False
    assert run["status"] == "enqueued"
    assert calls == ["status", ("enqueue", "admin-curation-backfill")]


def test_revert_rejects_baseline_commit():
    from fastapi import HTTPException
    from app.services.curation import CurationService

    baseline = SimpleNamespace(id=uuid4(), status="baseline", extra_metadata={"baseline": True})

    class FakeDB:
        async def get(self, _model, _id):
            return baseline

    try:
        asyncio.run(CurationService(FakeDB()).revert_commit(baseline.id))
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "Baseline" in exc.detail
    else:
        raise AssertionError("baseline commit was revertible")


def test_curation_json_value_serializes_nested_datetime_and_uuid():
    from app.services.curation import _json_value

    obj_id = uuid4()
    payload = {
        "id": obj_id,
        "posted_at": datetime(2022, 8, 12, 15, 57, 49, tzinfo=timezone.utc),
        "days": {date(2022, 8, 12)},
    }

    serialized = _json_value(payload)

    assert serialized["id"] == str(obj_id)
    assert serialized["posted_at"] == "2022-08-12T15:57:49+00:00"
    assert serialized["days"] == ["2022-08-12"]
    json.dumps(serialized)


def test_add_change_serializes_jsonb_payloads_before_diffing():
    from app.services.curation import CurationService

    commit_id = uuid4()
    work_id = uuid4()
    added = {}

    class FakeDB:
        def add(self, obj):
            added["change"] = obj

        async def flush(self):
            pass

    change = asyncio.run(CurationService(FakeDB())._add_change(
        SimpleNamespace(id=commit_id),
        subject_type="work",
        subject_id=str(work_id),
        action="work_added",
        before_state=None,
        after_state={
            "id": work_id,
            "posted_at": datetime(2022, 8, 12, 15, 57, 49, tzinfo=timezone.utc),
        },
        impact={"seen_at": datetime(2022, 8, 12, tzinfo=timezone.utc)},
    ))

    assert change is added["change"]
    assert change.after_state["id"] == str(work_id)
    assert change.after_state["posted_at"] == "2022-08-12T15:57:49+00:00"
    assert change.impact["seen_at"] == "2022-08-12T00:00:00+00:00"
    json.dumps(change.after_state)
    json.dumps(change.diff)
    json.dumps(change.impact)


def test_repository_graph_endpoint_uses_curation_service(monkeypatch):
    from app.api import repositories

    source_id = uuid4()
    calls = {}

    async def fake_context(_db, sid):
        calls["context"] = sid
        return object(), object(), object()

    class FakeCurationService:
        def __init__(self, _db):
            pass

        async def repository_graph(self, sid, offset=0, limit=100, trigger=None, include_baseline=True):
            calls["graph"] = (sid, offset, limit, trigger, include_baseline)
            return {"repository_id": sid, "nodes": [], "edges": [], "total": 0, "offset": offset, "limit": limit}

    monkeypatch.setattr(repositories, "_get_source_context", fake_context)
    monkeypatch.setattr(repositories, "CurationService", FakeCurationService)

    result = asyncio.run(repositories.get_repository_curation_graph(
        source_id,
        offset=20,
        limit=40,
        trigger="source_synced",
        include_baseline=False,
        db=object(),
    ))

    assert calls["context"] == source_id
    assert calls["graph"] == (source_id, 20, 40, "source_synced", False)
    assert result["total"] == 0


def test_repository_graph_query_is_scoped_to_repository():
    from app.services.curation import CurationService

    repository_id = uuid4()
    seen_params = []

    class CountResult:
        def scalar(self):
            return 0

    class EmptyScalars:
        def all(self):
            return []

    class RowsResult:
        def scalars(self):
            return EmptyScalars()

    class FakeDB:
        async def execute(self, stmt):
            params = stmt.compile().params
            seen_params.append(params)
            if len(seen_params) == 1:
                return CountResult()
            return RowsResult()

    result = asyncio.run(CurationService(FakeDB()).repository_graph(
        repository_id,
        trigger="source_synced",
        include_baseline=False,
    ))

    assert result["nodes"] == []
    assert result["total"] == 0
    assert any(str(repository_id) in params.values() for params in seen_params)
    assert all("repository" in params.values() for params in seen_params)
    assert all("source_synced" in params.values() for params in seen_params)
    assert all("baseline" in params.values() for params in seen_params)


def test_clear_all_deletes_curation_tables_before_works(monkeypatch):
    from app.api import admin

    class Result:
        rowcount = 0

    class FakeDB:
        def __init__(self):
            self.tables = []

        async def execute(self, stmt):
            sql = str(stmt)
            self.tables.append(sql.removeprefix("DELETE FROM "))
            return Result()

        async def commit(self):
            pass

        async def flush(self):
            pass

    class FakeSearchService:
        def __init__(self, _db):
            pass

        async def delete_all_works(self):
            pass

    db = FakeDB()
    monkeypatch.setattr(admin, "_clear_files", lambda _paths: None)

    import app.services.search as search
    monkeypatch.setattr(search, "SearchService", FakeSearchService)

    result = asyncio.run(admin.clear_entity("all", db=db))

    assert result["status"] == "ok"
    assert db.tables.index("work_curation_states") < db.tables.index("works")
    assert db.tables.index("asset_storage_states") < db.tables.index("assets")
    assert db.tables.index("creator_curation_states") < db.tables.index("creators")
    assert db.tables.index("curation_changes") < db.tables.index("curation_commits")


def test_clear_works_includes_curation_state_tables():
    from app.api import admin

    tables = admin.ENTITIES["works"]

    assert "work_curation_states" in tables
    assert "asset_storage_states" in tables
    assert tables.index("work_curation_states") < tables.index("works")
    assert tables.index("asset_storage_states") < tables.index("assets")
