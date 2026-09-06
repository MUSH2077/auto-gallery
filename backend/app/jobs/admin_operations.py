"""RQ jobs for long-running admin operations."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Iterable
from contextlib import suppress
from uuid import UUID

from app.database import async_session
from app.services.admin_data import clear_entity_data
from app.services.operations import (
    OPERATION_TTL_SECONDS,
    acquire_operation_lock,
    current_admin_operation_attempt,
    release_legacy_operation_lock,
    set_operation_status,
    update_current_admin_operation_progress,
)
from app.services.heavy_io import run_heavy_io_operation
from app.services.redis_pubsub import PublisherFenceError

logger = logging.getLogger(__name__)


async def _await_admin_file_finalizer(finalizer):
    """Delay outer cancellation until a fenced file/TaskRun commit settles."""

    finalizer_task = asyncio.create_task(finalizer)
    try:
        return await asyncio.shield(finalizer_task)
    except asyncio.CancelledError:
        try:
            await finalizer_task
        except Exception:
            logger.exception("Fenced administrator file finalizer failed during cancellation")
        raise


async def _finalize_registered_file_result(
    task_id: str,
    *,
    publish,
    after_commit=None,
) -> dict:
    """Publish a file result and terminal TaskRun under one exact-attempt lock."""

    from app.services.operations import fence_current_admin_operation_transaction
    from app.services.tasks import TaskService

    async def finalize() -> dict:
        async with async_session() as publish_db:
            task = await fence_current_admin_operation_transaction(
                publish_db,
                task_id=task_id,
            )
            result = publish()
            await TaskService(publish_db).update_task(
                task,
                status="complete",
                progress={
                    "phase": "complete",
                    "label": str(result.get("message") or "Complete"),
                },
                result=result,
                error=None,
                reason_code=None,
            )
            await publish_db.commit()
        if after_commit is not None:
            try:
                after_commit(result)
            except Exception:
                logger.warning(
                    "Unable to prune files after durable administrator completion",
                    exc_info=True,
                )
        return {**result, "_admin_handoff": True}

    return await _await_admin_file_finalizer(finalize())


class _DiskImportPublisherGuard:
    """Redis race arbiter plus durable TaskRun checkpoint for one publisher."""

    def __init__(self, job_id: str, attempt_token: str | None = None):
        from app.services.redis_client import get_redis

        self.job_id = job_id
        self.task_id = UUID(job_id)
        self.attempt_token = attempt_token
        self.redis = get_redis()
        self._lost = threading.Event()
        self._lost_reason: str | None = None
        self._recovery_won = False

    def _lose(
        self,
        reason: str,
        *,
        recovery_won: bool,
    ) -> PublisherFenceError:
        self._lost_reason = self._lost_reason or reason
        self._recovery_won = self._recovery_won or recovery_won
        self._lost.set()
        return PublisherFenceError(
            self._lost_reason,
            recovery_won=self._recovery_won,
        )

    @property
    def authority_lost(self) -> bool:
        return self._lost.is_set()

    def publish_heartbeat(self) -> bool:
        """Publish only if recovery has not atomically won the Redis fence."""

        from app.services.redis_pubsub import TaskEventPublisher

        if self._lost.is_set():
            raise PublisherFenceError(
                self._lost_reason or "disk import publisher fence was lost",
                recovery_won=self._recovery_won,
            )
        try:
            if not self.attempt_token:
                raise RuntimeError("publisher attempt was not initialized")
            published = TaskEventPublisher.publish_heartbeat(
                self.job_id,
                "admin",
                pid=os.getpid(),
                fence_aware=True,
                attempt_token=self.attempt_token,
                redis_client=self.redis,
            )
        except Exception as exc:
            raise self._lose(
                "disk import publisher fence is unavailable; stopping fail-closed",
                recovery_won=False,
            ) from exc
        if not published:
            raise self._lose(
                "disk import publisher fence was won by recovery",
                recovery_won=True,
            )
        return True

    async def initialize_attempt(self) -> str:
        """Capture/mint the immutable attempt before the first Redis action."""

        from app.services.publisher_attempts import (
            current_publisher_attempt,
            ensure_publisher_attempt,
            lock_publisher_task,
        )

        async with async_session() as db:
            task = await lock_publisher_task(db, self.task_id)
            if task is None:
                raise self._lose(
                    "disk import publisher has no durable TaskRun",
                    recovery_won=True,
                )
            durable = current_publisher_attempt(task)
            if self.attempt_token is None and durable is not None:
                raise self._lose(
                    "disk import publisher fence rejected a legacy delivery "
                    "without its captured attempt",
                    recovery_won=True,
                )
            if (
                self.attempt_token is not None
                and durable is not None
                and durable != self.attempt_token
            ):
                raise self._lose(
                    "disk import publisher attempt is no longer current",
                    recovery_won=True,
                )
            durable, _minted = ensure_publisher_attempt(
                task,
                captured_attempt=self.attempt_token,
            )
            self.attempt_token = durable
            await db.commit()
        return durable

    async def checkpoint(
        self,
        db,
        *,
        allowed_statuses: Iterable[str] = ("running",),
        lock_task: bool = False,
    ):
        """Reject a stale attempt, then prove its attempt-scoped Redis lease."""

        from sqlalchemy import select

        from app.models.task_run import TaskRun
        from app.services.publisher_attempts import (
            current_publisher_attempt,
            ensure_publisher_attempt,
            lock_publisher_task,
        )

        if lock_task:
            task = await lock_publisher_task(db, self.task_id)
        else:
            with db.no_autoflush:
                task = (
                    await db.execute(
                        select(TaskRun)
                        .where(
                            TaskRun.id == self.task_id,
                            TaskRun.kind == "admin",
                            TaskRun.operation_type == "admin-disk-import",
                        )
                        .execution_options(populate_existing=True),
                    )
                ).scalar_one_or_none()
        allowed = frozenset(allowed_statuses)
        if task is None:
            raise self._lose(
                "disk import publisher fence has no durable TaskRun",
                recovery_won=True,
            )
        durable_attempt = current_publisher_attempt(task)
        if self.attempt_token is None and durable_attempt is not None:
            raise self._lose(
                "disk import publisher fence rejected a legacy delivery "
                "without its captured attempt",
                recovery_won=True,
            )
        if durable_attempt is None:
            if not lock_task:
                raise self._lose(
                    "disk import publisher attempt is not initialized",
                    recovery_won=True,
                )
            durable_attempt, _minted = ensure_publisher_attempt(
                task,
                captured_attempt=self.attempt_token,
            )
        if self.attempt_token is None:
            self.attempt_token = durable_attempt
        elif durable_attempt != self.attempt_token:
            raise self._lose(
                "disk import publisher attempt is no longer current",
                recovery_won=True,
            )
        if task.status not in allowed:
            raise self._lose(
                "disk import publisher fence rejected durable status "
                f"'{task.status}'",
                recovery_won=True,
            )
        await asyncio.to_thread(self.publish_heartbeat)
        return task


def _disk_import_publication_count(result: dict) -> int:
    import_job_ids = result.get("import_job_ids")
    if isinstance(import_job_ids, (list, tuple, set)):
        return len(import_job_ids)
    return int(result.get("imported", result.get("jobs", 0)) or 0)


def disk_import_completion_progress(result: dict) -> dict:
    """Keep the resumable drain counters visible after terminal transition."""

    return {
        "phase": "complete",
        "label": f"Queued {_disk_import_publication_count(result)} import jobs",
        **{
            key: result.get(key, 0)
            for key in ("scanned", "existing", "imported", "skipped", "failed")
        },
    }


def _registered_terminal_outcome(
    operation_type: str,
    result: dict,
) -> tuple[str, str | None, str | None]:
    """Interpret semantic handler outcomes at the sole terminal writer."""

    if operation_type in {"admin-creator-reenrich", "danbooru-mapping-refresh"} and result.get(
        "aborted"
    ):
        reason = str(result.get("abort_reason") or "creator mapping refresh aborted")
        return "failed", reason, "operation_semantic_failure"
    if (
        operation_type == "admin-search-reindex"
        and "status" in result
        and result.get("status") != "ok"
    ):
        reason = str(result.get("message") or "search reindex failed")
        return "failed", reason, "operation_semantic_failure"
    if operation_type == "asset-dedup-scan" and result.get("status") in {
        "failed",
        "superseded",
    }:
        status = str(result.get("status"))
        reason = str(result.get("message") or f"asset dedup scan {status}")
        return (
            "failed",
            reason,
            "operation_superseded" if status == "superseded" else "operation_semantic_failure",
        )
    return "complete", None, None


def run_registered_admin_operation(task_id: str, attempt: int) -> dict:
    """Single RQ entrypoint: PostgreSQL supplies every validated argument."""

    return asyncio.run(_run_registered_admin_operation(task_id, int(attempt)))


async def _heartbeat_registered_admin_operation(
    task_id: str,
    attempt: int,
    stop: asyncio.Event,
    lease_db=None,
) -> str | None:
    from app.services.operations import (
        ADMIN_ATTEMPT_HEARTBEAT_INTERVAL_SECONDS,
        heartbeat_admin_operation,
    )

    while True:
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=ADMIN_ATTEMPT_HEARTBEAT_INTERVAL_SECONDS,
            )
            return
        except TimeoutError:
            try:
                current = await heartbeat_admin_operation(
                    task_id,
                    attempt,
                    db=lease_db,
                )
            except Exception as exc:
                logger.warning(
                    "Administrator execution lease heartbeat failed task=%s attempt=%s",
                    task_id,
                    attempt,
                    exc_info=True,
                )
                return f"PostgreSQL execution lease heartbeat failed: {type(exc).__name__}"
            if not current:
                return "PostgreSQL execution lease was lost"


async def _run_registered_admin_operation(task_id: str, attempt: int) -> dict:
    from app.services.operations import (
        AdminOperationAttemptRejected,
        admin_operation_execution_lease,
        admin_operation_attempt_context,
        claim_admin_operation,
        update_admin_task,
    )

    async with admin_operation_execution_lease(task_id) as lease_db:
        operation_type, options = await claim_admin_operation(task_id, attempt)
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            _heartbeat_registered_admin_operation(
                task_id,
                attempt,
                heartbeat_stop,
                lease_db,
            )
        )

        async def execute() -> dict:
            with admin_operation_attempt_context(task_id, attempt):
                return await _execute_registered_admin_operation(
                    operation_type,
                    task_id,
                    attempt,
                    options,
                )

        operation_task = asyncio.create_task(execute())
        try:
            completed, _pending = await asyncio.wait(
                {operation_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in completed:
                lost_reason = heartbeat_task.result()
                if lost_reason:
                    operation_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await operation_task
                    raise AdminOperationAttemptRejected(lost_reason)
            result = await operation_task
            if result.get("_admin_handoff"):
                return result
            terminal_status, terminal_error, reason_code = _registered_terminal_outcome(
                operation_type,
                result,
            )
            if not await update_admin_task(
                task_id,
                attempt,
                allowed_current_statuses=("enqueued", "running", "paused", "recovering"),
                status=terminal_status,
                progress={
                    "phase": terminal_status,
                    "label": str(
                        result.get("message")
                        or terminal_error
                        or (
                            "Complete"
                            if terminal_status == "complete"
                            else "Operation failed"
                        )
                    ),
                },
                result=result,
                error=terminal_error,
                reason_code=reason_code,
            ):
                raise RuntimeError("Administrator operation attempt is no longer current")
            return result
        except Exception as exc:
            await update_admin_task(
                task_id,
                attempt,
                allowed_current_statuses=("enqueued", "running", "paused", "recovering"),
                status="failed",
                progress={"phase": "failed", "label": "Operation failed"},
                error=str(exc),
            )
            raise
        finally:
            heartbeat_stop.set()
            if not heartbeat_task.done():
                await heartbeat_task
            if not operation_task.done():
                operation_task.cancel()
                with suppress(asyncio.CancelledError):
                    await operation_task


async def _execute_registered_admin_operation(
    operation_type: str,
    task_id: str,
    attempt: int,
    options: dict,
) -> dict:
    """Dispatch a registered business handler after its durable claim."""

    if operation_type == "admin-clear":
        entity = str(options.get("entity") or "")
        return await _run_clear_operation(entity, task_id)
    if operation_type == "admin-cleanup-metadata-jsons":
        from app.config import settings
        from app.jobs.import_runner import cleanup_metadata_jsons

        removed = await cleanup_metadata_jsons(settings.download_root)
        return {"removed": removed, "message": f"Removed {removed} metadata files"}
    if operation_type == "admin-rebuild":
        return await _run_library_rebuild_operation(task_id, options)
    if operation_type == "admin-disk-import":
        return await _run_registered_disk_import_operation(
            task_id,
            options,
        )
    if operation_type in {"admin-creator-reenrich", "danbooru-mapping-refresh"}:
        return await _run_creator_reenrich_operation(task_id, options)
    if operation_type == "admin-creator-alias-backfill":
        return await _run_creator_alias_backfill_operation(task_id, options)
    if operation_type == "danbooru-import-all":
        from app.services.danbooru_import import import_all_danbooru_artist

        async with async_session() as db:
            return await import_all_danbooru_artist(options, db)
    if operation_type == "admin-search-reindex":
        return await _run_search_reindex_operation(task_id, options)
    if operation_type == "admin-curation-backfill":
        return await _run_curation_backfill_operation(task_id, options)
    if operation_type == "admin-gitllery-verify":
        return await _run_gitllery_verify_operation(task_id, options)
    if operation_type == "admin-gitllery-sync":
        return await _run_gitllery_sync_operation(task_id, options)
    if operation_type == "hierarchy-delete":
        return await _run_hierarchy_delete_operation(task_id, options)
    if operation_type == "asset-dedup-scan":
        from app.jobs.asset_dedup import run_registered_asset_dedup_scan

        return await run_registered_asset_dedup_scan(task_id, attempt, options)
    if operation_type == "admin-danbooru-batch-import":
        from app.jobs.batch_import import _batch_import

        return await _batch_import(list(options.get("pixiv_ids") or []), task_id)
    if operation_type == "admin-danbooru-url-batch-import":
        from app.jobs.batch_import import _url_batch_import

        return await _url_batch_import(list(options.get("urls") or []), task_id)
    if operation_type == "admin-download-conflict-reconciliation":
        from app.jobs.download_conflicts import (
            reconcile_historical_download_conflicts_unlocked,
        )

        return await reconcile_historical_download_conflicts_unlocked(
            int(options.get("limit") or 500)
        )
    if operation_type == "admin-integrity-scan":
        from app.api.admin.settings import _run_integrity_check

        await update_current_admin_operation_progress(
            task_id,
            {"phase": "scanning", "label": "Scanning data integrity"},
        )
        async with async_session() as db:
            result = await _run_integrity_check(db)
        return {**result, "message": "Integrity scan complete"}
    if operation_type == "admin-backup-estimate":
        from app.api.admin.backup import _estimate_component_sizes

        await update_current_admin_operation_progress(
            task_id,
            {"phase": "estimating", "label": "Estimating backup size"},
        )
        sizes = await asyncio.to_thread(_estimate_component_sizes)
        return {
            "components": {
                key: round(value / 1024, 1)
                for key, value in sizes.items()
            },
            "message": "Backup estimate complete",
        }
    if operation_type == "admin-backup-create":
        from app.api.admin.backup import (
            _create_backup_sync,
            prune_published_backup,
            publish_backup_candidate,
        )

        await update_current_admin_operation_progress(
            task_id,
            {"phase": "creating", "label": "Creating backup archive"},
        )
        staged = await asyncio.to_thread(
            _create_backup_sync,
            options,
            publish=False,
            candidate_token=f"{task_id}-attempt-{attempt}",
        )
        return await _finalize_registered_file_result(
            task_id,
            publish=lambda: {
                **publish_backup_candidate(staged, prune=False),
                "message": "Backup created",
            },
            after_commit=lambda result: prune_published_backup(result["filename"]),
        )
    if operation_type == "admin-restore-validate":
        from app.services.offline_restore import (
            publish_validated_upload,
            staging_root,
            validate_upload,
        )

        upload_id = str(options.get("upload_id") or "")
        await update_current_admin_operation_progress(
            task_id,
            {"phase": "validating", "label": "Validating restore archive"},
        )
        staged = await asyncio.to_thread(
            validate_upload,
            root=staging_root(),
            upload_id=upload_id,
            task_id=task_id,
            attempt=attempt,
            publish_ready=False,
        )
        return await _finalize_registered_file_result(
            task_id,
            publish=lambda: publish_validated_upload(
                root=staging_root(),
                upload_id=upload_id,
                task_id=task_id,
                attempt=attempt,
                staged=staged,
            ),
        )
    if operation_type == "admin-proxy-test":
        from app.api.admin.settings import _run_proxy_connectivity_test

        await update_current_admin_operation_progress(
            task_id,
            {"phase": "testing", "label": "Testing proxy connectivity"},
        )
        async with async_session() as db:
            result = await _run_proxy_connectivity_test(db)
        return {**result, "message": "Proxy connectivity test complete"}
    if operation_type == "admin-gallerydl-connectivity-test":
        from app.api.admin.gallerydl import _run_source_connection_test

        source = str(options.get("source") or "")
        await update_current_admin_operation_progress(
            task_id,
            {"phase": "testing", "label": f"Testing {source} connectivity"},
        )
        return await _run_source_connection_test(source)
    raise ValueError(f"Unsupported registered administrator operation: {operation_type}")


def run_clear_operation(entity: str, job_id: str) -> dict:
    """Entry point for RQ workers."""
    return asyncio.run(run_heavy_io_operation(
        "operation:clear", job_id, lambda: _run_clear_operation(entity, job_id)))


def run_cleanup_metadata_jsons_operation(
    job_id: str,
    options: dict | None = None,
) -> dict:
    """Rolling-upgrade bridge; new deliveries use the registered entrypoint."""

    del job_id, options
    from app.config import settings
    from app.jobs.import_runner import cleanup_metadata_jsons

    removed = asyncio.run(cleanup_metadata_jsons(settings.download_root))
    return {"removed": removed, "message": f"Removed {removed} metadata files"}


async def _run_clear_operation(entity: str, job_id: str) -> dict:
    from uuid import UUID
    from app.services.tasks import TaskService
    registered = current_admin_operation_attempt() is not None
    running_progress = {"phase": "running", "label": f"Clearing {entity}"}
    if registered:
        await update_current_admin_operation_progress(job_id, running_progress)
    else:
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="running",
                    progress=running_progress,
                )
                await task_db.commit()
        set_operation_status(
            job_id,
            "running",
            "admin-clear",
            progress=running_progress,
            meta={"entity": entity},
        )
    try:
        async with async_session() as db:
            result = await clear_entity_data(entity, db)
        if not registered:
            complete_progress = {
                "phase": "complete",
                "label": result.get("message", "Complete"),
            }
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(
                        task,
                        status="complete",
                        progress=complete_progress,
                        result=result,
                    )
                    await task_db.commit()
            set_operation_status(
                job_id,
                "complete",
                "admin-clear",
                progress=complete_progress,
                result=result,
                meta={"entity": entity},
            )
        return result
    except Exception as exc:
        logger.exception("Admin clear operation failed: job_id=%s entity=%s", job_id, entity)
        if not registered:
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(
                        task,
                        status="failed",
                        progress={"phase": "failed"},
                        error=str(exc),
                    )
                    await task_db.commit()
            set_operation_status(
                job_id,
                "failed",
                "admin-clear",
                progress={"phase": "failed"},
                error=str(exc),
                meta={"entity": entity},
            )
        raise


def run_library_rebuild_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — rebuild /library/ from DB."""
    return asyncio.run(run_heavy_io_operation(
        "operation:library-rebuild", job_id,
        lambda: _run_library_rebuild_operation(job_id, options or {})))


