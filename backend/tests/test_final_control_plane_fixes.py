"""Blocking final-review regressions for the administrator control plane."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import redis as redis_lib
from sqlalchemy import delete, func, select, text


PREFIX = "final_control_plane_"


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["all", "jobs", "subscriptions", "creators"])
async def test_destructive_clear_locks_pipeline_rows_in_global_order(entity):
    """Clear cannot invert the artifact/download/import/TaskRun lock order."""
    from app.services.admin_data import _lock_pipeline_rows_for_clear

    class RecordingSession:
        def __init__(self):
            self.statements: list[str] = []

        async def execute(self, statement):
            self.statements.append(str(statement))

    db = RecordingSession()
    await _lock_pipeline_rows_for_clear(entity, db)  # type: ignore[arg-type]
    locked_tables = [
        table
        for statement in db.statements
        for table in ("storage_artifacts", "download_jobs", "import_jobs", "task_runs")
        if f"FROM {table}" in statement
    ]
    assert locked_tables == [
        "storage_artifacts",
        "download_jobs",
        "import_jobs",
        "task_runs",
    ]


async def _clear_admin_tasks(db) -> None:
    from app.models import TaskEvent, TaskRun

    await db.execute(delete(TaskEvent))
    await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
    await db.commit()


def _stub_transport(monkeypatch) -> None:
    from app.services import operations

    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        operations,
        "_enqueue_admin_rq",
        lambda *_args, rq_job_id, **_kwargs: SimpleNamespace(id=rq_job_id),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_disk_import_uses_taskrun_attempt_when_redis_is_unavailable(
    monkeypatch,
):
    """A DB-current disk import must neither acquire nor heartbeat a Redis fence."""
    from app.api.admin import settings as admin_settings
    from app.database import async_session, engine
    from app.jobs.admin_operations import _run_registered_admin_operation
    from app.models import Creator, TaskRun
    from app.services import disk_import, operations, redis_client

    task_id: UUID | None = None
    creator_name = f"{PREFIX}disk_{uuid4()}"
    _stub_transport(monkeypatch)

    def redis_forbidden():
        raise redis_lib.ConnectionError("redis observability unavailable")

    monkeypatch.setattr(redis_client, "get_redis", redis_forbidden)
    monkeypatch.setattr(admin_settings, "get_redis", redis_forbidden)

    async def reconcile(db, _options, progress_cb, *, publisher_checkpoint, **_kwargs):
        db.add(Creator(name=creator_name, display_name=creator_name))
        await publisher_checkpoint(db)
        await db.commit()
        await progress_cb(
            {
                "scanned": 1,
                "total": 1,
                "existing": 0,
                "imported": 1,
                "skipped": 0,
                "failed": 0,
            }
        )
        return {
            "jobs": 0,
            "scanned": 1,
            "existing": 0,
            "imported": 1,
            "skipped": 0,
            "failed": 0,
        }

    monkeypatch.setattr(disk_import, "reconcile_downloads_to_db", reconcile)
    try:
        async with async_session() as db:
            await _clear_admin_tasks(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-disk-import",
                scope_key="library:disk-import:active",
                title="Disk import",
                entity="disk-import",
                options={"source": "pixiv"},
            )
            task_id = prepared.task.id
            await db.commit()

        result = await _run_registered_admin_operation(str(task_id), 1)

        assert result["imported"] == 1
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            assert task.status == "complete"
            assert await db.scalar(
                select(func.count()).select_from(Creator).where(Creator.name == creator_name)
            ) == 1
    finally:
        async with async_session() as db:
            from app.models import (
                CreatorCurationState,
                CreatorLink,
                SourceCreator,
                Subscription,
                SubscriptionSource,
            )

            creator_ids = select(Creator.id).where(Creator.name == creator_name)
            subscription_ids = select(Subscription.id).where(
                Subscription.creator_id.in_(creator_ids)
            )
            await db.execute(
                delete(SubscriptionSource).where(
                    SubscriptionSource.subscription_id.in_(subscription_ids)
                )
            )
            await db.execute(
                delete(Subscription).where(Subscription.creator_id.in_(creator_ids))
            )
            await db.execute(
                delete(CreatorCurationState).where(
                    CreatorCurationState.creator_id.in_(creator_ids)
                )
            )
            await db.execute(
                delete(CreatorLink).where(CreatorLink.creator_id.in_(creator_ids))
            )
            await db.execute(
                delete(SourceCreator).where(SourceCreator.creator_id.in_(creator_ids))
            )
            await db.execute(delete(Creator).where(Creator.name == creator_name))
            await _clear_admin_tasks(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_scan_does_not_consult_redis_for_registered_disk_import(monkeypatch):
    """Registered admin liveness belongs to TaskRun dispatch recovery, not TTL keys."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations
    from app.services.task_engine import TaskEngine

    redis_reads = 0

    class ForbiddenRedis:
        def pipeline(self, *_args, **_kwargs):
            nonlocal redis_reads
            redis_reads += 1
            raise AssertionError("registered admin stale scan consulted Redis")

    task_id: UUID | None = None
    try:
        async with async_session() as db:
            await _clear_admin_tasks(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-disk-import",
                scope_key="library:disk-import:active",
                title="Disk import",
                entity="disk-import",
                options={},
            )
            task_id = prepared.task.id
            prepared.task.status = "running"
            prepared.task.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
            await db.commit()

            assert await TaskEngine(db).detect_stale_tasks(
                redis_client=ForbiddenRedis(),
                now=datetime.now(timezone.utc),
            ) == 0
            await db.refresh(prepared.task)
            assert prepared.task.status == "running"
        assert redis_reads == 0
    finally:
        async with async_session() as db:
            await _clear_admin_tasks(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_type", "scope_key", "result", "reason_code"),
    [
        (
            "admin-creator-reenrich",
            "library:creator-reenrich:active",
            {
                "scanned": 1,
                "found": 0,
                "not_found": 0,
                "errors": 1,
                "aborted": True,
                "abort_reason": "danbooru_error:Unavailable",
                "items": [],
            },
            "operation_semantic_failure",
        ),
        (
            "admin-search-reindex",
            "library:search-reindex:active",
            {"status": "failed", "message": "index swap failed"},
            "operation_semantic_failure",
        ),
        (
            "asset-dedup-scan",
            "lock:admin:asset-dedup-scan",
            {"status": "superseded", "message": "newer slice owns cursor"},
            "operation_superseded",
        ),
    ],
)
async def test_outer_runner_preserves_semantic_terminal_failure(
    monkeypatch,
    operation_type,
    scope_key,
    result,
    reason_code,
):
    """A normally returned failure/supersession can never become complete."""
    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.models import TaskRun
    from app.services import operations

    _stub_transport(monkeypatch)
    task_id: UUID | None = None

    async def semantic_result(*_args, **_kwargs):
        return result

    monkeypatch.setattr(
        admin_operations,
        "_execute_registered_admin_operation",
        semantic_result,
    )
    try:
        async with async_session() as db:
            await _clear_admin_tasks(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type=operation_type,
                scope_key=scope_key,
                title="Semantic outcome",
                entity="test",
                options={},
            )
            task_id = prepared.task.id
            await db.commit()

        returned = await admin_operations._run_registered_admin_operation(
            str(task_id),
            1,
        )
        assert returned == result
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            assert task.status == "failed"
            assert task.reason_code == reason_code
            assert task.result_data == result
            assert task.error_log
    finally:
        async with async_session() as db:
            await _clear_admin_tasks(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["pixiv", "url"])
