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












def _registered_terminal_outcome(operation_type: str, result: dict) -> tuple[str, str | None, str | None]:
    """Compatibility adapter for callers of the former worker result policy."""
    from app.services.operations import ADMIN_OPERATION_REGISTRY
    outcome = ADMIN_OPERATION_REGISTRY[operation_type].normalize_result(result)
    return outcome.status, outcome.error, outcome.reason_code


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
    result = await _run_registered_admin_operation_delivery(task_id, attempt)
    # A ready successor is already durable. Publish only after the previous
    # execution lease has been released, so another worker can claim it safely.
    successor = result.pop("_admin_ready_successor", None)
    successor_deadline = result.pop("_admin_successor_deadline", None)
    if successor is not None:
        import time
        from app.services.operations import publish_admin_operation
        from app.services.redis_budget import budget_redis
        remaining = successor_deadline - time.monotonic() if successor_deadline is not None else 3
        # The durable outbox owns recovery when the worker has spent its tail
        # reserve. Never start another Redis wait with no budget remaining.
        if remaining >= .75:
            with budget_redis(seconds=min(3, remaining), reserve_seconds=0):
                await publish_admin_operation(task_id, int(successor))
    return result


async def _run_registered_admin_operation_delivery(task_id: str, attempt: int) -> dict:
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
            outcome = await operation_task
            result = outcome.payload
            if outcome.continues:
                return result
            terminal_status, terminal_error, reason_code = outcome.status, outcome.error, outcome.reason_code
            if not await update_admin_task(
                task_id,
                attempt,
                allowed_current_statuses=("enqueued", "running", "paused", "recovering"),
                status=terminal_status,
                progress={
                    **outcome.progress,
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


async def _execute_registered_admin_operation(operation_type: str, task_id: str, attempt: int, options: dict):
    """Run the trusted registry's handler and obtain its normalized outcome."""
    from app.services.operations import ADMIN_OPERATION_REGISTRY
    try:
        spec = ADMIN_OPERATION_REGISTRY[operation_type]
    except KeyError:
        raise ValueError(f"Unsupported registered administrator operation: {operation_type}") from None
    return await spec.execute(task_id, attempt, options)


# RQ records from earlier versions resolve these historical function names.
from app.services.admin_operation_handlers import (
    _await_admin_file_finalizer,
    _finalize_registered_file_result,
    _DiskImportPublisherGuard,
    _disk_import_publication_count,
    disk_import_completion_progress,
    run_clear_operation,
    run_cleanup_metadata_jsons_operation,
    _run_clear_operation,
    run_library_rebuild_operation,
    _run_library_rebuild_operation,
    run_disk_import_operation,
    _run_registered_disk_import_operation,
    _run_disk_import_operation,
    run_gitllery_rebuild_operation,
    run_creator_reenrich_operation,
    _run_creator_reenrich_operation,
    run_gitllery_sync_operation,
    run_gitllery_verify_operation,
    run_gitllery_build_operation,
    _run_gitllery_build_operation,
    _run_gitllery_verify_operation,
    _handoff_background_admin_operation,
    _run_gitllery_sync_operation,
    run_search_reindex_operation,
    run_creator_alias_backfill_operation,
    _run_creator_alias_backfill_operation,
    _run_search_reindex_operation,
    run_curation_backfill_operation,
    _run_curation_backfill_operation,
    run_hierarchy_delete_operation,
    _run_hierarchy_delete_operation,
    run_subscription_sync_batch,
    run_subscription_sync_batch_cleanup,
)
