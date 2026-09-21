"""Restartable rebuild workflows share the durable remote delivery engine."""
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.database import async_session
from app.models.tag import Tag
from app.services.search import TAGS_INDEX
from app.services.search_projection_outbox import request_search_projection
from tests.test_search_delivery import delivery, ready_receipt  # noqa: F401


class Meili:
    def __init__(self):
        self.indexes = {TAGS_INDEX: {"orphan": {"id": "orphan"}}}
        self.tasks = {}
        self.writes = []
        self.pending = False

    def __call__(self, request):
        path = request.url.path
        if path.startswith("/tasks/"):
            uid = int(path.rsplit("/", 1)[-1])
            task = self.tasks[uid]
            return httpx.Response(200, json={"uid": uid, "status": "processing" if self.pending else "succeeded"})
        if request.method == "GET":
            uid = path.split("/")[2]
            if uid not in self.indexes:
                return httpx.Response(404, json={"code": "index_not_found"})
            if path.endswith("/stats"):
                return httpx.Response(200, json={"numberOfDocuments": len(self.indexes[uid])})
            return httpx.Response(200, json={"uid": uid, "primaryKey": "id"})
        payload = json.loads(request.content) if request.content else None
        self.writes.append((request.method, path, payload))
        if path == "/swap-indexes":
            for pair in payload:
                a, b = pair["indexes"]
                self.indexes[a], self.indexes[b] = self.indexes[b], self.indexes[a]
        elif path == "/indexes":
            self.indexes[payload["uid"]] = {}
        else:
            uid = path.split("/")[2]
            if request.method == "DELETE":
                self.indexes.pop(uid, None)
            elif path.endswith("/settings"):
                self.indexes.setdefault(uid, {})
            elif path.endswith("/delete-batch"):
                for identity in payload:
                    self.indexes[uid].pop(identity, None)
            else:
                self.indexes.setdefault(uid, {}).update({doc["id"]: doc for doc in payload})
        uid = len(self.tasks) + 1
        self.tasks[uid] = True
        return httpx.Response(202, json={"taskUid": uid})


