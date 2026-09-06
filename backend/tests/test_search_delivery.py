"""Durable search delivery against isolated PostgreSQL and controlled HTTP."""
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.database import async_session
from app.models import Base
from app.models.search_projection_outbox import SearchProjectionOutbox
from app.services.search_projection_outbox import request_search_projection, DEFAULT_WORKS_INDEX_UID


@pytest.fixture
async def delivery(monkeypatch, tmp_path):
    from app.services import search_delivery as delivery
    from app.services import heavy_io
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    @asynccontextmanager
    async def capacity(*args, **kwargs):
        yield SimpleNamespace(work_units=kwargs["max_work_units"])
    monkeypatch.setattr(heavy_io, "adaptive_resource_slice", capacity)
    monkeypatch.setattr(heavy_io, "_local_lock_path", lambda: tmp_path / "heavy-io.lock")
    async with async_session() as db:
        await db.execute(text("ALTER TABLE search_delivery_receipts ADD COLUMN IF NOT EXISTS phase varchar(24) DEFAULT 'settings'"))
        await db.execute(text("TRUNCATE search_delivery_receipts, search_projection_outbox, search_index_states"))
        await db.commit()
    yield delivery
    async with async_session() as db:
        await db.execute(text("TRUNCATE search_delivery_receipts, search_projection_outbox, search_index_states"))
        await db.commit()


class Remote:
    def __init__(self):
        self.status = "processing"
        self.writes = []
        self.polls = 0
        self.fail_submit = False

    def __call__(self, request):
        if request.method == "GET" and "/tasks/" in request.url.path:
            self.polls += 1
            return httpx.Response(200, json={"uid": 41, "status": self.status, "error": {"message": "disk full"} if self.status == "failed" else None})
        if request.method == "GET" and request.url.path.endswith("/settings"):
            from app.services.search import INDEX_SETTINGS
            return httpx.Response(200, json=INDEX_SETTINGS[DEFAULT_WORKS_INDEX_UID])
        if request.method == "GET":
            return httpx.Response(200, json={"uid": DEFAULT_WORKS_INDEX_UID, "primaryKey": "id"})
        self.writes.append((request.url.path, json.loads(request.content)))
        if self.fail_submit:
            raise httpx.ReadTimeout("accepted, response lost")
        return httpx.Response(202, json={"taskUid": 41})


async def enqueue_delete(identity=None):
    identity = identity or uuid4()
    async with async_session() as db:
        await request_search_projection(db, deleted_work_ids=[identity])
        await db.commit()
    return identity


async def ready_receipt():
    async with async_session() as db:
        await db.execute(text("UPDATE search_delivery_receipts SET available_at=now()-interval '1 second', lease_until=NULL"))
        await db.commit()


@pytest.mark.asyncio
async def test_pending_returns_without_ack_and_newer_mutation_survives(delivery):
    identity = await enqueue_delete()
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        result = await delivery.run_delivery_slice(client=client)
        assert result["status"] == "pending"
        assert len(remote.writes) == 1
        assert remote.polls == 0
        await enqueue_delete(identity)
        await ready_receipt()
        result = await delivery.run_delivery_slice(client=client)
        assert result["status"] == "pending"
        assert remote.polls == 1
        assert len(remote.writes) == 1
        await ready_receipt()
        remote.status = "succeeded"
        result = await delivery.run_delivery_slice(client=client)
        assert result["status"] == "ok"
    async with async_session() as db:
        row = (await db.execute(select(SearchProjectionOutbox))).scalar_one()
        assert row.version == 2
        assert row.completed_at is None
        assert row.attempts == 0


@pytest.mark.asyncio
async def test_http_ambiguity_never_releases_or_replays_unproven_write(delivery):
    await enqueue_delete()
    remote = Remote()
    remote.fail_submit = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        result = await delivery.run_delivery_slice(client=client)
        assert result["status"] == "ambiguous"
        await ready_receipt()
        result = await delivery.run_delivery_slice(client=client)
        assert result["status"] == "ambiguous"
        assert len(remote.writes) == 1
    async with async_session() as db:
        row = (await db.execute(select(SearchProjectionOutbox))).scalar_one()
        assert row.completed_at is None
        assert row.attempts == 0
    assert delivery.remote_flight.read_marker() is not None


@pytest.mark.asyncio
async def test_remote_failure_retries_exact_versions_and_releases_marker(delivery):
    await enqueue_delete()
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        await delivery.run_delivery_slice(client=client)
        await ready_receipt()
        remote.status = "failed"
        result = await delivery.run_delivery_slice(client=client)
        assert result["status"] == "error"
    async with async_session() as db:
        row = (await db.execute(select(SearchProjectionOutbox))).scalar_one()
        assert row.completed_at is None
        assert row.attempts == 1
        assert row.available_at > datetime.now(timezone.utc)
    assert delivery.remote_flight.read_marker() is None


