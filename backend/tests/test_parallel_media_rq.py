"""Cross-process correctness checks for the real media RQ path.

These tests intentionally use PostgreSQL, Redis and RQ workers. Throughput
measurements belong to the parallelism benchmark and are kept out of this
module.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import redis
from rq import Queue
from sqlalchemy import delete, select, update


pytestmark = pytest.mark.integration

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@dataclass
class MediaEnvironment:
    download_root: Path
    library_root: Path
    lock_path: Path
    assets: list[UUID]
    namespace: str
    redis: redis.Redis


def _normal_snapshot() -> dict:
    from app.services.resource_pressure import RESOURCE_PROFILES

    profiles = {
        name: {
            "allowed": True,
            "reason": None,
            "memory_reservation_bytes": int(profile.memory_reservation_bytes),
            "work_units": 1,
            "slice_seconds": 20.0,
        }
        for name, profile in RESOURCE_PROFILES.items()
    }
    return {
        "status": "normal",
        "controller_mode": "normal",
        "reasons": [],
        "budget": {
            "governance_mode": "enforce",
            "throughput_scale": 1.0,
            "profiles": profiles,
            "reservation": {"reserved_bytes": 0, "active_count": 0},
        },
        "memory": {"available_bytes": 16 * 1024**3},
        "controller": {"hard_limits": {"memory_available_bytes": 256 * 1024**2}},
    }


def _paused_snapshot() -> dict:
    snapshot = _normal_snapshot()
    snapshot["status"] = "paused"
    snapshot["controller_mode"] = "critical"
    snapshot["reasons"] = ["test_low_memory"]
    snapshot["budget"]["profiles"]["image_derive"].update(
        allowed=False, reason="test_low_memory"
    )
    return snapshot


def _publish_snapshot(client: redis.Redis, snapshot: dict) -> None:
    from app.services.resource_pressure_snapshot import PRESSURE_SNAPSHOT_KEY

    client.set(PRESSURE_SNAPSHOT_KEY, json.dumps(snapshot), ex=120)


def _wait_for(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("timed out waiting for the real RQ worker boundary")


async def _wait_for_async(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.03)
    raise AssertionError("timed out waiting for the real database boundary")


def _wait_for_redis_count(client: redis.Redis, key: str, wanted: int) -> None:
    _wait_for(lambda: int(client.get(key) or 0) >= wanted)


def _run_media_consumer(namespace: str, queue_name: str, mode: str) -> None:
    """Run a real ResourceAwareWorker in a fresh interpreter."""

    import redis as redis_module
    from rq import Queue as RQQueue

    from app.services import media_derivatives
    from app.services.resource_aware_worker import ResourceAwareWorker

    client = redis_module.Redis.from_url(os.environ["REDIS_URL"])
    test_queue = RQQueue(queue_name, connection=client)
    operations = RQQueue("operations", connection=client)

    class TestWorker(ResourceAwareWorker):
        @staticmethod
        def _is_heavy_queue(queue) -> bool:
            if mode == "pressure":
                return False
            return str(getattr(queue, "name", "")) in {queue_name, "operations"}

        def run_maintenance_tasks(self):
            return None

        def _publish_pressure_state(self, snapshot):
            # The parent test owns the injected shared snapshot.
            return {"hard_gate_active": False, "soft_scale": 1.0, "cgroup_deltas": {}}

    if mode in {"overlap", "hold"}:
        original = media_derivatives._derive_sync

        def instrumented(descriptor, request):
            pid = str(os.getpid())
            client.rpush(namespace + ":derive-pids", pid)
            client.incr(namespace + ":derive-start")
            if mode == "overlap":
                _wait_for_redis_count(client, namespace + ":derive-start", 2)
            else:
                client.set(namespace + ":claimed", str(request["id"]))
                client.set(namespace + ":workhorse-pid", pid)
                while not client.get(namespace + ":continue"):
                    time.sleep(0.02)
            client.set(namespace + ":running:" + pid, "1")
            try:
                if mode == "overlap":
                    client.incr(namespace + ":inside-start")
                    _wait_for_redis_count(client, namespace + ":inside-start", 2)
                    client.set(namespace + ":overlap", "1")
                return original(descriptor, request)
            finally:
                client.delete(namespace + ":running:" + pid)
                client.incr(namespace + ":derive-end")

        media_derivatives._derive_sync = instrumented

    worker = TestWorker(
        [test_queue, operations],
        connection=client,
        name=namespace + "-" + str(os.getpid()),
    )
    worker.work(
        burst=mode != "pressure",
        with_scheduler=False,
        logging_level="WARNING",
    )


def _start_consumer(env: MediaEnvironment, queue_name: str, mode: str, log: Path):
    handle = log.open("w+", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from tests.test_parallel_media_rq import _run_media_consumer; "
            "import sys; _run_media_consumer(sys.argv[1], sys.argv[2], sys.argv[3])",
            env.namespace,
            queue_name,
            mode,
        ],
        env={**os.environ, "HEAVY_IO_LOCK_PATH": str(env.lock_path)},
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process, handle


@pytest.fixture
async def media_environment(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session
    from app.models import Asset, AssetSource, MediaDerivativeOutbox, Work, WorkSource
    from app.services.media_derivatives import request_media_derivatives
    from app.services.redis_client import get_redis

    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    lock_path = tmp_path / "locks" / "heavy-io.lock"
    download_root.mkdir()
    library_root.mkdir()
    lock_path.parent.mkdir()
    monkeypatch.setenv("DOWNLOAD_ROOT", str(download_root))
    monkeypatch.setenv("LIBRARY_ROOT", str(library_root))
    monkeypatch.setenv("HEAVY_IO_LOCK_PATH", str(lock_path))
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr(settings, "library_root", str(library_root))

    namespace = "parallel-media-" + uuid4().hex
    client = get_redis()
    assert client.connection_pool.connection_kwargs["db"] == 15
    client.flushdb()
    monkeypatch.setenv("MEDIA_WORKER_CONCURRENCY", "1")
    monkeypatch.setattr(settings, "media_worker_concurrency", 1)

    rows = []
    async with async_session() as db:
        for index in range(8):
            work = Work(title=f"RQ media correctness {namespace}-{index}")
            source = WorkSource(
                work=work,
                source="manual",
                source_work_id=f"{namespace}-{index}",
                raw_metadata={},
            )
            relative = f"{namespace}/{index}.png"
            asset = Asset(
                file_path=relative,
                file_name=f"{index}.png",
                file_size=len(_PNG),
                mime_type="image/png",
                width=1,
                height=1,
            )
            link = AssetSource(
                asset=asset,
                work_source=source,
                source="manual",
                source_asset_id=f"{namespace}-{index}",
                ordinal=0,
                role="page",
            )
            db.add_all([work, source, asset, link])
            rows.append((work, source, asset, link))
        await db.flush()
        requests = []
        for _work, _source, asset, _link in rows:
            path = download_root / asset.file_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_PNG)
            stat = path.stat()
            requests.append(
                {
                    "asset_id": asset.id,
                    "requested": {"thumbnail": True, "sha256": True},
                    "source_size": stat.st_size,
                    "source_mtime_ns": stat.st_mtime_ns,
                }
            )
        await request_media_derivatives(db, requests)
        await db.commit()

    # These tests publish their own real RQ jobs after seeding. Remove only the
    # seed's notifications from the suite-owned Redis DB15 before consumers run.
    client.flushdb()
    env = MediaEnvironment(
        download_root=download_root,
        library_root=library_root,
        lock_path=lock_path,
        assets=[asset.id for _work, _source, asset, _link in rows],
        namespace=namespace,
        redis=client,
    )
    try:
        yield env
    finally:
        client.flushdb()
        async with async_session() as db:
            ids = env.assets
            await db.execute(delete(MediaDerivativeOutbox).where(MediaDerivativeOutbox.asset_id.in_(ids)))
            await db.execute(delete(AssetSource).where(AssetSource.asset_id.in_(ids)))
            work_ids = [work.id for work, _source, _asset, _link in rows]
            await db.execute(delete(WorkSource).where(WorkSource.work_id.in_(work_ids)))
            await db.execute(delete(Asset).where(Asset.id.in_(ids)))
            await db.execute(delete(Work).where(Work.id.in_(work_ids)))
            await db.commit()


async def _asset_outbox(asset_id: UUID):
    from app.database import async_session
    from app.models import Asset, MediaDerivativeOutbox

    async with async_session() as db:
        asset = await db.scalar(select(Asset).where(Asset.id == asset_id))
        outbox = await db.scalar(
            select(MediaDerivativeOutbox).where(MediaDerivativeOutbox.asset_id == asset_id)
        )
        return asset, outbox


def _enqueue_media(client: redis.Redis, queue_name: str, *, job_id: str | None = None):
    queue = Queue(queue_name, connection=client)
    return queue.enqueue(
        "app.jobs.media_derivatives.run_media_derivative_outbox",
        1,
        20.0,
        job_id=job_id or "parallel-media-job-" + uuid4().hex,
        job_timeout=3600,
        result_ttl=300,
        failure_ttl=300,
        meta={"resource_descriptor": "media-derivatives"},
    )


async def _keep_only_first_intent(env: MediaEnvironment) -> None:
    from app.database import async_session
    from app.models import MediaDerivativeOutbox

    async with async_session() as db:
        await db.execute(
            delete(MediaDerivativeOutbox).where(
                MediaDerivativeOutbox.asset_id.in_(env.assets[1:])
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_real_rq_consumers_claim_one_asset_and_emit_one_output(media_environment, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setenv("MEDIA_WORKER_CONCURRENCY", "4")
    monkeypatch.setattr(settings, "media_worker_concurrency", 4)
    env = media_environment
    queue_name = "parallel-media:" + env.namespace
    await _keep_only_first_intent(env)
    jobs = [_enqueue_media(env.redis, queue_name) for _ in range(4)]
    processes = []
    handles = []
    try:
        for index in range(4):
            process, handle = _start_consumer(
                env, queue_name, "normal", tmp_path / f"claim-{index}.log"
            )
            processes.append(process)
            handles.append(handle)
        async def complete():
            _asset, outbox = await _asset_outbox(env.assets[0])
            return outbox is not None and outbox.state == "complete"
        await _wait_for_async(complete)
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        for handle in handles:
            handle.close()

    asset, outbox = await _asset_outbox(env.assets[0])
    assert outbox.state == "complete"
    assert outbox.attempts == 1
    assert asset.thumb_sm_path
    output = env.library_root / asset.thumb_sm_path
    assert output.is_file() and output.stat().st_size > 0
    assert asset.sha256
    assert len({job.id for job in jobs}) == 4


@pytest.mark.asyncio
async def test_real_rq_workhorses_overlap_inside_distinct_media_slots(media_environment, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setenv("MEDIA_WORKER_CONCURRENCY", "4")
    monkeypatch.setattr(settings, "media_worker_concurrency", 4)
    env = media_environment
    queue_name = "parallel-media:" + env.namespace
    await _keep_only_first_intent(env)
    from app.database import async_session
    from app.models import Asset
    from app.services.media_derivatives import request_media_derivatives

    async with async_session() as db:
        assets = [
            await db.scalar(select(Asset).where(Asset.id == asset_id))
            for asset_id in env.assets[:2]
        ]
        requests = []
        for asset in assets:
            source = env.download_root / asset.file_path
            stat = source.stat()
            requests.append(
                {
                    "asset_id": asset.id,
                    "requested": {"thumbnail": True, "sha256": True},
                    "source_size": stat.st_size,
                    "source_mtime_ns": stat.st_mtime_ns,
                }
            )
        await request_media_derivatives(db, requests)
        await db.commit()
    [_enqueue_media(env.redis, queue_name) for _ in range(2)]
    processes = []
    handles = []
    try:
        for index in range(2):
            process, handle = _start_consumer(
                env, queue_name, "overlap", tmp_path / f"overlap-{index}.log"
            )
            processes.append(process)
            handles.append(handle)
        _wait_for(lambda: env.redis.get(env.namespace + ":overlap") == b"1")
        _wait_for(lambda: int(env.redis.get(env.namespace + ":derive-end") or 0) >= 2)
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        for handle in handles:
            handle.close()

    pids = {value.decode() for value in env.redis.lrange(env.namespace + ":derive-pids", 0, -1)}
    assert len(pids) >= 2
    assert env.redis.get(env.namespace + ":overlap") == b"1"
    for asset_id in env.assets[:2]:
        asset, outbox = await _asset_outbox(asset_id)
        assert outbox.state == "complete"
        assert asset.thumb_sm_path and (env.library_root / asset.thumb_sm_path).is_file()


@pytest.mark.asyncio
async def test_real_job_waits_for_low_memory_then_recovery_scan_runs_once(media_environment, tmp_path):
    env = media_environment
    queue_name = "parallel-media:" + env.namespace
    await _keep_only_first_intent(env)
    _publish_snapshot(env.redis, _paused_snapshot())
    _enqueue_media(env.redis, queue_name)
    process, handle = _start_consumer(env, queue_name, "pressure", tmp_path / "pressure.log")
    try:
        _wait_for(lambda: env.redis.hexists("outbox:resource-deferred", "media"))
        _asset, outbox = await _asset_outbox(env.assets[0])
        assert outbox.state == "pending"
        _publish_snapshot(env.redis, _normal_snapshot())
        from app.services import outbox_coordinator

        first = await asyncio.to_thread(outbox_coordinator.wake_resource_deferred_outboxes)
        second = await asyncio.to_thread(outbox_coordinator.wake_resource_deferred_outboxes)
        assert first["enqueued"] == 1
        assert second["enqueued"] == 0
        async def complete():
            _asset, current = await _asset_outbox(env.assets[0])
            return current is not None and current.state == "complete"
        await _wait_for_async(complete)
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        handle.close()

    _asset, outbox = await _asset_outbox(env.assets[0])
    assert outbox.state == "complete"
    assert outbox.attempts == 1


@pytest.mark.asyncio
async def test_sigkill_after_real_claim_recovers_expired_lease_and_output(media_environment, tmp_path, monkeypatch):
    from app.config import settings
    from app.services.heavy_io import MEDIA_RESERVATION_KEYS

    monkeypatch.setenv("MEDIA_WORKER_CONCURRENCY", "4")
    monkeypatch.setattr(settings, "media_worker_concurrency", 4)
    env = media_environment
    queue_name = "parallel-media:" + env.namespace
    await _keep_only_first_intent(env)
    _enqueue_media(env.redis, queue_name)
    process, handle = _start_consumer(env, queue_name, "hold", tmp_path / "sigkill.log")
    try:
        _wait_for(lambda: env.redis.get(env.namespace + ":claimed"))
        workhorse_pid = int(env.redis.get(env.namespace + ":workhorse-pid"))
        assert any(env.redis.exists(key) for key in MEDIA_RESERVATION_KEYS)
        os.kill(workhorse_pid, signal.SIGKILL)
        _wait_for(lambda: process.poll() is not None)
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        handle.close()

    from app.database import async_session
    from app.models import MediaDerivativeOutbox
    from app.services.media_derivatives import request_media_derivatives

    # A different asset must continue while the killed asset's production
    # claim is still live. Advancing expiry must not hide a pool-wide stall.
    other_source = env.download_root / f"{env.namespace}/1.png"
    stat = other_source.stat()
    async with async_session() as db:
        await request_media_derivatives(db, [{
            "asset_id": env.assets[1], "requested": {"thumbnail": True, "sha256": True},
            "source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
        }])
        await db.commit()
    _enqueue_media(env.redis, queue_name)
    other_process, other_handle = _start_consumer(env, queue_name, "normal", tmp_path / "other-after-sigkill.log")
    try:
        async def other_complete():
            _asset, other = await _asset_outbox(env.assets[1])
            return other is not None and other.state == "complete"
        await _wait_for_async(other_complete)
        _asset, interrupted = await _asset_outbox(env.assets[0])
        assert interrupted.state == "processing"
    finally:
        if other_process.poll() is None:
            try:
                other_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(other_process.pid, signal.SIGKILL)
                other_process.wait(timeout=5)
        other_handle.close()

    async with async_session() as db:
        row = await db.scalar(
            select(MediaDerivativeOutbox).where(MediaDerivativeOutbox.asset_id == env.assets[0])
        )
        assert row.state == "processing"
        assert row.lease_expires_at is not None
        await db.execute(
            update(MediaDerivativeOutbox)
            .where(MediaDerivativeOutbox.id == row.id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await db.commit()

    # The real workhorse and parent have both exited, so their POSIX descriptors
    # are closed. Advance only the dead owner's Redis safety TTL, just as this
    # test advances the SQL lease, rather than waiting five production minutes.
    from app.services.heavy_io import RESOURCE_RESERVATION_KEYS, resource_lease_keys
    dead_lease_keys = []
    candidate_keys = set(RESOURCE_RESERVATION_KEYS) | set(resource_lease_keys("image_derive"))
    for key in candidate_keys:
        raw = env.redis.get(key)
        if raw and json.loads(raw).get("owner") == f"media-derivative:{env.assets[0]}":
            dead_lease_keys.append(key)
            env.redis.expire(key, 1)
    _wait_for(lambda: not any(env.redis.exists(key) for key in dead_lease_keys))

    recovery_job = _enqueue_media(env.redis, queue_name)
    process, handle = _start_consumer(env, queue_name, "normal", tmp_path / "sigkill-recovery.log")
    try:
        async def complete():
            _asset, current = await _asset_outbox(env.assets[0])
            return current is not None and current.state == "complete"
        await _wait_for_async(complete)
    finally:
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        handle.close()

    asset, outbox = await _asset_outbox(env.assets[0])
    assert recovery_job.get_status(refresh=True).value in {"finished", "queued"}
    assert outbox.state == "complete"
    assert outbox.lease_expires_at is None
    assert outbox.attempts == 2
    assert asset.thumb_sm_path
    assert (env.library_root / asset.thumb_sm_path).is_file()


@pytest.mark.asyncio
async def test_real_reclaimed_claim_cannot_overwrite_new_result(media_environment):
    from app.database import async_session
    from app.models import Asset, MediaDerivativeOutbox
    from app.services import media_derivatives

    old = await media_derivatives._claim_one(media_environment.assets[0])
    assert old is not None
    async with async_session() as db:
        await db.execute(
            update(MediaDerivativeOutbox)
            .where(MediaDerivativeOutbox.id == old["id"])
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await db.commit()
    new = await media_derivatives._claim_one(media_environment.assets[0])
    assert new is not None
    assert new["lease_expires_at"] != old["lease_expires_at"]

    stale = media_environment.library_root / "stale.webp"
    fresh = media_environment.library_root / "fresh.webp"
    stale.write_bytes(b"stale")
    fresh.write_bytes(b"fresh")
    assert await media_derivatives._complete(old, {"thumb_sm_path": "stale.webp"}) is False
    assert await media_derivatives._complete(new, {"thumb_sm_path": "fresh.webp"}) is True

    async with async_session() as db:
        asset = await db.scalar(select(Asset).where(Asset.id == media_environment.assets[0]))
        outbox = await db.scalar(
            select(MediaDerivativeOutbox).where(MediaDerivativeOutbox.asset_id == media_environment.assets[0])
        )
    assert asset.thumb_sm_path == "fresh.webp"
    assert outbox.state == "complete"
    assert outbox.lease_expires_at is None
    assert media_derivatives._LEASE_SECONDS == 30 * 60