async def _run_library_rebuild_operation(job_id: str, options: dict) -> dict:
    from app.services.admin_data import rebuild_library_index
    from uuid import UUID
    from app.services.tasks import TaskService
    registered = current_admin_operation_attempt() is not None
    running_progress = {
        "phase": "running",
        "label": "Rebuilding library index...",
    }
    if registered:
        await update_current_admin_operation_progress(job_id, running_progress)
    else:
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="running",
                    progress=running_progress,
                )
                await task_db.commit()
        set_operation_status(
            job_id,
            "running",
            "admin-rebuild",
            progress=running_progress,
            meta={"entity": "library", **options},
        )
    try:
        async def update_progress(progress: dict):
            task_progress = {
                **progress,
                "label": f"Scanned {progress['scanned']} of {progress['total']}",
            }
            if current_admin_operation_attempt() is not None:
                await update_current_admin_operation_progress(
                    job_id,
                    task_progress,
                )
            else:
                set_operation_status(
                    job_id,
                    "running",
                    "admin-rebuild",
                    progress=task_progress,
                    meta={"entity": "library", **options},
                )

        async with async_session() as db:
            result = await rebuild_library_index(db, options, update_progress)
        if not registered:
            complete_progress = {
                "phase": "complete",
                "label": result.get("message", "Complete"),
            }
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(
                        task,
                        status="complete",
                        progress=complete_progress,
                        result=result,
                    )
                    await task_db.commit()
            set_operation_status(
                job_id,
                "complete",
                "admin-rebuild",
                progress=complete_progress,
                result=result,
                meta={"entity": "library", **options},
            )
        return result
    except Exception as exc:
        logger.exception("Library rebuild failed: job_id=%s", job_id)
        if not registered:
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(
                        task,
                        status="failed",
                        progress={"phase": "failed"},
                        error=str(exc),
                    )
                    await task_db.commit()
            set_operation_status(
                job_id,
                "failed",
                "admin-rebuild",
                progress={"phase": "failed"},
                error=str(exc),
                meta={"entity": "library", **options},
            )
        raise
    finally:
        if not registered:
            release_legacy_operation_lock("library:rebuild:active", job_id)


