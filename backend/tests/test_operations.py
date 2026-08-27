import json
import asyncio
import threading
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import delete, select


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def setex(self, key, ttl, value):
        self.values[key] = value.encode("utf-8")
        self.ttls[key] = ttl

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value.encode() if isinstance(value, str) else value
        self.ttls[key] = ex
        return True


def test_operation_status_roundtrip(monkeypatch):
    from app.services import operations

    fake = FakeRedis()
    monkeypatch.setattr(operations, "get_redis", lambda: fake)

    payload = operations.set_operation_status(
        "job-1",
        "running",
        "admin-clear",
        progress={"phase": "running"},
        meta={"entity": "works"},
    )

    assert payload["status"] == "running"
    assert fake.ttls[operations.operation_key("job-1")] == operations.OPERATION_TTL_SECONDS
    assert operations.get_operation_status("job-1") == json.loads(
        fake.values[operations.operation_key("job-1")].decode("utf-8")
    )


def test_unknown_operation_returns_none(monkeypatch):
    from app.services import operations

    monkeypatch.setattr(operations, "get_redis", lambda: FakeRedis())

    assert operations.get_operation_status("missing") is None


def test_legacy_uuid_only_operation_lock_and_cache_remain_compatible(monkeypatch):
    """Non-bounded operations keep their historical UUID-only contract."""
    from app.services import operations

    fake = FakeRedis()
    monkeypatch.setattr(operations, "get_redis", lambda: fake)
    fake.values["library:legacy:active"] = b"legacy-job"

    operations.set_operation_status(
        "legacy-job",
        "running",
        "admin-rebuild",
        progress={"phase": "running"},
    )

    assert operations.get_operation_status("legacy-job")["status"] == "running"
    assert operations.release_owned_operation_lock(
        fake,
        "library:legacy:active",
        "legacy-job",
    ) is True
    assert fake.get("library:legacy:active") is None


def test_attemptless_operation_writes_cannot_mutate_attempt_owned_state(
    monkeypatch,
):
    """Rolling attemptless callers are stale once a private owner exists."""
    from app.services import operations

    fake = FakeRedis()
    monkeypatch.setattr(operations, "get_redis", lambda: fake)
    fake.values["library:disk-import:active"] = b"bounded-job"
    fake.values[operations.operation_attempt_key("bounded-job")] = b"attempt-b"
    operations.set_operation_status(
        "bounded-job",
        "running",
        "admin-disk-import",
        progress={"phase": "running"},
        publisher_attempt="attempt-b",
        redis_client=fake,
    )

    assert operations.set_operation_status(
        "bounded-job",
        "failed",
        "admin-disk-import",
        error="stale legacy delivery",
    ) is None
    assert operations.release_owned_operation_lock(
        fake,
        "library:disk-import:active",
        "bounded-job",
    ) is False
    assert fake.get("library:disk-import:active") == b"bounded-job"
    assert operations.get_operation_status("bounded-job")["status"] == "running"


def test_attempt_cas_fails_closed_when_redis_eval_is_unavailable(monkeypatch):
    """A Redis execution error never degrades attempt CAS to check-then-write."""
    from app.services import operations

    class FailingEvalRedis(FakeRedis):
        def eval(self, *_args, **_kwargs):
            raise RuntimeError("redis eval unavailable")

    fake = FailingEvalRedis()
    monkeypatch.setattr(operations, "get_redis", lambda: fake)
    fake.values["library:disk-import:active"] = b"bounded-job"
    fake.values[operations.operation_attempt_key("bounded-job")] = b"attempt-b"

    with pytest.raises(RuntimeError, match="eval unavailable"):
        operations.set_operation_status(
            "bounded-job",
            "running",
            "admin-disk-import",
            publisher_attempt="attempt-b",
            redis_client=fake,
        )
    with pytest.raises(RuntimeError, match="eval unavailable"):
        operations.release_owned_operation_lock(
            fake,
            "library:disk-import:active",
            "bounded-job",
            publisher_attempt="attempt-b",
        )
    with pytest.raises(RuntimeError, match="eval unavailable"):
        operations.acquire_operation_lock(
            fake,
            "library:other-disk-import:active",
            "other-job",
            ttl_seconds=60,
            publisher_attempt="attempt-c",
        )
    with pytest.raises(RuntimeError, match="eval unavailable"):
        operations.get_operation_status("bounded-job")
    with pytest.raises(RuntimeError, match="eval unavailable"):
        operations.set_operation_status(
            "legacy-job",
            "running",
            "admin-rebuild",
            redis_client=fake,
        )
    fake.values["library:legacy:active"] = b"legacy-job"
    with pytest.raises(RuntimeError, match="eval unavailable"):
        operations.release_owned_operation_lock(
            fake,
            "library:legacy:active",
            "legacy-job",
        )

    assert fake.get(operations.operation_key("bounded-job")) is None
    assert fake.get("library:disk-import:active") == b"bounded-job"
    assert fake.get("library:other-disk-import:active") is None
    assert fake.get("library:legacy:active") == b"legacy-job"