@pytest.mark.asyncio
async def test_remote_fence_survives_redis_loss_but_admits_small_ingest(delivery):
    from app.services.heavy_io import RenewableRedisLease, local_lock_for_workload, RESOURCE_DISK_TOKEN_KEY, RESOURCE_BACKGROUND_TOKEN_KEY
    from app.services.redis_client import get_redis
    redis = get_redis()
    redis.flushdb()
    delivery.remote_flight.create_marker(str(uuid4()))
    maintenance = local_lock_for_workload("maintenance")
    background = local_lock_for_workload("image_derive")
    ingest_lock = local_lock_for_workload("import_db")
    try:
        assert not maintenance.try_acquire()
        assert not background.try_acquire()
        assert ingest_lock.try_acquire()
        ingest = RenewableRedisLease([RESOURCE_DISK_TOKEN_KEY], workload="import_db", owner="small-import", redis_client=redis,
                                    reservation_key=RESOURCE_DISK_TOKEN_KEY, reservation_bytes=64 * 1024 * 1024, reservation_capacity_bytes=300 * 1024 * 1024)
        assert await ingest.try_acquire()
        assert redis.ttl(delivery.remote_flight.REMOTE_RESERVATION_KEY) == -1
        await ingest.release()
        redis.flushdb()
        too_large = RenewableRedisLease([RESOURCE_DISK_TOKEN_KEY], workload="import_db", owner="large-import", redis_client=redis,
                                    reservation_key=RESOURCE_DISK_TOKEN_KEY, reservation_bytes=128 * 1024 * 1024, reservation_capacity_bytes=300 * 1024 * 1024)
        assert not await too_large.try_acquire()
        assert too_large.denial_reason == "resource_reservation_capacity"
    finally:
        maintenance.release()
        background.release()
        ingest_lock.release()
        redis.flushdb()


@pytest.mark.asyncio
async def test_two_normal_workers_submit_only_once(delivery):
    import asyncio
    await enqueue_delete()
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        results = await asyncio.gather(delivery.run_delivery_slice(client=client), delivery.run_delivery_slice(client=client))
    assert len(remote.writes) == 1
    assert any(item["status"] == "pending" for item in results)


@pytest.mark.asyncio
async def test_first_index_settings_are_durable_separate_task(delivery):
    await enqueue_delete()
    remote = Remote()
    def missing_settings(request):
        if request.method == "GET" and request.url.path.endswith("/settings"):
            return httpx.Response(404, json={"code": "index_not_found"})
        return remote(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(missing_settings)) as client:
        first = await delivery.run_delivery_slice(client=client)
        assert first["status"] == "pending"
        assert remote.writes[0][0].endswith("/settings")
        await ready_receipt()
        remote.status = "succeeded"
        await delivery.run_delivery_slice(client=client)
        async with async_session() as db:
            event = (await db.execute(select(SearchProjectionOutbox))).scalar_one()
            assert event.completed_at is None
        await ready_receipt()
        await delivery.run_delivery_slice(client=client)
        assert len(remote.writes) == 2
        assert remote.writes[1][0].endswith("/documents/delete-batch")


@pytest.mark.asyncio
async def test_task_identity_survives_crash_before_sql_response_commit(delivery, monkeypatch):
    await enqueue_delete()
    remote = Remote()
    save = delivery._save
    async def crash(receipt, token, deadline, **values):
        if values.get("state") == "pending":
            raise RuntimeError("crash after HTTP response")
        return await save(receipt, token, deadline, **values)
    monkeypatch.setattr(delivery, "_save", crash)
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        with pytest.raises(RuntimeError, match="crash after HTTP"):
            await delivery.run_delivery_slice(client=client)
        monkeypatch.setattr(delivery, "_save", save)
        await ready_receipt()
        remote.status = "succeeded"
        recovered = await delivery.run_delivery_slice(client=client)
        assert recovered["status"] in ("pending", "ok")
        if recovered["status"] == "pending":
            await ready_receipt()
            assert (await delivery.run_delivery_slice(client=client))["status"] == "ok"
        assert len(remote.writes) == 1
    async with async_session() as db:
        row = (await db.execute(select(SearchProjectionOutbox))).scalar_one()
        assert row.completed_at is not None


