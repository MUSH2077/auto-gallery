from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


@pytest.fixture
def refused_meilisearch(monkeypatch):
    import socket
    from app.config import settings

    # An unused ephemeral port refuses locally; reserved low ports can be
    # silently filtered by the host and consume the unrelated probe timeout.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(settings, "meili_url", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/api/v1/creators/count", "/api/v1/system/queue-stats"])
async def test_redis_pause_does_not_stall_http_event_loop(endpoint):
    import asyncio
    import time
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete
    from app.auth import create_access_token
    from app.database import async_session, engine
    from app.main import app
    from app.models.user import User
    from app.services.redis_client import get_redis

    username = "http_redis_budget_test"
    async with async_session() as db:
        await db.execute(delete(User).where(User.username == username))
        user = User(username=username, password_hash="unused", is_admin=True, is_active=True, must_change_password=False)
        db.add(user)
        await db.commit()
        token = create_access_token(user)
    gaps = []
    done = asyncio.Event()
    async def ticker():
        previous = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.monotonic()
            gaps.append(now - previous)
            previous = now
    tick = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(0.02)
        await asyncio.to_thread(get_redis().execute_command, "CLIENT", "PAUSE", 500)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(endpoint, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        assert max(gaps) < 0.15, gaps
    finally:
        done.set()
        await tick
        async with async_session() as db:
            await db.execute(delete(User).where(User.username == username))
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workbench_health_redis_pause_allows_other_http_requests(monkeypatch, refused_meilisearch):
    import asyncio
    import time
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete
    from app.api import system
    from app.auth import create_access_token
    from app.database import async_session, engine
    from app.main import app
    from app.models.user import User
    from app.services.redis_client import get_redis

    username = "workbench_health_test_" + uuid4().hex
    async with async_session() as db:
        user = User(username=username, password_hash="unused", is_admin=True, is_active=True, must_change_password=False)
        db.add(user)
        await db.commit()
        token = create_access_token(user)

    health_started = asyncio.Event()
    original_health = system._quick_service_health
    async def pause_at_health(db):
        # Pause at this boundary so preceding queue reads cannot consume the
        # fault before the real health check executes.
        await asyncio.to_thread(get_redis().execute_command, "CLIENT", "PAUSE", 500)
        health_started.set()
        return await original_health(db)
    monkeypatch.setattr(system, "_workbench_cache", None)
    monkeypatch.setattr(system, "_workbench_cache_ts", 0)
    gaps = []
    done = asyncio.Event()
    async def ticker():
        previous = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.monotonic()
            gaps.append(now - previous)
            previous = now
    tick = asyncio.create_task(ticker())
    request = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as client:
            # Warm the full HTTP path so the timing measures the injected
            # Redis fault rather than one-time imports and serializers.
            healthy = await client.get("/api/v1/system/workbench", params={"refresh": "true"})
            assert healthy.status_code == 200, healthy.text
            assert healthy.json()["health"]["redis"] == "up"
            monkeypatch.setattr(system, "_quick_service_health", pause_at_health)
            await asyncio.sleep(0.02)
            gaps.clear()
            request = asyncio.create_task(client.get("/api/v1/system/workbench", params={"refresh": "true"}))
            await asyncio.wait_for(health_started.wait(), timeout=2)
            peer = await asyncio.wait_for(client.get("/api/v1/auth/me"), timeout=0.3)
            assert peer.status_code == 200
            assert not request.done(), "peer must complete while the health probe is waiting"
            response = await asyncio.wait_for(request, timeout=2)
            assert response.status_code == 200, response.text
            assert response.json()["health"]["redis"] == "down"
            assert max(gaps) < 0.15, gaps
    finally:
        if request is not None and not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        done.set()
        await tick
        async with async_session() as db:
            await db.execute(delete(User).where(User.username == username))
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["refused", "silent"])
@pytest.mark.parametrize("endpoint", ["/api/v1/creators/count", "/api/v1/system/queue-stats", "/api/v1/system/workbench?refresh=true"])
async def test_unavailable_redis_reads_finish_without_blocking_other_requests(endpoint, fault, monkeypatch, refused_meilisearch):
    import asyncio
    import time
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete
    from app.auth import create_access_token
    from app.config import settings
    from app.database import async_session, engine
    from app.main import app
    from app.models.user import User

    writers = []
    async def silent(reader, writer):
        writers.append(writer)
        try:
            await reader.read()
        finally:
            writer.close()
    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    if fault == "refused":
        server.close()
        await server.wait_closed()
    username = "http_redis_unavailable_test"
    request = None
    from app.api import system
    monkeypatch.setattr(system, "_workbench_cache", None)
    monkeypatch.setattr(system, "_workbench_cache_ts", 0)
    try:
        async with async_session() as db:
            await db.execute(delete(User).where(User.username == username))
            user = User(username=username, password_hash="unused", is_admin=True, is_active=True, must_change_password=False)
            db.add(user)
            await db.commit()
            token = create_access_token(user)
        with monkeypatch.context() as patch:
            patch.setattr(settings, "redis_url", f"redis://127.0.0.1:{port}/15")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as client:
                start = time.monotonic()
                request = asyncio.create_task(client.get(endpoint))
                await asyncio.sleep(0.03)
                peer_start = time.monotonic()
                peer = await asyncio.wait_for(client.get("/api/v1/auth/me"), timeout=0.3)
                assert peer.status_code == 200
                assert time.monotonic() - peer_start < 0.3
                deadline = 3 if "workbench" in endpoint else 1.5
                response = await asyncio.wait_for(request, timeout=deadline)
                assert response.status_code == 200, response.text
                assert time.monotonic() - start < deadline
                if "workbench" in endpoint:
                    assert response.json()["health"]["redis"] == "down"
                    assert response.json()["queue"]["default"] == -1
                elif "queue-stats" in endpoint:
                    assert response.json()["default_queue"] == -1
                else:
                    assert response.json()["count"] >= 0
    finally:
        if request is not None and not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        server.close()
        await server.wait_closed()
        for writer in writers:
            writer.close()
        async with async_session() as db:
            await db.execute(delete(User).where(User.username == username))
            await db.commit()
        await engine.dispose()


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def one(self):
        return self._rows[0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_queue_stats_preserve_registry_expiry_cleanup(monkeypatch):
    import time
    from uuid import uuid4
    from rq import Queue
    from rq.registry import FailedJobRegistry
    from app.api import system
    from app.services.redis_client import get_redis

    redis = get_redis()
    name = "http-queue-test-" + uuid4().hex
    queue = Queue(name, connection=redis)
    registry = FailedJobRegistry(queue=queue)
    redis.rpush(queue.key, "queued-fixture")
    redis.zadd(registry.key, {"expired-fixture": time.time() - 30, "retained-fixture": time.time() + 300})
    monkeypatch.setattr(system, "QUEUE_NAMES", ("default", "scheduled", name))
    try:
        result = await system._queue_stats_payload()
        assert result["queues"][name] == {"queued": 1, "scheduled": 0, "started": 0, "failed": 1}
        assert redis.zcard(registry.key) == 1
    finally:
        redis.delete(queue.key, registry.key)


class _Result:
    def __init__(self, *, scalar_value=None, rows=None, scalar_rows=None):
        self._scalar_value = scalar_value
        self._rows = rows or []
        self._scalar_rows = scalar_rows or []

    def scalar(self):
        return self._scalar_value

    def all(self):
        return self._rows

    def one(self):
        return self._rows[0]

    def scalars(self):
        return _Scalars(self._scalar_rows)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_limit_retains_window_after_a_redis_response_timeout():
    import asyncio
    from uuid import uuid4
    from app.services.rate_limiter import RateLimiter
    from app.services.redis_client import get_redis

    redis = get_redis()
    limiter = RateLimiter(prefix="http-limit-test-" + uuid4().hex, max_requests=2, window_seconds=60)
    key = limiter._key("fixture")
    try:
        first, second, rejected = [await limiter.check("fixture") for _ in range(3)]
        assert first[0] and first[1] == 1
        assert second[0] and second[1] == 0
        assert rejected[0] is False and rejected[2] > 0
        redis.delete(key)
        await asyncio.to_thread(redis.execute_command, "CLIENT", "PAUSE", 500)
        await limiter.check("fixture")
        await asyncio.sleep(0.2)
        # Redis may reject the connection before the script arrives. If it
        # did execute, the counter must still have a finite expiry.
        ttl = redis.ttl(key)
        assert ttl == -2 or 0 < ttl <= 60
    finally:
        redis.delete(key)


class _Session:
    def __init__(self, results):
        self._results = iter(results)
        self.execute_count = 0

    async def execute(self, _statement):
        self.execute_count += 1
        return next(self._results)


@pytest.mark.asyncio
async def test_workbench_refresh_populates_cache_and_exposes_recent_context(monkeypatch):
    from app.api import system

    now = datetime(2026, 7, 28, 8, 30, tzinfo=timezone.utc)
    creator = SimpleNamespace(id=uuid4(), display_name="Atlas Ink", name="atlas")
    subscription = SimpleNamespace(id=uuid4(), name="Atlas archive")
    download = SimpleNamespace(
        id=uuid4(),
        subscription_id=subscription.id,
        subscription_source_id=uuid4(),
        source="pixiv",
        source_url="https://www.pixiv.net/users/10000001",
        status="downloading",
        pipeline_stage="download",
        progress_data={"current": 48, "total": 132, "percent": 36.4},
        created_at=now,
        updated_at=now,
        error_log=None,
    )
    import_job = SimpleNamespace(
        id=uuid4(),
        download_job_id=download.id,
        status="running",
        progress_stage="works",
        progress_works_done=8,
        progress_works_total=12,
        progress_data={"current": 8, "total": 12, "percent": 66.7},
        created_at=now,
        updated_at=now,
        error_log=None,
    )
    work = SimpleNamespace(
        id=uuid4(),
        title="Harbor Light Study",
        thumbnail_asset_id=uuid4(),
        created_at=now,
    )
    source = SimpleNamespace(id=uuid4(), source="pixiv", source_url=download.source_url, last_synced_at=now)

    session = _Session([
        _Result(rows=[(1, 0, 0, 1, 0, 0)]),
        _Result(rows=[(download, subscription, creator)]),
        _Result(rows=[(import_job, download, subscription, creator)]),
        _Result(scalar_rows=[work]),
        _Result(scalar_rows=[work.id]),
        _Result(rows=[(work.id, "pixiv", creator.display_name, creator.name)]),
        _Result(rows=[(source, subscription, creator)]),
    ])

    monkeypatch.setattr(system, "_workbench_cache", {"cached": True})
    monkeypatch.setattr(system, "_workbench_cache_ts", system.time.monotonic())
    monkeypatch.setattr(
        system.os,
        "statvfs",
        lambda _path: SimpleNamespace(f_frsize=1, f_blocks=1000, f_bavail=620),
    )
    monkeypatch.setattr(system, "_queue_stats_payload", AsyncMock(return_value={
        "default_queue": 2,
        "scheduled_queue": 1,
        "failed_jobs": 1,
        "started_jobs": 2,
        "next_sync_scan_at": now.isoformat(),
    }))
    monkeypatch.setattr(system, "get_scheduler_config", AsyncMock(return_value={
        "scheduler_enabled": True,
        "schedule_mode": "interval",
        "timezone": "Asia/Shanghai",
        "scheduler_scan_interval_minutes": 15,
    }))
    monkeypatch.setattr(system, "_count_active_rebuilds", AsyncMock(return_value=0))
    monkeypatch.setattr(system, "_get_proxy_health_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(system, "_quick_service_health", AsyncMock(return_value={"backend": "up"}))
    monkeypatch.setattr(system, "auth_attention_counts", AsyncMock(return_value={
        "auth_actionable_count": 0,
        "auth_disabled_or_unchecked_count": 0,
        "credential_issue_count": 0,
    }))

    actor = SimpleNamespace(id=1, is_admin=True, permissions=[])
    async def enrich(_db, rows, **kwargs):
        for row in rows:
            row.available_actions, row.disabled_reasons = [], {}
    monkeypatch.setattr(system, "enrich_actions", enrich)
    payload = await system.workbench_summary(refresh=True, db=session, user=actor)

    assert payload["queue"]["active_download_count"] == 1
    assert payload["recent"]["download_jobs"][0]["creator_name"] == "Atlas Ink"
    assert payload["recent"]["download_jobs"][0]["progress_data"]["current"] == 48
    assert payload["recent"]["import_jobs"][0]["source"] == "pixiv"
    assert payload["recent"]["import_jobs"][0]["progress_works_total"] == 12
    assert payload["recent"]["works"][0]["source"] == "pixiv"
    assert payload["recent"]["works"][0]["creator_name"] == "Atlas Ink"
    assert payload["recent"]["works"][0]["has_video"] is True
    assert system._workbench_cache is payload
    assert system._WORKBENCH_CACHE_TTL == 10.0

    cached = await system.workbench_summary(refresh=False, db=SimpleNamespace(), user=actor)
    assert cached is payload
    assert session.execute_count == 7


@pytest.mark.asyncio
async def test_task_event_marks_shared_workbench_invalidation(monkeypatch):
    from app.services import tasks

    marked = []
    monkeypatch.setattr(
        tasks,
        "mark_workbench_invalidation_pending",
        lambda db: marked.append(db),
    )

    class Session:
        def __init__(self):
            self.rows = []

        def add(self, row):
            self.rows.append(row)

        async def flush(self):
            return None

    db = Session()
    task = SimpleNamespace(id=uuid4())

    await tasks.TaskService(db).add_event(
        task,
        "status_changed",
        from_status="running",
        to_status="complete",
    )

    assert marked == [db]