def test_attempt_release_requires_positive_pointer_ownership():
    """A missing pointer is uncertainty, never proof that an attempt owns a lock."""
    from app.services import operations
    from app.services.redis_client import get_redis

    redis_client = get_redis()
    job_id = f"release-positive-{uuid4()}"
    lock_key = f"library:disk-import:test:{uuid4()}"
    redis_client.delete(
        lock_key,
        operations.operation_attempt_key(job_id),
    )
    try:
        redis_client.set(lock_key, job_id, ex=60)

        assert operations.release_owned_operation_lock(
            redis_client,
            lock_key,
            job_id,
            publisher_attempt="attempt-a",
        ) is False
        assert redis_client.get(lock_key) is not None
    finally:
        redis_client.delete(
            lock_key,
            operations.operation_attempt_key(job_id),
        )


def test_legacy_status_racing_attempt_install_is_rejected_atomically():
    """A UUID-only publisher cannot write between B's owner install and cache."""
    from app.services import operations
    from app.services.redis_client import get_redis

    redis_client = get_redis()
    job_id = f"legacy-race-{uuid4()}"
    entered = threading.Event()
    resume = threading.Event()

    class InterleavingRedis:
        def get(self, key):
            value = redis_client.get(key)
            if key == operations.operation_attempt_key(job_id):
                entered.set()
                assert resume.wait(5)
            return value

        def eval(self, *args, **kwargs):
            entered.set()
            assert resume.wait(5)
            return redis_client.eval(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(redis_client, name)

    result: dict[str, object] = {}

    def legacy_write() -> None:
        result["value"] = operations.set_operation_status(
            job_id,
            "running",
            "admin-disk-import",
            progress={"phase": "legacy-a"},
            redis_client=InterleavingRedis(),
        )

    keys = (
        operations.operation_key(job_id),
        operations.operation_attempt_key(job_id),
        operations.operation_cache_attempt_key(job_id),
    )
    redis_client.delete(*keys)
    writer = threading.Thread(target=legacy_write, name="legacy-cache-writer")
    try:
        writer.start()
        assert entered.wait(5)
        redis_client.setex(
            operations.operation_attempt_key(job_id),
            60,
            "attempt-b",
        )
        resume.set()
        writer.join(5)
        assert not writer.is_alive()

        assert result["value"] is None
        assert redis_client.get(operations.operation_key(job_id)) is None
        assert operations.get_operation_status(job_id) is None
    finally:
        resume.set()
        if writer.is_alive():
            writer.join(5)
        redis_client.delete(*keys)


@pytest.mark.asyncio
async def test_active_operations_filter_stale_attempt_cache_and_keep_legacy():
    """The collection endpoint applies the same owner check as item reads."""
    from app.api.admin import data as data_api
    from app.services import operations
    from app.services.redis_client import get_redis

    redis_client = get_redis()
    stale_id = f"stale-list-{uuid4()}"
    legacy_id = f"legacy-list-{uuid4()}"
    stale_payload = json.dumps({
        "job_id": stale_id,
        "status": "running",
        "operation_type": "admin-disk-import",
        "updated_at": 2,
    })
    legacy_payload = json.dumps({
        "job_id": legacy_id,
        "status": "running",
        "operation_type": "admin-rebuild",
        "updated_at": 1,
    })
    keys = (
        operations.operation_key(stale_id),
        operations.operation_attempt_key(stale_id),
        operations.operation_cache_attempt_key(stale_id),
        operations.operation_key(legacy_id),
        operations.operation_attempt_key(legacy_id),
        operations.operation_cache_attempt_key(legacy_id),
    )
    redis_client.delete(*keys)
    try:
        redis_client.setex(operations.operation_key(stale_id), 60, stale_payload)
        redis_client.setex(
            operations.operation_attempt_key(stale_id),
            60,
            "attempt-b",
        )
        redis_client.setex(
            operations.operation_cache_attempt_key(stale_id),
            60,
            "attempt-a",
        )
        redis_client.setex(operations.operation_key(legacy_id), 60, legacy_payload)

        response = await data_api.list_active_operations()
        visible = {item["job_id"] for item in response["operations"]}

        assert stale_id not in visible
        assert legacy_id in visible
    finally:
        redis_client.delete(*keys)


def test_invalidate_api_caches_deletes_expected_domains(monkeypatch):
    from app.services import cache

    calls = []
    def fake_delete(pattern):
        calls.append(pattern)
        return 1

    monkeypatch.setattr(cache, "cache_delete_pattern", fake_delete)

    deleted = cache.invalidate_api_caches("creators", "subscriptions", "creators")

    assert calls == ["creators:*", "subscriptions:*"]
    assert deleted == {"creators": 1, "subscriptions": 1}


def test_interactive_cache_ttls_are_shorter_than_heavy_stats():
    from app.services.cache import TTL

    assert TTL["creators:list"] == 30
    assert TTL["creators:count"] == 30
    assert TTL["subscriptions:list"] == 30
    assert TTL["creators:stats"] == 60


def test_clear_entity_data_invalidates_related_cache_domains(monkeypatch):
    from app.services import admin_data

    class Result:
        rowcount = 1

    class FakeDB:
        async def execute(self, _stmt):
            return Result()

        async def commit(self):
            pass

        async def flush(self):
            pass

    calls = []
    async def fake_clear_search(_db, _entity):
        return None

    monkeypatch.setattr(admin_data, "_clear_files", lambda _paths: None)
    monkeypatch.setattr(admin_data, "_clear_search_index", fake_clear_search)
    async def fake_clear_failed_rq_jobs(_db):
        return 0

    monkeypatch.setattr(admin_data, "clear_failed_rq_jobs", fake_clear_failed_rq_jobs)
    monkeypatch.setattr(
        admin_data,
        "invalidate_api_caches",
        lambda *domains: calls.append(("api", domains)),
    )
    monkeypatch.setattr(
        admin_data,
        "invalidate_creator_subscription_caches",
        lambda include_works=False: calls.append(("creator-sub", include_works)),
    )

    asyncio.run(admin_data.clear_entity_data("creators", FakeDB()))
    asyncio.run(admin_data.clear_entity_data("jobs", FakeDB()))
    asyncio.run(admin_data.clear_entity_data("all", FakeDB()))

    assert ("creator-sub", True) in calls
    assert ("api", ("subscriptions", "creators")) in calls
    assert ("api", ("tags",)) in calls


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clear_jobs_deletes_history_but_preserves_active_task_authorities(
    monkeypatch,
):
    """The jobs clear keeps every nonterminal TaskRun and deletes terminal history."""
    from app.database import async_session, engine
    from app.models import TaskEvent, TaskRun
    from app.services import admin_data
    from app.services.tasks import TaskService

    monkeypatch.setattr(
        admin_data,
        "clear_failed_rq_jobs",
        lambda _db: asyncio.sleep(0, result=0),
    )
    active_ids: set[UUID] = set()
    terminal_id = None
    restore_authority_id = None
    try:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun))
            service = TaskService(db)
            for status in ("enqueued", "running", "paused", "recovering"):
                task = await service.create_task(
                    kind="admin",
                    operation_type="admin-search-reindex",
                    title=f"Active {status}",
                    status=status,
                )
                active_ids.add(task.id)
            terminal = await service.create_task(
                kind="admin",
                operation_type="admin-search-reindex",
                title="Old complete history",
                status="complete",
            )
            terminal_id = terminal.id
            restore_authority = await service.create_task(
                kind="admin",
                operation_type="admin-restore-validate",
                title="Completed restore handoff authority",
                status="complete",
            )
            restore_authority_id = restore_authority.id
            await db.commit()

            result = await admin_data.clear_entity_data("jobs", db)

        assert result["deleted"]["task_runs"] == 1
        async with async_session() as verify_db:
            remaining = set((await verify_db.execute(select(TaskRun.id))).scalars())
            assert active_ids <= remaining
            assert terminal_id not in remaining
            assert restore_authority_id in remaining
    finally:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun))
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_rebuild_checkpoints_in_taskrun_without_redis(monkeypatch):
    """A committed rebuild page stores its cursor below the current TaskRun attempt."""
    from app.database import async_session, engine
    from app.models import TaskEvent, TaskRun, Work
    from app.services import admin_data, operations
    from app.services import settings as settings_service

    monkeypatch.setattr(settings_service, "load_gallerydl_config", lambda: {})
    monkeypatch.setattr(
        admin_data,
        "get_redis",
        lambda: (_ for _ in ()).throw(RuntimeError("redis unavailable")),
    )
    task_id = None
    work_id = None
    observed_checkpoint: list[dict] = []
    try:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            work = Work(title=f"checkpoint-{uuid4()}")
            db.add(work)
            await db.flush()
            work_id = work.id
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-rebuild",
                scope_key="library:rebuild:active",
                title="Library rebuild",
                entity="library",
                options={"work_id": str(work_id), "batch_size": 10},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        async def observe_progress(progress):
            assert progress["scanned"] == 1
            async with async_session() as verify_db:
                task = await verify_db.get(TaskRun, task_id)
                dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
                observed_checkpoint.append(
                    dict(dispatch["checkpoints"]["library_rebuild"])
                )

        with operations.admin_operation_attempt_context(task_id, 1):
            async with async_session() as db:
                result = await admin_data.rebuild_library_index(
                    db,
                    {"work_id": str(work_id), "batch_size": 10},
                    observe_progress,
                )

        assert result["scanned"] == 1
        assert observed_checkpoint[0]["id"] == str(work_id)
        assert observed_checkpoint[0]["stats"]["scanned"] == 1
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert dispatch.get("checkpoints", {}).get("library_rebuild") is None
    finally:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            if work_id is not None:
                work = await db.get(Work, work_id)
                if work is not None:
                    await db.delete(work)
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuild_retry_carries_forward_last_fenced_postgresql_checkpoint():
    """A replacement attempt resumes the last checkpoint owned by its predecessor."""
    from app.database import async_session, engine
    from app.models import TaskEvent, TaskRun
    from app.services import operations

    task_id = None
    checkpoint = {
        "created_at": "2026-08-24T12:00:00+00:00",
        "id": str(uuid4()),
        "options": {
            "mode": "repair",
            "source": None,
            "creator_id": None,
            "work_id": None,
        },
        "stats": {
            "scanned": 500,
            "skipped": 25,
            "metadata_written": 475,
            "thumbnails_generated": 300,
        },
        "errors": 2,
    }
    try:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-rebuild",
                scope_key="library:rebuild:active",
                title="Library rebuild",
                entity="library",
                options={"mode": "repair"},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        with operations.admin_operation_attempt_context(task_id, 1):
            async with async_session() as db:
                await operations.set_current_admin_operation_checkpoint(
                    db,
                    "library_rebuild",
                    checkpoint,
                )
                await db.commit()

        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            task.status = "failed"
            await db.commit()
        retry = await operations.prepare_admin_operation_retry(task_id)
        assert retry.attempt == 2

        with operations.admin_operation_attempt_context(task_id, 2):
            async with async_session() as db:
                resumed = await operations.get_current_admin_operation_checkpoint(
                    db,
                    "library_rebuild",
                )
        assert resumed == checkpoint
    finally:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            await db.commit()
        await engine.dispose()