def run_disk_import_operation(
    job_id: str,
    options: dict | None = None,
    attempt_token: str | None = None,
) -> dict:
    """Entry point for RQ workers — import on-disk download files into the DB."""

    async def run_authorized() -> dict:
        guard = _DiskImportPublisherGuard(job_id, attempt_token)
        await guard.initialize_attempt()
        async with async_session() as startup_db:
            await guard.checkpoint(
                startup_db,
                allowed_statuses=("enqueued", "running"),
                lock_task=True,
            )
            await startup_db.rollback()
        from app.services.operations import current_operation_attempt

        expected_operational_attempt = current_operation_attempt(
            guard.redis,
            job_id,
        )
        if expected_operational_attempt not in {
            None,
            guard.attempt_token,
        }:
            raise guard._lose(
                "disk import operational attempt is no longer current",
                recovery_won=True,
            )
        if not acquire_operation_lock(
            guard.redis,
            "library:disk-import:active",
            job_id,
            ttl_seconds=OPERATION_TTL_SECONDS,
            publisher_attempt=guard.attempt_token,
            replace_same_job=True,
            expected_current_attempt=expected_operational_attempt,
        ):
            raise guard._lose(
                "disk import operational owner is no longer current",
                recovery_won=True,
            )
        return await run_heavy_io_operation(
            "operation:disk-import",
            job_id,
            lambda: _run_disk_import_operation(
                job_id,
                options or {},
                guard.attempt_token,
                publisher_guard=guard,
            ),
            publisher_attempt=guard.attempt_token,
        )

    return asyncio.run(run_authorized())