@pytest.mark.asyncio
async def test_one_index_action_and_bounded_hydration_before_wire_cap(delivery, monkeypatch):
    from app.services.search import SearchService
    ids = [uuid4() for _ in range(501)]
    hydrated = []
    async def documents(self, identities):
        hydrated.append(len(identities))
        return [{"id": str(identity), "title": "x" * 900_000} for identity in identities]
    monkeypatch.setattr(SearchService, "_build_work_documents", documents)
    async with async_session() as db:
        await request_search_projection(db, work_ids=ids, deleted_tag_ids=[uuid4()])
        await db.commit()
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        await delivery.run_delivery_slice(limit=5000, client=client)
    assert hydrated == [500]
    assert len(remote.writes) == 1
    assert len(remote.writes[0][1]) == 4
    assert len(json.dumps(remote.writes[0][1]).encode()) <= 4 * 1024 * 1024


@pytest.mark.asyncio
async def test_poll_network_error_keeps_remote_reservation_without_attempt_failure(delivery):
    await enqueue_delete()
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        await delivery.run_delivery_slice(client=client)
    await ready_receipt()
    def unavailable(request):
        raise httpx.ConnectError("temporarily unreachable")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        assert (await delivery.run_delivery_slice(client=client))["status"] == "pending"
    assert delivery.remote_flight.read_marker() is not None
    async with async_session() as db:
        row = (await db.execute(select(SearchProjectionOutbox))).scalar_one()
        assert row.attempts == 0
        assert row.completed_at is None


@pytest.mark.asyncio
async def test_ready_probe_includes_crashed_receipt_without_counting_backlog(delivery):
    from app.services.outbox_coordinator import outbox_counts, outbox_health
    await enqueue_delete()
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        await delivery.run_delivery_slice(client=client)
    await ready_receipt()
    async with async_session() as db:
        await db.execute(text("DELETE FROM search_projection_outbox"))
        await db.commit()
        assert (await outbox_counts(db, ready_only=True))["search"] == 1
        health = await outbox_health(db)
        assert health["search"]["remote_delivery"]["task_uid"] == 41


@pytest.mark.asyncio
async def test_real_meili_tag_upsert_and_delete(delivery):
    import asyncio
    from app.models.tag import Tag
    from app.config import settings
    tag_id = uuid4()
    async with async_session() as db:
        db.add(Tag(id=tag_id, normalized_name=f"delivery-{tag_id}"))
        await request_search_projection(db, tag_ids=[tag_id])
        await db.commit()
    async with httpx.AsyncClient() as client:
        for _ in range(60):
            outcome = await delivery.run_delivery_slice(client=client)
            if outcome["status"] == "ok":
                break
            assert outcome["status"] in ("pending", "idle")
            await asyncio.sleep(.05)
            await ready_receipt()
        assert outcome["status"] == "ok"
        uid = settings.meili_index_prefix + "tags"
        response = await client.get(f"{settings.meili_url}/indexes/{uid}/documents/{tag_id}", headers={"Authorization": f"Bearer {settings.meili_master_key}"})
        assert response.status_code == 200
        assert response.json()["normalized_name"] == f"delivery-{tag_id}"
        async with async_session() as db:
            await request_search_projection(db, deleted_tag_ids=[tag_id])
            await db.commit()
        for _ in range(60):
            outcome = await delivery.run_delivery_slice(client=client)
            if outcome["status"] == "ok":
                break
            await asyncio.sleep(.05)
            await ready_receipt()
        assert outcome["status"] == "ok"
        response = await client.get(f"{settings.meili_url}/indexes/{uid}/documents/{tag_id}", headers={"Authorization": f"Bearer {settings.meili_master_key}"})
        assert response.status_code == 404
    async with async_session() as db:
        await db.execute(text("DELETE FROM tags WHERE id=:id"), {"id": tag_id})
        await db.commit()