@pytest.mark.asyncio
async def test_rebuild_resumes_from_persisted_keyset_and_replays_mutation(delivery):
    from app.services import search_rebuild
    ids = sorted([uuid4() for _ in range(5)])
    async with async_session() as db:
        db.add_all([Tag(id=identity, normalized_name=f"rebuild-{identity}") for identity in ids])
        await db.commit()
    remote = Meili()
    started = await search_rebuild.start_rebuild((TAGS_INDEX,), batch_size=2)
    assert started["status"] == "pending"
    build_id = started["build_id"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        mutated = False
        for _ in range(60):
            await delivery.run_delivery_slice(client=client)
            await ready_receipt()
            state = await search_rebuild.rebuild_status(build_id)
            if state["counts"].get(TAGS_INDEX, 0) >= 2 and not mutated:
                async with async_session() as db:
                    row = await db.get(Tag, ids[0])
                    row.normalized_name = f"changed-{ids[0]}"
                    await request_search_projection(db, tag_ids=[ids[0]])
                    await db.commit()
                mutated = True
            if state["status"] == "ok":
                break
        assert state["status"] == "ok"
        assert mutated
        assert "orphan" not in remote.indexes[TAGS_INDEX]
        assert remote.indexes[TAGS_INDEX][str(ids[0])]["normalized_name"] == f"changed-{ids[0]}"
        assert all(len(payload) <= 2 for method, path, payload in remote.writes if path.endswith("/documents"))
        assert len([path for _, path, _ in remote.writes if path == "/swap-indexes"]) == 1
    async with async_session() as db:
        await db.execute(text("DELETE FROM tags WHERE id = ANY(:ids)"), {"ids": ids})
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_registered_admin_rebuild_hands_off_pending_instead_of_failing(delivery, monkeypatch, ambiguous):
    from app.services import operations
    from app.models.task_run import TaskRun
    from app.jobs.admin_operations import _run_search_reindex_operation
    remote = Meili()
    run_slice = delivery.run_delivery_slice
    def transport(request):
        if ambiguous and request.method != "GET":
            raise httpx.ReadTimeout("submission outcome unknown")
        return remote(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        async def local_slice(**kwargs):
            return await run_slice(client=client)
        monkeypatch.setattr(delivery, "run_delivery_slice", local_slice)
        async with async_session() as db:
            prepared = await operations.prepare_admin_operation(db, operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active", title="Search rebuild", entity="search", options={},
                queue_name="maintenance", job_timeout=60)
            task_id = prepared.task.id
            await db.commit()
        try:
            with operations.admin_operation_attempt_context(str(task_id), 1):
                outcome = await _run_search_reindex_operation(str(task_id), {})
            assert outcome["_admin_handoff"] is True
            async with async_session() as db:
                task = await db.get(TaskRun, task_id)
                assert task.status == "enqueued"
                assert task.attempts == 2
                dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
                assert dispatch["options"]["_search_build_id"]
                assert dispatch["publication_state"] == "pending"
                if ambiguous:
                    from datetime import datetime
                    assert (datetime.fromisoformat(dispatch["next_retry_at"]) - task.updated_at).total_seconds() >= 14
        finally:
            async with async_session() as db:
                await db.execute(text("DELETE FROM task_runs WHERE id=:id"), {"id": task_id})
                await db.commit()


@pytest.mark.asyncio
async def test_replay_receipt_does_not_ack_live_versions_before_swap(delivery):
    from app.services import search_rebuild
    from app.models.search_rebuild import SearchRebuild
    from app.models.search_projection_outbox import SearchProjectionOutbox
    ids = [uuid4() for _ in range(25)]
    async with async_session() as db:
        await request_search_projection(db, deleted_tag_ids=ids)
        await db.commit()
    started = await search_rebuild.start_rebuild((TAGS_INDEX,), batch_size=500)
    async with async_session() as db:
        build = await db.get(SearchRebuild, UUID(started["build_id"]))
        build.phase = "replay"
        await db.commit()
    remote = Meili()
    remote.indexes[f"{TAGS_INDEX}__staging_{UUID(started['build_id']).hex[:12]}"] = {}
    from app.database import engine
    from sqlalchemy import event
    inserts = []
    def record(conn, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO search_rebuild_replay"):
            inserts.append(statement)
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            await delivery.run_delivery_slice(client=client)
            await ready_receipt()
            await delivery.run_delivery_slice(client=client)
        async with async_session() as db:
            events = list((await db.execute(select(SearchProjectionOutbox))).scalars())
            assert all(event.completed_at is None for event in events)
        assert len(inserts) == 1
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


@pytest.mark.asyncio
async def test_rebuild_requests_fresh_consistency_checkpoint(delivery):
    from app.services import search_rebuild
    from app.models.repository_sync_receipt import SearchIndexState
    started = await search_rebuild.start_rebuild((TAGS_INDEX,))
    assert started["counts"] == {TAGS_INDEX: 0}
    async with async_session() as db:
        state = (await db.execute(select(SearchIndexState).where(SearchIndexState.index_uid == TAGS_INDEX))).scalar_one_or_none()
        assert state is not None
        assert state.database_generation > state.indexed_generation


@pytest.mark.asyncio
async def test_swap_acknowledges_only_replayed_versions_after_crash(delivery, monkeypatch):
    from app.services import search_rebuild
    from app.models.search_rebuild import SearchRebuild, SearchRebuildReplay
    from app.models.search_projection_outbox import SearchProjectionOutbox
    identity = uuid4()
    async with async_session() as db:
        await request_search_projection(db, deleted_tag_ids=[identity])
        await db.commit()
        event = (await db.execute(select(SearchProjectionOutbox))).scalar_one()
        event_id = event.id
    started = await search_rebuild.start_rebuild((TAGS_INDEX,))
    build_id = UUID(started["build_id"])
    async with async_session() as db:
        build = await db.get(SearchRebuild, build_id)
        build.phase = "swap"
        db.add(SearchRebuildReplay(build_id=build_id, outbox_id=event_id, version=1))
        await db.commit()
    remote = Meili()
    remote.indexes[f"{TAGS_INDEX}__staging_{build_id.hex[:12]}"] = {}
    save = delivery._save
    async def crash(receipt, token, deadline, **values):
        if values.get("state") == "pending":
            raise RuntimeError("lost process after swap acceptance")
        return await save(receipt, token, deadline, **values)
    monkeypatch.setattr(delivery, "_save", crash)
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        with pytest.raises(RuntimeError, match="after swap acceptance"):
            await delivery.run_delivery_slice(client=client)
        async with async_session() as db:
            await request_search_projection(db, deleted_tag_ids=[identity])
            await db.commit()
        monkeypatch.setattr(delivery, "_save", save)
        await ready_receipt()
        await delivery.run_delivery_slice(client=client)
        await ready_receipt()
        await delivery.run_delivery_slice(client=client)
    async with async_session() as db:
        event = await db.get(SearchProjectionOutbox, event_id)
        assert event.version == 2
        assert event.completed_at is None
    assert len([path for _, path, _ in remote.writes if path == "/swap-indexes"]) == 1


@pytest.mark.asyncio
async def test_real_meili_rebuild_finishes_through_delayed_steps(delivery):
    import asyncio
    from app.services import search_rebuild
    from app.config import settings
    identity = uuid4()
    async with async_session() as db:
        db.add(Tag(id=identity, normalized_name=f"real-rebuild-{identity}"))
        await db.commit()
    started = await search_rebuild.start_rebuild((TAGS_INDEX,))
    async with httpx.AsyncClient() as client:
        for _ in range(100):
            await delivery.run_delivery_slice(client=client)
            status = await search_rebuild.rebuild_status(started["build_id"])
            if status["status"] != "pending":
                break
            await ready_receipt()
            await asyncio.sleep(.05)
        assert status["status"] == "ok", status
        response = await client.get(f"{settings.meili_url}/indexes/{TAGS_INDEX}/documents/{identity}",
                                    headers={"Authorization": f"Bearer {settings.meili_master_key}"})
        assert response.status_code == 200
        assert response.json()["normalized_name"] == f"real-rebuild-{identity}"
    async with async_session() as db:
        await db.execute(text("DELETE FROM tags WHERE id=:id"), {"id": identity})
        await db.commit()


@pytest.mark.asyncio
async def test_oversized_rebuild_document_fails_durably_without_remote_write(delivery, monkeypatch):
    from app.services import search_rebuild
    from app.services.search import SearchService
    from app.models.search_rebuild import SearchRebuild
    identity = uuid4()
    async with async_session() as db:
        db.add(Tag(id=identity, normalized_name=f"oversize-{identity}"))
        await db.commit()
    started = await search_rebuild.start_rebuild((TAGS_INDEX,))
    async with async_session() as db:
        build = await db.get(SearchRebuild, UUID(started["build_id"]))
        build.phase = "build"
        await db.commit()
    async def oversized(self, ids):
        return [{"id": str(ids[0]), "normalized_name": "x" * (4 * 1024 * 1024)}]
    monkeypatch.setattr(SearchService, "_build_tag_documents", oversized)
    remote = Meili()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            outcome = await delivery.run_delivery_slice(client=client)
        assert outcome["status"] == "error"
        state = await search_rebuild.rebuild_status(started["build_id"])
        assert state["status"] == "pending"
        assert state["phase"] == "discard_replay"
        assert "exceeds 4194304 bytes" in state["message"]
        assert remote.writes == []
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM tags WHERE id=:id"), {"id": identity})
            await db.commit()


@pytest.mark.asyncio
async def test_cancelled_owner_prevents_next_rebuild_write(delivery):
    from app.services import search_rebuild
    from app.models.task_run import TaskRun
    owner = uuid4()
    async with async_session() as db:
        db.add(TaskRun(id=owner, kind="admin", operation_type="admin-search-reindex", status="cancelled"))
        await db.commit()
    started = await search_rebuild.start_rebuild((TAGS_INDEX,), owner=str(owner))
    remote = Meili()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            outcome = await delivery.run_delivery_slice(client=client)
        assert outcome["status"] == "error"
        assert remote.writes == []
        state = await search_rebuild.rebuild_status(started["build_id"])
        assert state["status"] == "pending"
        assert state["phase"] == "discard_replay"
        assert "owner stopped" in state["message"]
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM task_runs WHERE id=:id"), {"id": owner})
            await db.commit()


@pytest.mark.asyncio
async def test_all_six_indexes_share_one_ordered_swap(delivery):
    from app.services import search_rebuild
    indexes = tuple(search_rebuild._specs())
    remote = Meili()
    started = await search_rebuild.start_rebuild(indexes)
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        for _ in range(160):
            await delivery.run_delivery_slice(client=client)
            await ready_receipt()
            status = await search_rebuild.rebuild_status(started["build_id"])
            if status["status"] != "pending":
                break
    assert status["status"] == "ok", status
    swaps = [payload for _, path, payload in remote.writes if path == "/swap-indexes"]
    assert len(swaps) == 1
    from app.services.search import WORKS_INDEX, CREATORS_INDEX, TAGS_INDEX, REPOSITORIES_INDEX, SUBSCRIPTIONS_INDEX, MEMBERSHIPS_INDEX
    assert indexes == (WORKS_INDEX, CREATORS_INDEX, TAGS_INDEX, REPOSITORIES_INDEX, SUBSCRIPTIONS_INDEX, MEMBERSHIPS_INDEX)
    assert swaps[0] == [
        {"indexes": [index, f"{index}__staging_{UUID(started['build_id']).hex[:12]}"]}
        for index in indexes
    ]
    assert set(remote.indexes) == set(indexes)


@pytest.mark.asyncio
async def test_rebuild_migration_roundtrip_preserves_active_work_guard(monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.database import engine
    migrations = []
    for name in ["f8d0e2a4b6c8_add_search_delivery_receipts", "f9e1a3b5c7d9_add_search_rebuild_continuations"]:
        path = Path(__file__).parents[1] / f"alembic/versions/{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        migrations.append(module)
    schema = "rebuild_migration_" + uuid4().hex
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        def verify(conn):
            for migration in migrations:
                monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
                migration.upgrade()
            conn.execute(text("INSERT INTO search_rebuilds(id,state,phase,progress) VALUES (:id,'running','build','{}')"), {"id": uuid4()})
            with pytest.raises(Exception, match="Drain or reconcile"):
                with conn.begin_nested():
                    migrations[1].downgrade()
            conn.execute(text("UPDATE search_rebuilds SET state='complete'"))
            migrations[1].downgrade()
            migrations[1].upgrade()
            assert conn.execute(text("SELECT count(*) FROM search_rebuilds")).scalar_one() == 0
        await connection.run_sync(verify)
        await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.mark.asyncio
async def test_legacy_owner_is_completed_when_durable_cleanup_finishes(delivery):
    from app.models.task_run import TaskRun
    from app.models.search_rebuild import SearchRebuild
    from app.services import search_rebuild
    owner = uuid4()
    async with async_session() as db:
        db.add(TaskRun(id=owner, kind="admin", operation_type="admin-search-reindex", status="running"))
        await db.commit()
    started = await search_rebuild.start_rebuild((TAGS_INDEX,), owner=str(owner))
    async with async_session() as db:
        build = await db.get(SearchRebuild, UUID(started["build_id"]))
        build.phase = "cleanup"
        await db.commit()
    remote = Meili()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            await delivery.run_delivery_slice(client=client)
            await ready_receipt()
            await delivery.run_delivery_slice(client=client)
        async with async_session() as db:
            assert (await db.get(TaskRun, owner)).status == "complete"
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM task_runs WHERE id=:id"), {"id": owner})
            await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["index_not_found", "internal"])
async def test_failed_old_index_cleanup_does_not_fail_completed_swap(delivery, code):
    from app.models.search_rebuild import SearchRebuild
    from app.services import search_rebuild
    started = await search_rebuild.start_rebuild((TAGS_INDEX,))
    async with async_session() as db:
        build = await db.get(SearchRebuild, UUID(started["build_id"]))
        build.phase = "cleanup"
        await db.commit()
    remote = Meili()
    def fail_cleanup(request):
        if request.url.path.startswith("/tasks/"):
            return httpx.Response(200, json={"status": "failed", "error": {"code": code}})
        return remote(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_cleanup)) as client:
        await delivery.run_delivery_slice(client=client)
        await ready_receipt()
        await delivery.run_delivery_slice(client=client)
    status = await search_rebuild.rebuild_status(started["build_id"])
    assert status["status"] == "ok"
    assert ("cleanup" in status["message"].lower()) == (code != "index_not_found")


def test_rebuild_prepared_receipt_has_execution_lease():
    from app.models.search_rebuild import SearchRebuild
    from app.services.search_rebuild import _receipt
    from app.services.search_delivery import now
    receipt = _receipt(SearchRebuild(id=uuid4(), phase="build"), TAGS_INDEX, "upsert", [], {})
    assert receipt.lease_token
    assert receipt.lease_until is not None
    assert 29 <= (receipt.lease_until - now()).total_seconds() <= 30


@pytest.mark.asyncio
async def test_ensure_live_restores_sql_budget_after_http(delivery, monkeypatch):
    from contextlib import asynccontextmanager
    from app.models.search_rebuild import SearchRebuild
    from app.services import search_rebuild
    import time
    started = await search_rebuild.start_rebuild((TAGS_INDEX,))
    async with async_session() as db:
        build = await db.get(SearchRebuild, UUID(started["build_id"]))
        build.phase = "ensure_live"
        await db.commit()
    original_session = delivery.session
    checked = []
    @asynccontextmanager
    async def checked_session(deadline):
        async with original_session(deadline) as db:
            original_get = db.get
            async def get(*args, **kwargs):
                budget = (await db.execute(text("SHOW statement_timeout"))).scalar_one()
                checked.append(budget)
                assert budget != "0"
                return await original_get(*args, **kwargs)
            db.get = get
            yield db
    monkeypatch.setattr(delivery, "session", checked_session)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Meili())) as client:
        await search_rebuild.prepare_next(500, time.monotonic() + 20, client)
    assert checked


@pytest.mark.asyncio
async def test_registered_admin_retries_writer_contention(delivery):
    from app.services import operations
    from app.models.task_run import TaskRun
    from app.jobs.admin_operations import _run_search_reindex_operation
    from app.services.heavy_io import LocalHeavyIOLock, _local_lock_path
    async with async_session() as db:
        prepared = await operations.prepare_admin_operation(db, operation_type="admin-search-reindex",
            scope_key="library:search-reindex:active", title="Search rebuild", entity="search", options={},
            queue_name="maintenance", job_timeout=60)
        task_id = prepared.task.id
        await db.commit()
    writer = LocalHeavyIOLock(_local_lock_path().with_name("search-writer.lock"))
    assert writer.try_acquire()
    try:
        with operations.admin_operation_attempt_context(str(task_id), 1):
            outcome = await _run_search_reindex_operation(str(task_id), {})
        assert outcome["_admin_handoff"] is True
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            assert task.status == "enqueued"
            assert task.attempts == 2
            assert task.meta[operations.ADMIN_DISPATCH_META_KEY]["publication_state"] == "pending"
    finally:
        writer.release()
        async with async_session() as db:
            await db.execute(text("DELETE FROM task_runs WHERE id=:id"), {"id": task_id})
            await db.commit()