async def _run_registered_disk_import_operation(
    job_id: str,
    options: dict,
) -> dict:
    """Run a dispatch-backed disk import with PostgreSQL as sole authority."""

    from app.api.admin.settings import invalidate_storage_breakdown_cache
    from app.services.disk_import import reconcile_downloads_to_db
    from app.services.operations import fence_current_admin_operation_transaction

    async def checkpoint(
        db,
        *,
        allowed_statuses: Iterable[str] = ("running",),
        lock_task: bool = False,
    ):
        del lock_task
        return await fence_current_admin_operation_transaction(
            db,
            task_id=job_id,
            allowed_statuses=allowed_statuses,
        )

    await update_current_admin_operation_progress(
        job_id,
        {"phase": "running", "label": "Scanning download root..."},
    )

    async def update_progress(progress: dict) -> None:
        await update_current_admin_operation_progress(
            job_id,
            {
                **progress,
                "label": (
                    f"Imported {progress.get('imported', 0)}; "
                    f"scanned {progress.get('scanned', 0)} of {progress.get('total', 0)}"
                ),
            },
        )

    async with async_session() as db:
        result = await reconcile_downloads_to_db(
            db,
            {**options, "parent_task_id": job_id},
            update_progress,
            publisher_checkpoint=checkpoint,
            # Registered attempts never install a Redis publisher token. Redis
            # remains transport/observability for child import publication.
            publisher_attempt=None,
        )
    invalidate_storage_breakdown_cache()
    return {
        **result,
        "message": result.get("message")
        or f"Queued {_disk_import_publication_count(result)} import jobs",
    }