async def test_superseded_batch_attempt_commits_zero_domain_mutations(monkeypatch, mode):
    """Both registered batch paths must propagate fencing and roll back their work."""
    from app.database import async_session, engine
    from app.jobs import batch_import
    from app.models import Creator, TaskRun
    from app.services import operations
    from app.services import danbooru as danbooru_svc

    unique = uuid4().hex
    creator_name = f"{PREFIX}batch_{unique}"
    task_id: UUID | None = None
    operation_type = (
        "admin-danbooru-batch-import"
        if mode == "pixiv"
        else "admin-danbooru-url-batch-import"
    )
    scope_key = (
        f"danbooru:batch-import:{unique}"
        if mode == "pixiv"
        else f"danbooru:url-batch-import:{unique}"
    )

    def result_fixture(**_kwargs):
        url = f"https://www.pixiv.net/users/{unique}"
        artist = {
            "id": 987654,
            "name": creator_name,
            "urls": [{"url": url, "normalized_url": url, "is_active": True}],
        }
        links = [
            {
                "url": url,
                "link_type": "profile",
                "source": "pixiv",
                "confidence": 1.0,
                "is_verified": True,
                "notes": None,
            }
        ]
        return artist, links

    monkeypatch.setattr(danbooru_svc, "search_and_extract", result_fixture)
    try:
        async with async_session() as db:
            await _clear_admin_tasks(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type=operation_type,
                scope_key=scope_key,
                title="Batch import",
                entity="creators",
                options={},
                queue_name="imports",
            )
            task_id = prepared.task.id
            await db.commit()

        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            dispatch = dict(task.meta[operations.ADMIN_DISPATCH_META_KEY])
            dispatch["attempt"] = 2
            meta = dict(task.meta)
            meta[operations.ADMIN_DISPATCH_META_KEY] = dispatch
            task.meta = meta
            task.attempts = 2
            task.status = "running"
            await db.commit()

        with operations.admin_operation_attempt_context(task_id, 1):
            with pytest.raises(operations.AdminOperationAttemptRejected):
                if mode == "pixiv":
                    await batch_import._batch_import([unique], str(task_id))
                else:
                    await batch_import._url_batch_import(
                        [f"https://www.pixiv.net/users/{unique}"],
                        str(task_id),
                    )

        async with async_session() as db:
            assert await db.scalar(
                select(func.count()).select_from(Creator).where(Creator.name == creator_name)
            ) == 0
    finally:
        async with async_session() as db:
            from app.models import (
                CreatorCurationState,
                CreatorLink,
                SourceCreator,
                Subscription,
                SubscriptionSource,
            )

            creator_ids = select(Creator.id).where(Creator.name == creator_name)
            subscription_ids = select(Subscription.id).where(
                Subscription.creator_id.in_(creator_ids)
            )
            await db.execute(
                delete(SubscriptionSource).where(
                    SubscriptionSource.subscription_id.in_(subscription_ids)
                )
            )
            await db.execute(
                delete(Subscription).where(Subscription.creator_id.in_(creator_ids))
            )
            await db.execute(
                delete(CreatorCurationState).where(
                    CreatorCurationState.creator_id.in_(creator_ids)
                )
            )
            await db.execute(
                delete(CreatorLink).where(CreatorLink.creator_id.in_(creator_ids))
            )
            await db.execute(
                delete(SourceCreator).where(SourceCreator.creator_id.in_(creator_ids))
            )
            await db.execute(delete(Creator).where(Creator.name == creator_name))
            await _clear_admin_tasks(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_creator_refresh_is_keyset_bounded_fenced_and_resumable(monkeypatch):
    """A large sweep persists one bounded page and resumes aggregate counters."""
    from app.database import async_session, engine
    from app.models import Creator, TaskRun
    from app.services import creator_enrichment, operations
    from app.services.creator import CreatorService

    page_size = 100
    total = page_size * 2 + 37
    run_key = uuid4().hex
    name_prefix = f"{PREFIX}refresh_{run_key}_"
    task_id: UUID | None = None
    rotated = False

    async def refresh(_db, creator):
        creator.description = "mapped-by-current-attempt"
        return {"status": creator_enrichment.STATUS_FOUND, "artist_id": 1}

    async def no_projection(*_args, **_kwargs):
        return None

    monkeypatch.setattr(creator_enrichment, "CREATOR_REFRESH_PAGE_SIZE", page_size, raising=False)
    monkeypatch.setattr(creator_enrichment, "refresh_creator_mapping", refresh)
    monkeypatch.setattr(CreatorService, "_request_creator_projection", no_projection)
    try:
        async with async_session() as db:
            await _clear_admin_tasks(db)
            db.add_all(
                Creator(name=f"{name_prefix}{index:04d}", display_name=f"Creator {index}")
                for index in range(total)
            )
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="danbooru-mapping-refresh",
                scope_key="library:creator-reenrich:active",
                title="Refresh mappings",
                entity="creators",
                options={"scope": "all"},
            )
            task_id = prepared.task.id
            await db.commit()

        async def rotate_after_first_page(progress):
            nonlocal rotated
            if rotated or int(progress.get("scanned", 0)) < page_size:
                return
            rotated = True
            async with async_session() as db:
                task = await db.get(TaskRun, task_id)
                dispatch = dict(task.meta[operations.ADMIN_DISPATCH_META_KEY])
                dispatch["attempt"] = 2
                meta = dict(task.meta)
                meta[operations.ADMIN_DISPATCH_META_KEY] = dispatch
                task.meta = meta
                task.attempts = 2
                await db.commit()

        with operations.admin_operation_attempt_context(task_id, 1):
            async with async_session() as db:
                with pytest.raises(operations.AdminOperationAttemptRejected):
                    await creator_enrichment.refresh_all_creator_mappings(
                        db,
                        progress_cb=rotate_after_first_page,
                    )

        async with async_session() as db:
            first_attempt_count = await db.scalar(
                select(func.count())
                .select_from(Creator)
                .where(
                    Creator.name.startswith(name_prefix),
                    Creator.description == "mapped-by-current-attempt",
                )
            )
            assert first_attempt_count == page_size

        with operations.admin_operation_attempt_context(task_id, 2):
            async with async_session() as db:
                resumed = await creator_enrichment.refresh_all_creator_mappings(db)

        assert resumed["scanned"] == total
        assert resumed["found"] == total
        assert len(resumed["items"]) <= 100
        assert resumed["details_truncated"] is True
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            checkpoint = task.meta[operations.ADMIN_DISPATCH_META_KEY].get(
                "checkpoints", {}
            ).get("creator_mapping_refresh")
            assert checkpoint is None
    finally:
        async with async_session() as db:
            await db.execute(delete(Creator).where(Creator.name.startswith(name_prefix)))
            await _clear_admin_tasks(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backup_schedule_and_legacy_clear_are_postgresql_first(monkeypatch):
    """Schedule writes and the compatibility clear route survive Redis loss."""
    from app.api.admin import data as data_api
    from app.database import async_session, engine
    from app.models import SystemSetting, TaskRun
    from app.services import operations

    _stub_transport(monkeypatch)
    monkeypatch.setattr(
        data_api,
        "get_redis",
        lambda: (_ for _ in ()).throw(redis_lib.ConnectionError("redis down")),
    )

    async def forbidden_clear(*_args, **_kwargs):
        raise AssertionError("legacy clear route performed synchronous destruction")

    monkeypatch.setattr(
        "app.services.admin_data.clear_entity_data",
        forbidden_clear,
    )
    try:
        async with async_session() as db:
            await _clear_admin_tasks(db)
            await db.execute(delete(SystemSetting).where(SystemSetting.key == "backup_schedule"))
            await db.commit()

            schedule = await data_api.schedule_backup(
                data_api.BackupScheduleRequest(enabled=True, interval_hours=7),
                db,
            )
            assert schedule["status"] == "ok"

            accepted = await data_api.clear_entity(
                "tags",
                data_api.ClearOperationRequest(entity="tags", confirmation="DELETE-TAGS"),
                db,
            )
            assert accepted["status"] == "enqueued"
            assert accepted["operation_type"] == "admin-clear"

        async with async_session() as db:
            setting = await db.get(SystemSetting, "backup_schedule")
            assert setting.value["enabled"] is True
            assert setting.value["interval_hours"] == 7
            task = await db.get(TaskRun, UUID(accepted["task_id"]))
            assert task.status == "enqueued"
            assert task.meta[operations.ADMIN_DISPATCH_META_KEY]["publication_state"] in {
                "pending",
                "published",
            }
    finally:
        async with async_session() as db:
            await db.execute(delete(SystemSetting).where(SystemSetting.key == "backup_schedule"))
            await _clear_admin_tasks(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_due_backup_occurrence_commits_taskrun_before_redis_publication(monkeypatch):
    """Every scheduled occurrence is recoverable from PostgreSQL after Redis loss."""
    from app.database import async_session, engine
    from app.models import SystemSetting, TaskRun
    from app.services import backup_schedule, operations

    class UnavailableRedis:
        def __getattr__(self, _name):
            raise redis_lib.ConnectionError("redis unavailable")

    try:
        async with async_session() as db:
            await _clear_admin_tasks(db)
            await db.execute(delete(SystemSetting).where(SystemSetting.key == "backup_schedule"))
            db.add(
                SystemSetting(
                    key="backup_schedule",
                    value={
                        "enabled": True,
                        "interval_hours": 3,
                        "next_run_at": (
                            datetime.now(timezone.utc) - timedelta(minutes=1)
                        ).isoformat(),
                    },
                )
            )
            await db.commit()

        outcome = await backup_schedule.dispatch_due_backup(
            now=datetime.now(timezone.utc),
            redis_client=UnavailableRedis(),
        )
        assert outcome["created"] is True
        async with async_session() as db:
            task = await db.get(TaskRun, UUID(outcome["task_id"]))
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert task.operation_type == "admin-backup-create"
            assert task.status == "enqueued"
            assert dispatch["publication_state"] == operations.ADMIN_DISPATCH_PENDING
            setting = await db.get(SystemSetting, "backup_schedule")
            assert datetime.fromisoformat(setting.value["next_run_at"]) > datetime.now(
                timezone.utc
            )
    finally:
        async with async_session() as db:
            await db.execute(delete(SystemSetting).where(SystemSetting.key == "backup_schedule"))
            await _clear_admin_tasks(db)
        await engine.dispose()