def test_danbooru_import_invalidates_creator_subscription_caches(monkeypatch):
    from app.services import danbooru_import
    from app.services.creator import CreatorService

    class Result:
        def scalar_one_or_none(self):
            return None

    class FakeDB:
        def __init__(self):
            self.added = []
            self.committed = False

        async def get(self, _model, _id):
            return None

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            for obj in self.added:
                if hasattr(obj, "id") and getattr(obj, "id", None) is None:
                    obj.id = uuid4()

        async def execute(self, _stmt):
            return Result()

        async def commit(self):
            self.committed = True

    calls = []
    async def fake_find_existing_creator(*_args, **_kwargs):
        return None

    async def fake_subscription_defaults(_db):
        return {"sync_interval_hours": 6, "sync_enabled": True, "is_active": True}

    async def fake_creator_projection(_service, _creator_id):
        return None

    monkeypatch.setattr(
        danbooru_import.danbooru_svc,
        "search_and_extract",
        lambda **_kwargs: (
            {
                "id": 123,
                "name": "artist_tag",
                "urls": [{"url": "https://www.pixiv.net/users/123", "normalized_url": "https://www.pixiv.net/users/123", "is_active": True}],
            },
            [{"url": "https://www.pixiv.net/users/123", "link_type": "profile", "source": "pixiv", "confidence": 1.0, "is_verified": True}],
        ),
    )
    monkeypatch.setattr(danbooru_import.danbooru_svc, "is_downloadable_url", lambda _url: True)
    monkeypatch.setattr(danbooru_import.danbooru_svc, "_classify_url", lambda _url: "pixiv")
    monkeypatch.setattr(danbooru_import, "find_existing_creator", fake_find_existing_creator)
    monkeypatch.setattr(danbooru_import, "get_subscription_defaults", fake_subscription_defaults)
    monkeypatch.setattr(CreatorService, "_request_creator_projection", fake_creator_projection)
    monkeypatch.setattr(
        danbooru_import,
        "invalidate_creator_subscription_caches",
        lambda: calls.append("invalidated"),
    )

    result = asyncio.run(danbooru_import.import_all_danbooru_artist({"pixiv_id": "123"}, FakeDB()))

    assert result["status"] == "ok"
    assert result["creator_id"]
    assert result["subscription_id"]
    assert result["links_imported"] == 1
    assert result["sources_created"] == 2
    assert calls == ["invalidated"]