async def _run_disk_import_operation(
    job_id: str,
    options: dict,
    attempt_token: str | None = None,
    *,
    publisher_guard: _DiskImportPublisherGuard | None = None,
) -> dict:
    from app.jobs.worker_control import HeartbeatPublisher
    from app.services.disk_import import reconcile_downloads_to_db
    from app.services.tasks import TaskService

    guard = publisher_guard or _DiskImportPublisherGuard(job_id, attempt_token)
    heartbeat = HeartbeatPublisher(
        job_id,
        "admin",
        heartbeat_callback=guard.publish_heartbeat,
    )
    try:
        await guard.initialize_attempt()
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await guard.checkpoint(
                task_db,
                allowed_statuses=("enqueued", "running"),
                lock_task=True,
            )
            await svc.update_task(
                task,
                status="running",
                progress={
                    "phase": "running",
                    "label": "Scanning download root...",
                },
            )
            await task_db.commit()
        set_operation_status(
            job_id,
            "running",
            "admin-disk-import",
            progress={
                "phase": "running",
                "label": "Scanning download root...",
            },
            meta={"entity": "disk-import", **options},
            publisher_attempt=guard.attempt_token,
            redis_client=guard.redis,
        )
        heartbeat.start()

        async def update_progress(progress: dict):
            task_progress = {
                **progress,
                "label": (
                    f"Imported {progress.get('imported', 0)}; "
                    f"scanned {progress.get('scanned', 0)} of {progress.get('total', 0)}"
                ),
            }
            # The operations cache is transient; the TaskRun is the durable UI
            # projection and must retain the same resumable counters.
            async with async_session() as progress_db:
                progress_task = await guard.checkpoint(
                    progress_db,
                    lock_task=True,
                )
                await TaskService(progress_db).update_task(
                    progress_task,
                    status="running",
                    progress=task_progress,
                )
                await progress_db.commit()
            set_operation_status(
                job_id,
                "running",
                "admin-disk-import",
                progress=task_progress,
                meta={"entity": "disk-import", **options},
                publisher_attempt=guard.attempt_token,
                redis_client=guard.redis,
            )

        async with async_session() as db:
            result = await reconcile_downloads_to_db(
                db,
                {**options, "parent_task_id": job_id},
                update_progress,
                publisher_checkpoint=guard.checkpoint,
                publisher_attempt=guard.attempt_token,
            )
        from app.api.admin.settings import invalidate_storage_breakdown_cache
        invalidate_storage_breakdown_cache()
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await guard.checkpoint(task_db, lock_task=True)
            completion_progress = disk_import_completion_progress(result)
            await svc.update_task(
                task,
                status="complete",
                progress=completion_progress,
                result=result,
            )
            await task_db.commit()
        set_operation_status(
            job_id,
            "complete",
            "admin-disk-import",
            progress=disk_import_completion_progress(result),
            result=result,
            meta={"entity": "disk-import", **options},
            publisher_attempt=guard.attempt_token,
            redis_client=guard.redis,
        )
        return result
    except PublisherFenceError as exc:
        logger.warning("Disk import publisher stopped: job_id=%s error=%s", job_id, exc)
        # A fence/mismatch is itself proof that this worker cannot authorize a
        # durable or cache mutation. Recovery owns the stale transition; Redis
        # uncertainty remains fail-closed until a later authoritative scan.
        raise
    except Exception as exc:
        from app.services.publisher_attempts import redact_publisher_attempt

        safe_error = redact_publisher_attempt(exc, guard.attempt_token)
        logger.error(
            "Disk import failed: job_id=%s error=%s",
            job_id,
            safe_error,
        )
        failed_current_attempt = False
        async with async_session() as task_db:
            svc = TaskService(task_db)
            try:
                task = await guard.checkpoint(
                    task_db,
                    allowed_statuses=("enqueued", "running", "recovering"),
                    lock_task=True,
                )
            except PublisherFenceError:
                await task_db.rollback()
            else:
                await svc.update_task(
                    task,
                    status="failed",
                    progress={"phase": "failed"},
                    error=safe_error,
                )
                await task_db.commit()
                failed_current_attempt = True
        if failed_current_attempt:
            set_operation_status(
                job_id,
                "failed",
                "admin-disk-import",
                progress={"phase": "failed"},
                error=safe_error,
                meta={"entity": "disk-import", **options},
                publisher_attempt=guard.attempt_token,
                redis_client=guard.redis,
            )
        if safe_error != str(exc):
            raise RuntimeError(safe_error) from None
        raise
    finally:
        heartbeat.stop()
        if not guard.authority_lost:
            release_legacy_operation_lock(
                "library:disk-import:active",
                job_id,
                publisher_attempt=guard.attempt_token,
            )


def run_gitllery_rebuild_operation(job_id: str, options: dict | None = None) -> dict:
    """Rolling-upgrade safety: never restore disk directly into public DB."""

    raise RuntimeError(
        "legacy Gitllery rebuild is retired; use v1 staged restore"
    )