@pytest.mark.asyncio
async def test_ambiguous_receipt_can_attach_deliberately_verified_task(delivery):
    identity = await enqueue_delete()
    remote = Remote()
    remote.fail_submit = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        await delivery.run_delivery_slice(client=client)
    marker = delivery.remote_flight.read_marker()
    def verified(request):
        return httpx.Response(200, json={"uid": 41, "indexUid": DEFAULT_WORKS_INDEX_UID,
                                       "type": "documentDeletion", "status": "succeeded"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(verified)) as client:
        await delivery.reconcile_task(UUID(marker["owner"]), 41, client=client)
        await ready_receipt()
        assert (await delivery.run_delivery_slice(client=client))["status"] == "ok"


@pytest.mark.asyncio
async def test_checkpoint_exact_counts_wait_until_caught_up(delivery):
    from app.models.repository_sync_receipt import SearchIndexState
    await enqueue_delete()
    remote = Remote()
    stats_calls = []
    def with_stats(request):
        if request.url.path.endswith("/stats"):
            stats_calls.append(request.url.path)
            return httpx.Response(200, json={"numberOfDocuments": 0})
        return remote(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(with_stats)) as client:
        await delivery.run_delivery_slice(client=client)
        assert stats_calls == []
        await ready_receipt()
        remote.status = "succeeded"
        await delivery.run_delivery_slice(client=client)
        assert stats_calls == []
        await delivery.run_delivery_slice(client=client)
    assert len(stats_calls) == 1
    async with async_session() as db:
        state = (await db.execute(select(SearchIndexState))).scalar_one()
        assert state.indexed_generation == state.database_generation
        assert state.last_verified_at is not None


@pytest.mark.asyncio
async def test_rebuild_rejects_active_ordinary_receipt(delivery):
    from app.services.search import SearchService, WORKS_INDEX
    await enqueue_delete()
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        await delivery.run_delivery_slice(client=client)
    async with async_session() as db:
        with pytest.raises(RuntimeError, match="remote search"):
            import asyncio
            await asyncio.wait_for(SearchService(db)._rebuild_selected_indexes((WORKS_INDEX,)), timeout=1)


def test_poll_coordinator_does_not_wait_on_parent_heavy_admission(monkeypatch):
    from types import SimpleNamespace
    from app.services.resource_aware_worker import ResourceAwareWorker
    worker = object.__new__(ResourceAwareWorker)
    def forbidden(*args, **kwargs):
        raise AssertionError("poll coordinator must let child distinguish polling from submission")
    monkeypatch.setattr(worker, "_wait_until_pressure_allows_dequeue", forbidden)
    job = SimpleNamespace(id="poll", func_name="app.jobs.search_projection.run_search_projection_outbox", args=(), kwargs={}, meta={})
    lease, flock, owner = worker._profile_admission(job, "search_index")
    assert lease is None
    assert flock is None


@pytest.mark.asyncio
async def test_controlled_120_second_remote_flight_releases_every_local_slice(delivery):
    import time
    from app.services.heavy_io import local_lock_for_workload
    await enqueue_delete()
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        await delivery.run_delivery_slice(client=client)
        for elapsed in range(15, 121, 15):
            await ready_receipt()
            started = time.monotonic()
            assert (await delivery.run_delivery_slice(client=client))["status"] == "pending"
            assert time.monotonic() - started < 20
            # The remote duration is controlled by this clock: all 120 seconds
            # remain pending while local import transactions can complete.
            ingest = local_lock_for_workload("import_db")
            assert ingest.try_acquire()
            try:
                async with async_session() as db:
                    await request_search_projection(db, deleted_tag_ids=[uuid4()])
                    await db.commit()
            finally:
                ingest.release()
        assert len(remote.writes) == 1
        assert remote.polls == 8
        await ready_receipt()
        remote.status = "succeeded"
        assert (await delivery.run_delivery_slice(client=client))["status"] == "ok"


@pytest.mark.asyncio
async def test_cancel_during_marker_fsync_does_not_release_its_caller(delivery):
    import asyncio
    import threading
    entered, finish = threading.Event(), threading.Event()
    def writing_marker():
        entered.set()
        finish.wait(timeout=2)
    task = asyncio.create_task(delivery.durable(writing_marker))
    try:
        await asyncio.to_thread(entered.wait, .5)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_terminal_receipt_drops_large_payload_but_keeps_identity(delivery):
    from app.models.search_delivery_receipt import SearchDeliveryReceipt
    await enqueue_delete()
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        await delivery.run_delivery_slice(client=client)
        await ready_receipt()
        remote.status = "succeeded"
        await delivery.run_delivery_slice(client=client)
    async with async_session() as db:
        row = (await db.execute(select(SearchDeliveryReceipt))).scalar_one()
        assert row.payload == []
        assert row.task_uid == 41
        assert len(row.versions) == 1


@pytest.mark.asyncio
async def test_additive_migration_roundtrip_refuses_active_remote_identity(monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.database import engine
    module_path = Path(__file__).parents[1] / "alembic/versions/f8d0e2a4b6c8_add_search_delivery_receipts.py"
    spec = importlib.util.spec_from_file_location("delivery_migration", module_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "delivery_migration_" + uuid4().hex
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        def verify(sync_connection):
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync_connection)))
            migration.upgrade()
            sync_connection.execute(text("""INSERT INTO search_delivery_receipts
                (id,index_uid,action,versions,payload,state,phase,poll_count)
                VALUES (:id,'test','delete','[]','[]','pending','documents',0)"""), {"id": uuid4()})
            with pytest.raises(Exception, match="Drain or reconcile"):
                with sync_connection.begin_nested():
                    migration.downgrade()
            sync_connection.execute(text("UPDATE search_delivery_receipts SET state='complete'"))
            migration.downgrade()
            migration.upgrade()
            assert sync_connection.execute(text("SELECT count(*) FROM search_delivery_receipts")).scalar_one() == 0
        await connection.run_sync(verify)
        await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
