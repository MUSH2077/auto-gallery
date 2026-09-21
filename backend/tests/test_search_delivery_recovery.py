"""Behavioral crash and remote-admission regression cases."""
import time
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.database import async_session
from tests.test_search_delivery import delivery, enqueue_delete, ready_receipt, Remote  # noqa: F401
from tests.test_search_rebuild_delivery import Meili


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_task", [None, 41])
async def test_prepared_owned_marker_requires_new_admission(delivery, monkeypatch, previous_task):
    from app.services import heavy_io
    await enqueue_delete()
    receipt = await delivery._prepare(500, time.monotonic() + 20)
    await ready_receipt()
    if previous_task is not None:
        async with async_session() as db:
            await db.execute(text("UPDATE search_delivery_receipts SET phase='documents' WHERE id=:id"), {"id": receipt.id})
            await db.commit()
    delivery.remote_flight.create_marker(str(receipt.id))
    delivery.remote_flight.record_task(str(receipt.id), previous_task)
    admissions = []
    @asynccontextmanager
    async def denied(*args, **kwargs):
        admissions.append(args[0])
        yield None
    monkeypatch.setattr(heavy_io, "adaptive_resource_slice", denied)
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        outcome = await delivery.run_delivery_slice(client=client)
    assert remote.writes == []
    assert admissions == ["search_index"]
    assert outcome["status"] == "deferred"
    assert delivery.remote_flight.read_marker() is None


@pytest.mark.asyncio
async def test_redis_restart_between_restore_and_grant_cannot_erase_remote_budget(delivery):
    from app.services.heavy_io import RenewableRedisLease, RESOURCE_DISK_TOKEN_KEY
    from app.services.redis_client import get_redis
    redis = get_redis()
    redis.flushdb()
    delivery.remote_flight.create_marker(str(uuid4()))
    class RestartingRedis:
        def __getattr__(self, name):
            return getattr(redis, name)
        def eval(self, script, *args):
            redis.flushdb()
            return redis.eval(script, *args)
    lease = RenewableRedisLease([RESOURCE_DISK_TOKEN_KEY], workload="import_db", owner="import", redis_client=RestartingRedis(),
        reservation_key=RESOURCE_DISK_TOKEN_KEY, reservation_bytes=128 * 1024 * 1024, reservation_capacity_bytes=300 * 1024 * 1024)
    try:
        assert not await lease.try_acquire()
        assert redis.ttl(delivery.remote_flight.REMOTE_RESERVATION_KEY) == -1
    finally:
        await lease.release()
        redis.flushdb()


@pytest.mark.asyncio
async def test_pending_remote_swap_excludes_ingest_and_network_until_terminal(delivery):
    from app.models.search_rebuild import SearchRebuild
    from app.services import search_rebuild, heavy_io
    from app.services.search import TAGS_INDEX
    from app.services.redis_client import get_redis
    redis = get_redis()
    redis.flushdb()
    started = await search_rebuild.start_rebuild((TAGS_INDEX,))
    build_id = UUID(started["build_id"])
    async with async_session() as db:
        build = await db.get(SearchRebuild, build_id)
        build.phase = "swap"
        await db.commit()
    remote = Meili()
    remote.indexes[f"{TAGS_INDEX}__staging_{build_id.hex[:12]}"] = {}
    locks = [heavy_io.local_lock_for_workload(workload) for workload in ("import_db", "download_network", "image_derive", "maintenance")]
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        try:
            await delivery.run_delivery_slice(client=client)
            marker = delivery.remote_flight.read_marker()
            assert marker["workload"] == marker["profile"] == "maintenance"
            assert heavy_io.collect_active_resource_leases(redis)["maintenance_active"]
            redis.flushdb()
            assert all(not lock.try_acquire() for lock in locks)
            for workload in ("import_db", "download_network"):
                key = heavy_io.resource_budget_token_key(workload)
                lease = heavy_io.RenewableRedisLease([key], workload=workload, owner="blocked", redis_client=redis,
                    reservation_key=key, reservation_bytes=1, reservation_capacity_bytes=10**10)
                assert not await lease.try_acquire()
            remote.pending = True
            await ready_receipt()
            assert (await delivery.run_delivery_slice(client=client))["status"] == "pending"
            assert delivery.remote_flight.read_marker()["profile"] == "maintenance"
            redis.flushdb()
            assert all(not lock.try_acquire() for lock in locks)
            remote.pending = False
            await ready_receipt()
            await delivery.run_delivery_slice(client=client)
            assert delivery.remote_flight.read_marker() is None
            async with async_session() as db:
                build = await db.get(SearchRebuild, build_id)
                assert build.progress["swapped"] is True
            for lock in locks:
                assert lock.try_acquire()
                lock.release()
        finally:
            for lock in locks:
                lock.release()
            redis.flushdb()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["cancelled", "remote_failure", "cancelled_after_swap", "cleanup_failure"])