def run_creator_reenrich_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — refresh Danbooru creator mappings."""
    return asyncio.run(run_heavy_io_operation(
        "operation:creator-reenrich", job_id,
        lambda: _run_creator_reenrich_operation(job_id, options or {})))


async def _run_creator_reenrich_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID
    from app.services.creator_enrichment import (
        reenrich_pending,
        refresh_all_creator_mappings,
    )
    from app.services.tasks import TaskService

    refresh_all = options.get("scope") == "all"
    operation_type = (
        "danbooru-mapping-refresh"
        if refresh_all
        else "admin-creator-reenrich"
    )
    entity = "creators" if refresh_all else "creator-reenrich"
    running_label = (
        "Refreshing all Danbooru mappings..."
        if refresh_all
        else "Searching Danbooru..."
    )

    if current_admin_operation_attempt() is None:
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="running",
                    progress={"phase": "running", "label": running_label},
                )
                await task_db.commit()
    set_operation_status(job_id, "running", operation_type,
        progress={"phase": "running", "label": running_label},
        meta={"entity": entity, **options})
    try:
        async def update_progress(progress: dict):
            task_progress = {
                **progress,
                "label": (
                    f"Mapped {progress.get('found', 0)} of "
                    f"{progress.get('scanned', 0)} scanned"
                ),
            }
            if current_admin_operation_attempt() is not None:
                await update_current_admin_operation_progress(
                    job_id,
                    task_progress,
                )
            else:
                set_operation_status(
                    job_id,
                    "running",
                    operation_type,
                    progress=task_progress,
                    meta={"entity": entity, **options},
                )

        async with async_session() as db:
            if refresh_all:
                result = await refresh_all_creator_mappings(
                    db,
                    progress_cb=update_progress,
                )
            else:
                result = await reenrich_pending(db, progress_cb=update_progress)

        label = f"Mapped {result['found']} creators ({result['not_found']} not on Danbooru)"
        terminal_status = "complete"
        terminal_error = None
        if result.get("aborted"):
            label += " — aborted: Danbooru unreachable"
            terminal_status = "failed"
            terminal_error = "Danbooru became unavailable; partial results were retained"
        if current_admin_operation_attempt() is None:
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(
                        task,
                        status=terminal_status,
                        progress={"phase": terminal_status, "label": label},
                        result=result,
                        error=terminal_error,
                    )
                    await task_db.commit()
        set_operation_status(job_id, terminal_status, operation_type,
            progress={"phase": terminal_status, "label": label},
            result=result, error=terminal_error,
            meta={"entity": entity, **options})
        if result.get("found"):
            from app.services.cache import invalidate_api_caches, invalidate_creator_subscription_caches
            invalidate_api_caches("creators")
            invalidate_creator_subscription_caches()
        return result
    except Exception as exc:
        logger.exception("Creator re-enrichment failed: job_id=%s", job_id)
        if current_admin_operation_attempt() is None:
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(task, status="failed", progress={"phase": "failed"}, error=str(exc))
                    await task_db.commit()
        set_operation_status(job_id, "failed", operation_type,
            progress={"phase": "failed"}, error=str(exc), meta={"entity": entity, **options})
        raise
    finally:
        release_legacy_operation_lock(
            "library:creator-reenrich:active",
            job_id,
        )


def run_gitllery_sync_operation(job_id: str, options: dict | None = None) -> dict:
    """Rolling-upgrade bridge to one bounded v1 segment projection slice."""

    from app.jobs.gitllery_projection import run_gitllery_projection_outbox
    try:
        return run_gitllery_projection_outbox(limit=25, max_seconds=20.0)
    finally:
        release_legacy_operation_lock(
            "library:gitllery-sync:active",
            job_id,
        )


def run_gitllery_verify_operation(job_id: str, options: dict | None = None) -> dict:
    return asyncio.run(_run_gitllery_verify_operation(job_id, options or {}))


async def _run_gitllery_verify_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID
    from app.services.gitllery import GitlleryService
    from app.services.tasks import TaskService

    repository_id = str(options.get("repository_id") or "")
    deep = bool(options.get("deep"))
    registered = current_admin_operation_attempt() is not None
    running_progress = {
        "phase": "running",
        "label": "Verifying Gitllery repository",
    }
    if registered:
        await update_current_admin_operation_progress(job_id, running_progress)
    else:
        async with async_session() as db:
            task_service = TaskService(db)
            task = await task_service.get(UUID(job_id))
            if task:
                await task_service.update_task(
                    task,
                    status="running",
                    progress=running_progress,
                )
                await db.commit()
    set_operation_status(
        job_id,
        "running",
        "admin-gitllery-verify",
        progress={"phase": "running", "label": "Verifying Gitllery repository"},
        meta={"entity": "gitllery-verify", **options},
    )
    try:
        async with async_session() as db:
            result = await GitlleryService(db).verify_segment_repository(
                repository_id,
                deep=deep,
            )
        status = "complete" if result["ok"] else "failed"
        if not registered:
            async with async_session() as db:
                task_service = TaskService(db)
                task = await task_service.get(UUID(job_id))
                if task:
                    await task_service.update_task(
                        task,
                        status=status,
                        progress={
                            "phase": status,
                            "label": "Gitllery verification finished",
                        },
                        result=result,
                        error=None if result["ok"] else "; ".join(result["errors"]),
                    )
                    await db.commit()
        set_operation_status(
            job_id,
            status,
            "admin-gitllery-verify",
            progress={"phase": status, "label": "Gitllery verification finished"},
            result=result,
            error=None if result["ok"] else "; ".join(result["errors"]),
            meta={"entity": "gitllery-verify", **options},
        )
        if not result["ok"]:
            raise RuntimeError("Gitllery verification failed")
        return result
    except Exception as exc:
        logger.exception("Gitllery verification failed: job_id=%s", job_id)
        set_operation_status(
            job_id,
            "failed",
            "admin-gitllery-verify",
            progress={"phase": "failed"},
            error=str(exc),
            meta={"entity": "gitllery-verify", **options},
        )
        raise
    finally:
        release_legacy_operation_lock(
            f"gitllery:verify:{repository_id}",
            job_id,
        )


async def _handoff_background_admin_operation(
    job_id: str, options: dict, result: dict, *, continuation: dict,
    delay_seconds: float, totals: dict,
) -> dict:
    """Commit the successor; the due dispatcher publishes after its cooldown."""
    from app.services.operations import prepare_admin_operation_handoff

    delivery = current_admin_operation_attempt()
    if delivery is None:
        raise RuntimeError("Background continuation requires a registered administrator dispatch")
    async with async_session() as db:
        handoff = await prepare_admin_operation_handoff(
            db, job_id, delivery[1],
            options={**options, "_background_continuation": continuation,
                     "_background_totals": totals},
            delay_seconds=max(2.0, delay_seconds),
            progress={"phase": "enqueued", "label": "Next background slice queued", **totals},
        )
        if handoff is None:
            raise RuntimeError("Background administrator attempt is no longer current")
        await db.commit()
    return {**result, "status": "pending", "_admin_handoff": True}


async def _run_gitllery_sync_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID
    from app.services.gitllery import GitlleryService
    from app.services.gitllery.service import (
        gitllery_projection_lock,
        rebuild_checkpoint,
    )
    from app.services.tasks import TaskService

    mode = options.get("mode") or "reconcile"
    repository_id = options.get("repository_id")
    registered = current_admin_operation_attempt() is not None
    running_progress = {
        "phase": "running",
        "label": "Projecting curation history...",
    }
    if registered:
        await update_current_admin_operation_progress(job_id, running_progress)
    else:
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="running",
                    progress=running_progress,
                )
                await task_db.commit()
    set_operation_status(job_id, "running", "admin-gitllery-sync",
        progress={"phase": "running", "label": "Projecting curation history..."},
        meta={"entity": "gitllery-sync", **options})
    try:
        ordering_lock = gitllery_projection_lock()
        if not ordering_lock.try_acquire():
            return await _handoff_background_admin_operation(
                job_id, options, {}, continuation=options.get("_background_continuation") or {},
                delay_seconds=2.0, totals=options.get("_background_totals") or {},
            )
        try:
            async with async_session() as db:
                svc = GitlleryService(db)
                if mode == "backfill":
                    projected = await svc.backfill(
                        resource_owner=job_id,
                        continuation=options.get("_background_continuation"),
                    )
                else:
                    projected = await svc.project_pending(
                        repository_id,
                        resource_owner=job_id,
                        continuation=options.get("_background_continuation"),
                    )
                # Scoped requests are promoted by project_pending() to one
                # globally ordered pass, because a commit outbox row can span
                # multiple repositories. Re-establish the library checkpoint
                # after either entry point.
                continuation = svc.continuation
                delay_seconds = svc.successor_delay_seconds
                checkpoint_set = False
                if continuation is None:
                    checkpoint_set = await rebuild_checkpoint(db, svc.last_projection_high_water)
        finally:
            ordering_lock.release()

        totals = dict(options.get("_background_totals") or {})
        for rid, count in projected.items():
            totals[rid] = totals.get(rid, 0) + count
        result = {
            "mode": mode,
            "repository_id": repository_id,
            "projection_scope": "library",
            "projected_repos": len(totals),
            "projected_commits": sum(totals.values()),
            "checkpoint_rebuilt": checkpoint_set,
        }
        if continuation is not None:
            return await _handoff_background_admin_operation(
                job_id, options, result, continuation=continuation,
                delay_seconds=delay_seconds, totals=totals,
            )
        label = f"Projected {result['projected_commits']} commits across {result['projected_repos']} repos"
        if not registered:
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(
                        task,
                        status="complete",
                        progress={"phase": "complete", "label": label},
                        result=result,
                    )
                    await task_db.commit()
        set_operation_status(job_id, "complete", "admin-gitllery-sync",
            progress={"phase": "complete", "label": label},
            result=result, meta={"entity": "gitllery-sync", **options})
        return result
    except Exception as exc:
        logger.exception("Gitllery sync failed: job_id=%s", job_id)
        if not registered:
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(
                        task,
                        status="failed",
                        progress={"phase": "failed"},
                        error=str(exc),
                    )
                    await task_db.commit()
        set_operation_status(job_id, "failed", "admin-gitllery-sync",
            progress={"phase": "failed"}, error=str(exc), meta={"entity": "gitllery-sync", **options})
        raise
    finally:
        release_legacy_operation_lock(
            "library:gitllery-sync:active",
            job_id,
        )


def run_search_reindex_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — full Meilisearch reindex (works/creators/tags)."""
    # SearchService owns bounded search_index slices and takes maintenance only
    # for the atomic swap.  Wrapping the coordinator in the legacy operation
    # lock would otherwise serialize all 67k documents behind one long lease.
    return asyncio.run(_run_search_reindex_operation(job_id, options or {}))