@pytest.mark.asyncio
async def test_reference_mapping_refresh_uses_shared_single_flight_operation(monkeypatch):
    from app.api import reference

    seen = {}

    async def _enqueue(**kwargs):
        seen.update(kwargs)
        return {"status": "enqueued", "job_id": "refresh-job"}

    monkeypatch.setattr(reference, "enqueue_admin_operation", _enqueue)

    response = await reference.refresh_all_danbooru_mappings()

    assert response == {
        "status": "enqueued",
        "job_id": "refresh-job",
        "operation_type": "danbooru-mapping-refresh",
        "message": "Danbooru mapping refresh queued",
    }
    assert seen["lock_key"] == "library:creator-reenrich:active"
    assert seen["operation_type"] == "danbooru-mapping-refresh"
    assert seen["options"] == {"scope": "all"}
    assert seen["queue_name"] == "maintenance"


@pytest.mark.asyncio
async def test_reference_mapping_refresh_status_is_operation_scoped(monkeypatch):
    from fastapi import HTTPException
    from app.api import reference

    expected = {
        "job_id": "refresh-job",
        "status": "running",
        "operation_type": "danbooru-mapping-refresh",
        "progress": {"scanned": 2, "total": 10},
    }
    monkeypatch.setattr(reference, "get_operation_status", lambda _job_id: expected)
    assert await reference.get_danbooru_mapping_refresh("refresh-job") == expected

    monkeypatch.setattr(reference, "get_operation_status", lambda _job_id: {
        "job_id": "other-job",
        "status": "running",
        "operation_type": "admin-clear",
    })
    with pytest.raises(HTTPException) as exc:
        await reference.get_danbooru_mapping_refresh("other-job")
    assert exc.value.status_code == 404