async def test_failed_rebuild_durably_cleans_staging_and_replay(delivery, cause):
    from app.models.search_rebuild import SearchRebuild, SearchRebuildReplay
    from app.models.task_run import TaskRun
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services import search_rebuild
    from app.services.search import TAGS_INDEX
    from app.services.search_projection_outbox import request_search_projection
    owner = uuid4()
    async with async_session() as db:
        db.add(TaskRun(id=owner, kind="admin", operation_type="admin-search-reindex", status="running"))
        await request_search_projection(db, deleted_tag_ids=[uuid4()])
        await db.commit()
    started = await search_rebuild.start_rebuild((TAGS_INDEX,), owner=str(owner))
    build_id = UUID(started["build_id"])
    staging = f"{TAGS_INDEX}__staging_{build_id.hex[:12]}"
    remote = Meili()
    remote.indexes[staging] = {"staged": {"id": "staged"}}
    async with async_session() as db:
        event = (await db.execute(select(SearchProjectionOutbox))).scalar_one()
        db.add(SearchRebuildReplay(build_id=build_id, outbox_id=event.id, version=event.version))
        build = await db.get(SearchRebuild, build_id)
        if cause == "remote_failure":
            build.phase = "settings"
        else:
            task = await db.get(TaskRun, owner)
            task.status = "cancelled"
            build.phase = "acknowledge" if cause == "cancelled_after_swap" else "replay"
            build.progress = {**build.progress, "swapped": cause == "cancelled_after_swap"}
        await db.commit()
    failed_once = False
    def transport(request):
        nonlocal failed_once
        if cause == "cleanup_failure" and request.method == "DELETE" and not failed_once:
            failed_once = True
            return httpx.Response(202, json={"taskUid": 901})
        if cause == "cleanup_failure" and request.url.path == "/tasks/901":
            return httpx.Response(200, json={"status": "failed", "error": {"message": "temporary cleanup error"}})
        if cause == "remote_failure" and request.url.path.startswith("/tasks/") and not failed_once:
            failed_once = True
            return httpx.Response(200, json={"status": "failed", "error": {"message": "staging failed"}})
        return remote(request)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            for _ in range(20):
                await delivery.run_delivery_slice(client=client)
                await ready_receipt()
                state = await search_rebuild.rebuild_status(build_id)
                if state["status"] == "error":
                    break
                if cause == "cleanup_failure":
                    async with async_session() as db:
                        await db.execute(text("UPDATE search_delivery_receipts SET write_available_at=NULL"))
                        await db.commit()
        assert staging not in remote.indexes
        assert state["status"] == "error"
        assert "staging failed" in state["message"] if cause == "remote_failure" else "owner stopped" in state["message"]
        async with async_session() as db:
            assert not list((await db.execute(select(SearchRebuildReplay))).scalars())
            event = (await db.execute(select(SearchProjectionOutbox))).scalar_one()
            assert (event.completed_at is not None) == (cause == "cancelled_after_swap")
        assert not [path for _, path, _ in remote.writes if path == "/swap-indexes"]
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM task_runs WHERE id=:id"), {"id": owner})
            await db.commit()