def run_creator_alias_backfill_operation(
    job_id: str,
    options: dict | None = None,
) -> dict:
    """Compatibility entry point for the registered creator-alias operation."""

    return asyncio.run(
        _run_creator_alias_backfill_operation(job_id, options or {})
    )


async def _run_creator_alias_backfill_operation(
    job_id: str,
    options: dict,
) -> dict:
    from app.services.creator_aliases import backfill_all_creator_aliases

    del job_id, options
    async with async_session() as db:
        result = await backfill_all_creator_aliases(
            db,
            request_projection=True,
        )
    return {**result, "message": "Creator alias backfill complete"}


async def _run_search_reindex_operation(job_id: str, options: dict) -> dict:
    from app.services.search import SearchService
    from app.services.search_delivery import run_delivery_slice
    from app.services.search_rebuild import rebuild_status
    from app.services.operations import prepare_admin_operation_handoff
    from app.services.tasks import TaskService

    async with async_session() as db:
        result = await SearchService(db).reindex(resource_owner=job_id)
    delivery = current_admin_operation_attempt()
    step = {"successor_delay_seconds": 2}
    if result["status"] == "busy":
        result = {**result, "phase": "waiting", "batches": 0}
    elif result["status"] == "pending":
        step = await run_delivery_slice()
        result = await rebuild_status(result["build_id"])
        if step.get("status") == "ambiguous":
            step["successor_delay_seconds"] = 15
            result["message"] = "Search rebuild waiting for remote task reconciliation"
    if result["status"] in ("pending", "busy") and delivery is not None:
        async with async_session() as db:
            handoff = await prepare_admin_operation_handoff(
                db, job_id, delivery[1],
                options={**options, **({"_search_build_id": result["build_id"]} if result.get("build_id") else {})},
                delay_seconds=max(2, step.get("successor_delay_seconds", 2)),
                progress={"phase": result["phase"], "label": result["message"], "batches": result["batches"]},
            )
            if handoff is None:
                raise RuntimeError("Search rebuild administrator attempt is no longer current")
            await db.commit()
        # The due-dispatch coordinator respects next_retry_at. Calling
        # publish_admin_operation here would enqueue immediately and erase
        # the durable delay; the search outbox drives remote polling.
        return {**result, "_admin_handoff": True}
    status = "complete" if result["status"] == "ok" else "failed" if result["status"] == "error" else "running"
    if delivery is None:
        async with async_session() as db:
            service = TaskService(db)
            task = await service.get(UUID(job_id))
            if task:
                await service.update_task(task, status=status,
                    progress={"phase": result.get("phase", status), "label": result.get("message", "Search rebuild")}, result=result)
                await db.commit()
        set_operation_status(job_id, status, "admin-search-reindex", result=result,
                             progress={"phase": result.get("phase", status)}, meta={"entity": "search-reindex", **options})
    if status in ("complete", "failed"):
        release_legacy_operation_lock("library:search-reindex:active", job_id)
    return result


def run_curation_backfill_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — baseline curation history backfill."""
    return asyncio.run(_run_curation_backfill_operation(job_id, options or {}))


async def _run_curation_backfill_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID
    from app.services.curation import CurationService
    from app.services.tasks import TaskService

    registered = current_admin_operation_attempt() is not None
    running_progress = {
        "phase": "running",
        "label": "Backfilling curation baseline...",
    }
    if registered:
        await update_current_admin_operation_progress(job_id, running_progress)
    else:
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="running",
                    progress=running_progress,
                )
                await task_db.commit()
    set_operation_status(job_id, "running", "admin-curation-backfill",
        progress={"phase": "running", "label": "Backfilling curation baseline..."},
        meta={"entity": "curation-backfill", **options})
    try:
        async with async_session() as db:
            result = await CurationService(db).run_backfill(
                resource_owner=job_id,
                **({"continuation": options["_background_continuation"]}
                   if "_background_continuation" in options else {}),
            )
        totals = options.get("_background_totals") or {}
        for field in ("created", "skipped"):
            if field in result or field in totals:
                previous = totals.get(field) or {}
                current = result.get(field) or {}
                result[field] = {key: previous.get(key, 0) + current.get(key, 0)
                                 for key in previous.keys() | current.keys()}
        if result.get("status") == "pending":
            return await _handoff_background_admin_operation(
                job_id, options, result, continuation=result["continuation"],
                delay_seconds=result["successor_delay_seconds"],
                totals={field: result.get(field, {}) for field in ("created", "skipped")},
            )
        created = result.get("created", {})
        label = (f"Baseline: {created.get('creators', 0)} creators, "
                 f"{created.get('repositories', 0)} repos, {created.get('work_groups', 0)} work groups")
        if not registered:
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(
                        task,
                        status="complete",
                        progress={"phase": "complete", "label": label},
                        result=result,
                    )
                    await task_db.commit()
        set_operation_status(job_id, "complete", "admin-curation-backfill",
            progress={"phase": "complete", "label": label},
            result=result, meta={"entity": "curation-backfill", **options})
        return result
    except Exception as exc:
        logger.exception("Curation backfill failed: job_id=%s", job_id)
        if not registered:
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(
                        task,
                        status="failed",
                        progress={"phase": "failed"},
                        error=str(exc),
                    )
                    await task_db.commit()
        set_operation_status(job_id, "failed", "admin-curation-backfill",
            progress={"phase": "failed"}, error=str(exc), meta={"entity": "curation-backfill", **options})
        raise
    finally:
        release_legacy_operation_lock(
            "library:curation-backfill:active",
            job_id,
        )


def run_hierarchy_delete_operation(job_id: str, options: dict | None = None) -> dict:
    """Delete a repository/subscription/creator with bounded curation work."""

    return asyncio.run(run_heavy_io_operation(
        "operation:hierarchy-delete",
        job_id,
        lambda: _run_hierarchy_delete_operation(job_id, options or {}),
    ))


async def _run_hierarchy_delete_operation(job_id: str, options: dict) -> dict:
    from uuid import UUID

    from app.services.hierarchical_deletion import HierarchicalDeletionService
    from app.services.tasks import TaskService

    entity_type = options.get("entity_type")
    if entity_type not in {"repository", "subscription", "creator"}:
        raise ValueError(f"Unsupported hierarchy deletion type: {entity_type}")
    entity_ids = [UUID(value) for value in options.get("entity_ids") or []]
    if not entity_ids:
        raise ValueError("Hierarchy deletion requires at least one target")
    delete_files = bool(options.get("delete_files"))
    registered = current_admin_operation_attempt() is not None
    preflight_progress = {
        "phase": "preflight",
        "label": "Checking deletion scope",
        "current": 0,
        "total": 0,
    }
    if registered:
        await update_current_admin_operation_progress(job_id, preflight_progress)
    else:
        async with async_session() as task_db:
            svc = TaskService(task_db)
            task = await svc.get(UUID(job_id))
            if task:
                await svc.update_task(
                    task,
                    status="running",
                    progress=preflight_progress,
                )
                await task_db.commit()
    set_operation_status(
        job_id,
        "running",
        "hierarchy-delete",
        progress={"phase": "preflight", "label": "Checking deletion scope"},
        meta={"entity": "hierarchy-delete", **options},
    )

    async def publish_progress(current: int, total: int, label: str) -> None:
        progress = {
            "phase": "deleting",
            "label": label,
            "current": current,
            "total": total,
        }
        if registered:
            await update_current_admin_operation_progress(job_id, progress)
        else:
            async with async_session() as progress_db:
                progress_service = TaskService(progress_db)
                progress_task = await progress_service.get(UUID(job_id))
                if progress_task:
                    await progress_service.update_task(progress_task, progress=progress)
                    await progress_db.commit()
        set_operation_status(
            job_id,
            "running",
            "hierarchy-delete",
            progress=progress,
            meta={"entity": "hierarchy-delete", **options},
        )

    try:
        async with async_session() as db:
            deletion = HierarchicalDeletionService(db)
            scope = await deletion.scope(entity_type, entity_ids)
            result = await deletion.permanent_delete(
                scope,
                delete_files=delete_files,
                progress=publish_progress,
            )
        from app.services.cache import invalidate_creator_subscription_caches

        invalidate_creator_subscription_caches(include_works=True)
        if not registered:
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(
                        task,
                        status="complete",
                        progress={
                            "phase": "complete",
                            "label": result["message"],
                            "current": result["trashed_or_purged_works"],
                            "total": result["trashed_or_purged_works"],
                        },
                        result=result,
                    )
                    await task_db.commit()
        set_operation_status(
            job_id,
            "complete",
            "hierarchy-delete",
            progress={"phase": "complete", "label": result["message"]},
            result=result,
            meta={"entity": "hierarchy-delete", **options},
        )
        return result
    except Exception as exc:
        logger.exception("Hierarchy deletion failed: job_id=%s", job_id)
        if not registered:
            async with async_session() as task_db:
                svc = TaskService(task_db)
                task = await svc.get(UUID(job_id))
                if task:
                    await svc.update_task(
                        task,
                        status="failed",
                        progress={"phase": "failed", "label": "Deletion failed"},
                        error=str(exc),
                    )
                    await task_db.commit()
        set_operation_status(
            job_id,
            "failed",
            "hierarchy-delete",
            progress={"phase": "failed", "label": "Deletion failed"},
            error=str(exc),
            meta={"entity": "hierarchy-delete", **options},
        )
        raise
    finally:
        release_legacy_operation_lock(
            "library:hierarchy-delete:active",
            job_id,
        )
