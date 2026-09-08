"""Durable bounded subscription orchestration on the administrator runtime.

Source/item/domain rows precede the parent TaskRun lock. Cancellation commits
its parent fence first, then visits only batch-owned children. Publication
locks the parent for the Redis boundary, so cancellation cannot overtake it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from fastapi import HTTPException
from sqlalchemy import exists, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, OperationalError
from redis.exceptions import RedisError

from app.database import async_session
from app.models import DownloadJob, ImportJob, RepositorySyncReceipt, StorageArtifact, Subscription, SubscriptionSource, TaskRun
from app.models.scheduler_batch import SchedulerBatch, SchedulerBatchItem
from app.services.operations import (
    AdminOperationAttemptRejected,
    _lock_scope,
    current_admin_operation_attempt,
    fence_current_admin_operation_transaction,
    prepare_admin_operation,
    prepare_admin_operation_handoff,
)
from app.services.tasks import TaskService
from app.services.redis_budget import budget_redis, current_work_deadline

log = logging.getLogger(__name__)
OPERATION = "subscription-sync-batch"
SCOPE = "library:subscription-sync-batch:active"
TERMINAL = frozenset({"succeeded", "skipped", "failed", "cancelled"})
TRANSIENT = frozenset(
    {
        "lock_busy",
        "already_running",
        "recent_failure_backoff",
        "resource_pressure",
        "queue_saturated",
        "enqueue_busy",
        "redis_capacity",
        "redis_unwritable",
        "disk_backpressure",
        "import_backpressure",
        "storage_unavailable",
        "admission_check_failed",
        "enqueue_failed",
        "proxy_degraded",
        "download_unavailable",
    }
)
LEGACY_ID = UUID("3330dcea-e850-422f-9de8-3e5fd62fe6ef")


def now():
    return datetime.now(timezone.utc)


def _response(batch, task=None):
    return {
        "task_id": str(batch.task_id),
        "job_id": task.rq_job_id if task else None,
        "status": task.status if task else batch.state,
        "operation_type": OPERATION,
        "mode": batch.mode,
    }


async def admit_batch(db, *, mode, request_id=None, actor_user_id=None, legacy_task_id=None, commit=True):
    if mode not in {"force_eligible", "due_scan", "manual_all_enabled"}:
        raise HTTPException(422, detail="Invalid scheduler mode")
    request_id = request_id or uuid4()
    # One database lock covers request replay and global admission. The unique
    # constraints remain the authority even for callers outside this service.
    await _lock_scope(db, SCOPE)
    batch = (await db.execute(select(SchedulerBatch).where(SchedulerBatch.request_id == request_id))).scalar_one_or_none()
    if batch:
        if batch.mode != mode:
            raise HTTPException(
                409,
                detail={
                    "code": "request_mode_conflict",
                    "task_id": str(batch.task_id),
                    "mode": batch.mode,
                    "message": "Request identity already uses a different mode",
                },
            )
        task = await db.get(TaskRun, batch.task_id)
        result = _response(batch, task)
        if commit:
            await db.commit()
        return result
    active = (await db.execute(select(SchedulerBatch).where(SchedulerBatch.state == "active"))).scalar_one_or_none()
    if active:
        raise HTTPException(409, detail={"code": "batch_active", "task_id": str(active.task_id), "message": "A subscription sync batch is already active"})
    prepared = await prepare_admin_operation(
        db, operation_type=OPERATION, scope_key=SCOPE, title="Sync subscription sources", entity=OPERATION, options={"mode": mode}, queue_name="operations"
    )
    batch = SchedulerBatch(task_id=prepared.task.id, request_id=request_id, mode=mode, actor_user_id=actor_user_id, legacy_task_id=legacy_task_id)
    db.add(batch)
    await db.flush()
    result = _response(batch, prepared.task)
    if commit:
        await db.commit()
    # Existing due-dispatch recovery publishes the initial intent. No Redis,
    # source scan or network wait belongs in this HTTP transaction.
    return result


async def initialize_batch(db, task_id):
    batch = (await db.execute(select(SchedulerBatch).where(SchedulerBatch.task_id == task_id).with_for_update())).scalar_one()
    if batch.initialized_at is None:
        enabled = SubscriptionSource.is_enabled.is_(True)
        if batch.mode == "manual_all_enabled":
            from app.models.remote_discovery import UserSubscription, UserSubscriptionSource

            # Canonical source flags cache automatic demand, and legitimately
            # become false for manual-only members. Private policy is the
            # authority for this explicitly broader operator mode.
            enabled = or_(
                enabled,
                exists().where(
                    UserSubscriptionSource.subscription_source_id == SubscriptionSource.id,
                    UserSubscriptionSource.user_subscription_id == UserSubscription.id,
                    UserSubscriptionSource.is_enabled.is_(True),
                    UserSubscription.is_active.is_(True),
                ),
            )
        conditions = [Subscription.is_active.is_(True), enabled]
        if batch.mode != "manual_all_enabled":
            conditions.append(Subscription.sync_enabled.is_(True))
        candidates = (
            select(func.gen_random_uuid(), literal(batch.id), SubscriptionSource.id, SubscriptionSource.source, literal("pending"), literal(0), literal(False))
            .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
            .where(*conditions)
        )
        await db.execute(
            insert(SchedulerBatchItem)
            .from_select(["id", "batch_id", "source_id", "source", "status", "attempts", "owns_download"], candidates, include_defaults=False)
            .on_conflict_do_nothing(constraint="uq_scheduler_batch_source")
        )
        batch.initialized_at = now()
    await fence_current_admin_operation_transaction(db, task_id=task_id)
    await db.flush()
    return batch


def defer(item, reason, error=None):
    item.status = "waiting"
    item.reason_code = reason
    item.error = str(error)[:500] if error else None
    item.next_retry_at = now() + timedelta(seconds=min(300, 30 * 2 ** min(max(item.attempts - 1, 0), 4)))


async def reconcile_item(db, item):
    """Current domain rows override receipts; recoverable imports remain open."""
    if not item.download_job_id:
        return
    job = await db.get(DownloadJob, item.download_job_id)
    receipt = (await db.execute(select(RepositorySyncReceipt).where(RepositorySyncReceipt.source_download_job_id == item.download_job_id))).scalar_one_or_none()
    imports = list(
        (await db.execute(select(ImportJob).where(ImportJob.download_job_id == item.download_job_id).order_by(ImportJob.created_at.desc()))).scalars()
    )
    recoverable = (
        await db.execute(
            select(StorageArtifact.id)
            .where(StorageArtifact.download_job_id == item.download_job_id, StorageArtifact.state.in_(("new", "importing", "failed")))
            .limit(1)
        )
    ).scalar_one_or_none()
    status = job.status if job else receipt.status if receipt else None
    if imports and imports[0].status in {"failed", "stale", "cancelled"}:
        status = imports[0].status
    active_import = any(c.status in {"enqueued", "running", "paused", "recovering"} for c in imports)
    # Automatic retries are persisted as enqueued imports before publication.
    # A terminal failed import is exhausted/operator-controlled; failed ledger
    # rows must preserve recovery evidence without keeping this batch running.
    if active_import or (recoverable and status not in {"failed", "stale", "cancelled"}):
        item.status = "importing"
    elif status in {"enqueued", "paused"}:
        item.status = "queued" if status == "enqueued" else "waiting"
    elif status in {"downloading", "running", "recovering"}:
        item.status = "downloading"
    elif status in {"downloaded", "importing"}:
        item.status = "importing"
    else:
        if status in {"complete", "failed", "stale", "cancelled"}:
            if job and job.subscription_source_id:
                from app.services.operation_attention import upsert_repository_sync_receipt

                receipt = await upsert_repository_sync_receipt(db, job, status=status)
            item.status = "succeeded" if status == "complete" else "failed"
            item.reason_code = "sync_complete" if status == "complete" else f"child_{status}"
            item.outcome = {
                "status": status,
                "download_job_id": str(item.download_job_id),
                "receipt_id": str(receipt.id) if receipt else None,
                "works_imported": receipt.works_imported if receipt else 0,
            }
        elif job is None:
            item.status = "failed"
            item.reason_code = "child_identity_missing"
            item.error = "Download identity has no current row or durable receipt; manual reconciliation required"
    item.next_retry_at = None if item.status in TERMINAL else now() + timedelta(seconds=30)


async def _publish_bound_child(item_id):
    # The helper committed its binding before this separate session can read it.
    from app.services.download_dispatch import recover_download_dispatch_candidate

    publication = None
    async with async_session() as db:
        item = await db.get(SchedulerBatchItem, item_id)
        if item is None or not item.download_job_id or not item.owns_download:
            return
        job = await db.get(DownloadJob, item.download_job_id)
        child = await db.get(TaskRun, item.child_task_id) if item.child_task_id else None
        if job and child and job.status == child.status == "enqueued":
            publication = await recover_download_dispatch_candidate(db, child, job)
    if publication is not None:
        async with async_session() as db:
            item = (await db.execute(select(SchedulerBatchItem).where(SchedulerBatchItem.id == item_id).with_for_update())).scalar_one()
            if item.status in TERMINAL:
                return
            if publication == "deferred":
                defer(item, "publication_deferred")
            elif publication != "cancelled":
                await reconcile_item(db, item)
                if item.status == "queued":
                    item.reason_code = None
            batch = await db.get(SchedulerBatch, item.batch_id)
            await fence_current_admin_operation_transaction(db, task_id=batch.task_id)
            await db.commit()


async def _process_item(item_id, task_id, mode):
    from app.services.subscription_enqueue import enqueue_subscription_source_sync

    async with async_session() as db:
        item = (await db.execute(select(SchedulerBatchItem).where(SchedulerBatchItem.id == item_id).with_for_update())).scalar_one()
        if item.status in TERMINAL:
            return
        item.attempts += 1
        if item.download_job_id:
            await reconcile_item(db, item)
        else:
            result = await enqueue_subscription_source_sync(
                db,
                item.source_id,
                trigger="scheduler" if mode == "due_scan" else "manual_scheduler_batch",
                parent_task_id=UUID(task_id),
                force_reason=f"scheduler_{mode}",
                batch_item_id=item.id,
                batch_mode=mode,
            )
            # The enqueue helper commits the identity with its outbox. Never
            # reuse a caller ORM object after a publication/recovery rollback.
            item = await db.get(SchedulerBatchItem, item_id, populate_existing=True)
            if item.download_job_id:
                await reconcile_item(db, item)
            else:
                code = result.get("skip_reason") or (result.get("reason") or {}).get("code") or "unexpected_enqueue_result"
                linked = result.get("job_id") or (result.get("reason") or {}).get("details", {}).get("job_id")
                if code == "already_running" and linked:
                    item.download_job_id = UUID(linked)
                    item.owns_download = False
                    await reconcile_item(db, item)
                elif code in TRANSIENT or (result.get("reason") or {}).get("retryable"):
                    defer(item, code)
                else:
                    item.status = "failed" if result.get("status") == "error" else "skipped"
                    item.reason_code = code
        await fence_current_admin_operation_transaction(db, task_id=task_id)
        await db.commit()
    await _publish_bound_child(item_id)


async def _record_item_error(item_id, task_id, exc):
    async with async_session() as db:
        item = (await db.execute(select(SchedulerBatchItem).where(SchedulerBatchItem.id == item_id).with_for_update())).scalar_one()
        if item.status in TERMINAL:
            return
        item.attempts += 1
        if isinstance(exc, (RedisError, OperationalError, DBAPIError, OSError, TimeoutError)):
            defer(item, "infrastructure_unavailable", exc)
        else:
            item.status = "failed"
            item.reason_code = "unexpected_source_error"
            item.error = f"{type(exc).__name__}: {exc}"[:500]
        await fence_current_admin_operation_transaction(db, task_id=task_id)
        await db.commit()


async def summarize(db, batch):
    counts = dict(
        (
            await db.execute(select(SchedulerBatchItem.status, func.count()).where(SchedulerBatchItem.batch_id == batch.id).group_by(SchedulerBatchItem.status))
        ).all()
    )
    reasons = dict(
        (
            await db.execute(
                select(SchedulerBatchItem.reason_code, func.count())
                .where(SchedulerBatchItem.batch_id == batch.id, SchedulerBatchItem.status == "skipped")
                .group_by(SchedulerBatchItem.reason_code)
            )
        ).all()
    )
    result = {
        "mode": batch.mode,
        "candidate_count": sum(counts.values()),
        **{f"{s}_count": counts.get(s, 0) for s in ["pending", "queued", "waiting", "downloading", "importing", "succeeded", "skipped", "failed", "cancelled"]},
        "skipped_reasons": reasons,
    }
    result["enqueued_count"] = (
        await db.execute(
            select(func.count()).select_from(SchedulerBatchItem).where(SchedulerBatchItem.batch_id == batch.id, SchedulerBatchItem.owns_download.is_(True))
        )
    ).scalar_one()
    result["error_count"] = result["failed_count"]
    return result


async def _defer_infrastructure_slice(task_id, options):
    delivery = current_admin_operation_attempt()
    if delivery is None:
        raise AdminOperationAttemptRejected("No registered batch attempt")
    async with async_session() as db:
        task = await fence_current_admin_operation_transaction(db, task_id=task_id)
        progress = {**(task.progress_data or {}), "phase": "waiting", "label": "Waiting for infrastructure recovery"}
        result = {**(task.result_data or {}), "status": "pending", "waiting_reason": "infrastructure_unavailable"}
        await TaskService(db).update_task(task, progress=progress, result=result)
        handoff = await prepare_admin_operation_handoff(db, task_id, delivery[1], options=options, delay_seconds=30, progress=progress)
        if handoff is None:
            raise AdminOperationAttemptRejected("Batch retry is no longer current")
        await db.commit()
    return {**result, "_admin_handoff": True}


async def run_batch_slice(task_id: str, options: dict):
    with budget_redis() as budget:
        try:
            result = await _run_batch_slice(task_id, options)
        except (RedisError, OperationalError, DBAPIError, OSError, TimeoutError):
            result = await _defer_infrastructure_slice(task_id, options)
        if result.get("_admin_ready_successor"):
            result["_admin_successor_deadline"] = budget.deadline
        return result


async def _run_batch_slice(task_id: str, options: dict):
    delivery = current_admin_operation_attempt()
    if delivery is None:
        raise AdminOperationAttemptRejected("Scheduler batches require a registered attempt")
    deadline = current_work_deadline()
    async with async_session() as db:
        batch = await initialize_batch(db, UUID(task_id))
        batch_id, mode = batch.id, batch.mode
        await db.commit()
    async with async_session() as db:
        ids = list(
            (
                await db.execute(
                    select(SchedulerBatchItem.id)
                    .where(
                        SchedulerBatchItem.batch_id == batch_id,
                        SchedulerBatchItem.status.not_in(TERMINAL),
                        or_(SchedulerBatchItem.next_retry_at.is_(None), SchedulerBatchItem.next_retry_at <= now()),
                    )
                    .order_by(SchedulerBatchItem.next_retry_at.asc().nullsfirst(), SchedulerBatchItem.id)
                    .limit(25)
                )
            ).scalars()
        )
    for item_id in ids:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            async with asyncio.timeout(remaining):
                await _process_item(item_id, task_id, mode)
        except AdminOperationAttemptRejected:
            raise
        except Exception as exc:
            log.warning("Batch item %s deferred/failed: %s", item_id, type(exc).__name__)
            await _record_item_error(item_id, task_id, exc)
    async with async_session() as db:
        batch = await db.get(SchedulerBatch, batch_id)
        result = await summarize(db, batch)
        terminal_count = sum(result[f"{s}_count"] for s in TERMINAL)
        pending = terminal_count < result["candidate_count"]
        result["status"] = "pending" if pending else "partial_error" if result["failed_count"] else "complete" if result["succeeded_count"] else "noop"
        result["message"] = (
            "Subscription sync in progress"
            if pending
            else "Some subscription sources failed"
            if result["failed_count"]
            else "Subscription sync complete"
            if result["succeeded_count"]
            else "No sources synchronized"
        )
        batch.result = result
        progress = {
            "phase": "waiting" if pending else result["status"],
            "label": result["message"],
            "current": terminal_count,
            "total": result["candidate_count"],
            **result,
        }
        task = await fence_current_admin_operation_transaction(db, task_id=task_id)
        await TaskService(db).update_task(task, progress=progress, result=result)
        if pending:
            next_due = (
                await db.execute(
                    select(func.min(SchedulerBatchItem.next_retry_at)).where(
                        SchedulerBatchItem.batch_id == batch_id, SchedulerBatchItem.status.not_in(TERMINAL)
                    )
                )
            ).scalar_one()
            fresh = result["pending_count"] > 0
            delay = 1 if fresh else max(1, min(300, (next_due - now()).total_seconds())) if next_due else 30
            handoff = await prepare_admin_operation_handoff(db, task_id, delivery[1], options=options, delay_seconds=delay, progress=progress)
            if handoff is None:
                raise AdminOperationAttemptRejected("Batch handoff is no longer current")
        else:
            batch.state = "failed" if result["failed_count"] else "complete"
        await db.commit()
    return {**result, **({"_admin_handoff": True, "_admin_ready_successor": handoff.attempt if fresh else None} if pending else {})}


async def cancel_batch(db, task_id, *, operator, note=None):
    """Persist the parent fence and one cleanup outbox; never visit children here."""
    task = (await db.execute(select(TaskRun).where(TaskRun.id == task_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, detail="Task not found")
    if task.status == "complete":
        raise HTTPException(409, detail={"code": "batch_terminal", "message": "Batch is already complete"})
    batch = (await db.execute(select(SchedulerBatch).where(SchedulerBatch.task_id == task_id))).scalar_one_or_none()
    if batch is None:
        raise HTTPException(409, detail={"code": "legacy_batch", "message": "Recover this legacy batch first"})
    cleanup_id = (task.meta or {}).get("batch_cancel_cleanup_task_id")
    batch_id = batch.id
    if cleanup_id:
        pending = bool((task.result_data or {}).get("cleanup_pending", True))
        await db.commit()
        batch.state = "cancelled"
        await db.commit()
        return {"task_id": str(task_id), "status": "cancelled", "cleanup_pending": pending, "cleanup_task_id": cleanup_id}
    if not cleanup_id:
        prepared = await prepare_admin_operation(
            db,
            operation_type="subscription-sync-batch-cleanup",
            scope_key=f"library:subscription-sync-cancel:{task_id}",
            title="Cancel subscription batch downloads",
            entity="subscription-sync-batch-cleanup",
            options={"batch_id": str(batch_id), "parent_task_id": str(task_id), "operator": operator, "note": note},
            queue_name="operations",
        )
        prepared.task.parent_task_id = task_id
        cleanup_id = str(prepared.task.id)
        task.meta = {**(task.meta or {}), "batch_cancel_cleanup_task_id": cleanup_id}
    await TaskService(db).update_task(
        task,
        status="cancelled",
        result={**(task.result_data or {}), "status": "cancelled", "cleanup_pending": True, "cleanup_task_id": cleanup_id},
        progress={"phase": "cancelling", "label": "Publication stopped; child cleanup queued"},
    )
    await db.commit()
    # Parent-only fence precedes the batch row (snapshot locks batch->parent).
    # The cleanup has its own scope and must not block a new global batch.
    batch.state = "cancelled"
    await db.commit()
    return {"task_id": str(task_id), "status": "cancelled", "cleanup_pending": True, "cleanup_task_id": cleanup_id}


async def _cancel_batch_item(item_id, cleanup_task_id, options):
    from app.services.task_engine import TaskEngine
    from app.models.task_state import DOWNLOAD_CANCELLABLE_STATUSES

    async with async_session() as db:
        item = (await db.execute(select(SchedulerBatchItem).where(SchedulerBatchItem.id == item_id).with_for_update())).scalar_one()
        if item.status in TERMINAL:
            return
        item.attempts += 1
        await reconcile_item(db, item)
        if item.status not in TERMINAL:
            if item.owns_download and item.download_job_id:
                job = (await db.execute(select(DownloadJob).where(DownloadJob.id == item.download_job_id).with_for_update())).scalar_one_or_none()
                if job and job.status in DOWNLOAD_CANCELLABLE_STATUSES:
                    await TaskEngine(db).cancel_download(job.id, operator=options.get("operator"), note=options.get("note"))
            item.status = "cancelled"
            item.reason_code = "batch_cancelled"
            item.next_retry_at = None
        await fence_current_admin_operation_transaction(db, task_id=cleanup_task_id)
        await db.commit()


async def run_batch_cleanup_slice(task_id, options):
    with budget_redis() as budget:
        try:
            result = await _run_batch_cleanup_slice(task_id, options)
        except (RedisError, OperationalError, DBAPIError, OSError, TimeoutError):
            result = await _defer_infrastructure_slice(task_id, options)
        if result.get("_admin_ready_successor"):
            result["_admin_successor_deadline"] = budget.deadline
        return result


async def _run_batch_cleanup_slice(task_id, options):
    """Bounded, restartable child cancellation on the existing operation runtime."""
    delivery = current_admin_operation_attempt()
    if delivery is None:
        raise AdminOperationAttemptRejected("Batch cleanup requires a registered attempt")
    batch_id = UUID(options["batch_id"])
    parent_id = UUID(options["parent_task_id"])
    deadline = current_work_deadline()
    async with async_session() as db:
        # No download identity means there is no external child to control.
        await db.execute(
            update(SchedulerBatchItem)
            .where(SchedulerBatchItem.batch_id == batch_id, SchedulerBatchItem.download_job_id.is_(None), SchedulerBatchItem.status.not_in(TERMINAL))
            .values(status="cancelled", reason_code="batch_cancelled", next_retry_at=None)
        )
        await fence_current_admin_operation_transaction(db, task_id=task_id)
        await db.commit()
        due = or_(SchedulerBatchItem.reason_code.is_(None), SchedulerBatchItem.reason_code != "cancel_retry", SchedulerBatchItem.next_retry_at <= now())
        ids = list(
            (
                await db.execute(
                    select(SchedulerBatchItem.id)
                    .where(SchedulerBatchItem.batch_id == batch_id, SchedulerBatchItem.status.not_in(TERMINAL), due)
                    .order_by(SchedulerBatchItem.next_retry_at.asc().nullsfirst(), SchedulerBatchItem.id)
                    .limit(25)
                )
            ).scalars()
        )
    for item_id in ids:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            async with asyncio.timeout(remaining):
                await _cancel_batch_item(item_id, task_id, options)
        except AdminOperationAttemptRejected:
            raise
        except Exception as exc:
            async with async_session() as db:
                item = (await db.execute(select(SchedulerBatchItem).where(SchedulerBatchItem.id == item_id).with_for_update())).scalar_one()
                item.attempts += 1
                defer(item, "cancel_retry", type(exc).__name__)
                await fence_current_admin_operation_transaction(db, task_id=task_id)
                await db.commit()
    async with async_session() as db:
        batch = await db.get(SchedulerBatch, batch_id)
        result = await summarize(db, batch)
        count = sum(result[f"{status}_count"] for status in TERMINAL)
        pending = count < result["candidate_count"]
        result.update(
            status="cancelled",
            cleanup_pending=pending,
            cleanup_task_id=str(task_id),
            message="Child cancellation pending" if pending else "Batch cancellation complete",
        )
        batch.state = "cancelled"
        batch.result = result
        parent = (await db.execute(select(TaskRun).where(TaskRun.id == parent_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
        progress = {
            **result,
            "phase": "cancelling" if pending else "cancelled",
            "label": result["message"],
            "current": count,
            "total": result["candidate_count"],
        }
        if parent.status == "cancelled":
            await TaskService(db).update_task(parent, result=result, progress=progress)
        await fence_current_admin_operation_transaction(db, task_id=task_id)
        if pending:
            ready = (
                await db.execute(
                    select(SchedulerBatchItem.id)
                    .where(
                        SchedulerBatchItem.batch_id == batch_id,
                        SchedulerBatchItem.status.not_in(TERMINAL),
                        or_(
                            SchedulerBatchItem.reason_code.is_(None),
                            SchedulerBatchItem.reason_code != "cancel_retry",
                            SchedulerBatchItem.next_retry_at <= now(),
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none() is not None
            handoff = await prepare_admin_operation_handoff(db, task_id, delivery[1], options=options, delay_seconds=1 if ready else 30, progress=progress)
            if handoff is None:
                raise AdminOperationAttemptRejected("Cleanup handoff is no longer current")
        await db.commit()
    return {**result, **({"_admin_handoff": True, "_admin_ready_successor": handoff.attempt if ready else None} if pending else {})}


async def batch_items(db, task_id, *, offset=0, limit=50):
    batch = (await db.execute(select(SchedulerBatch).where(SchedulerBatch.task_id == task_id))).scalar_one_or_none()
    if batch is None:
        raise HTTPException(404, detail="Scheduler batch not found")
    total = (await db.execute(select(func.count()).select_from(SchedulerBatchItem).where(SchedulerBatchItem.batch_id == batch.id))).scalar_one()
    items = (
        await db.execute(
            select(SchedulerBatchItem)
            .where(SchedulerBatchItem.batch_id == batch.id)
            .order_by(SchedulerBatchItem.id)
            .offset(max(0, offset))
            .limit(max(1, min(100, limit)))
        )
    ).scalars()
    return {
        "total": total,
        "items": [
            {
                "id": str(i.id),
                "source_id": str(i.source_id),
                "source": i.source,
                "status": i.status,
                "attempts": i.attempts,
                "next_retry_at": i.next_retry_at,
                "download_job_id": str(i.download_job_id) if i.download_job_id else None,
                "reason_code": i.reason_code,
                "error": i.error,
                "outcome": i.outcome,
            }
            for i in items
        ],
    }


async def recover_legacy_batch(db, *, apply=False, legacy_task_id=LEGACY_ID):
    """Derive an idempotent recovery batch; never rewrite legacy evidence."""
    legacy = await db.get(TaskRun, legacy_task_id)
    if legacy is None or legacy.operation_type != OPERATION:
        raise HTTPException(404, detail="Legacy scheduler batch not found")
    recorded = legacy.result_data or {}
    mode = recorded.get("mode") or (legacy.meta or {}).get("mode") or "manual_all_enabled"
    recovery_request = uuid5(NAMESPACE_URL, f"auto-gallery:scheduler-recovery:{legacy_task_id}")
    existing = (await db.execute(select(SchedulerBatch).where(SchedulerBatch.request_id == recovery_request))).scalar_one_or_none()
    if existing:
        return {**_response(existing, await db.get(TaskRun, existing.task_id)), "dry_run": not apply, "existing": True}
    candidates = {}
    unresolved = []
    for value in recorded.get("job_ids", []):
        job_id = UUID(value)
        job = await db.get(DownloadJob, job_id)
        receipt = (await db.execute(select(RepositorySyncReceipt).where(RepositorySyncReceipt.source_download_job_id == job_id))).scalar_one_or_none()
        source_id = job.subscription_source_id if job else receipt.repository_id if receipt else None
        if source_id is None:
            unresolved.append(str(job_id))
        else:
            candidates[source_id] = job_id
    for row in recorded.get("skipped", []) + recorded.get("errors", []):
        if row.get("source_id"):
            candidates.setdefault(UUID(row["source_id"]), None)
    # Reconcile queue-skipped candidates against later domain evidence before
    # deciding that another enqueue is needed. Failed attempts are explicit
    # outcomes; missing evidence is never interpreted as successful processing.
    for source_id, original_job in list(candidates.items()):
        if original_job is not None:
            continue
        current = (
            await db.execute(
                select(DownloadJob)
                .where(DownloadJob.subscription_source_id == source_id, DownloadJob.created_at >= legacy.created_at)
                .order_by(DownloadJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        receipt = (
            await db.execute(
                select(RepositorySyncReceipt)
                .where(RepositorySyncReceipt.repository_id == source_id, RepositorySyncReceipt.finished_at >= legacy.created_at)
                .order_by(RepositorySyncReceipt.finished_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        # Any active job wins; otherwise the newest terminal evidence wins.
        if current and (current.status not in {"complete", "failed", "stale", "cancelled"} or receipt is None or current.created_at >= receipt.finished_at):
            candidates[source_id] = current.id
        elif receipt:
            candidates[source_id] = receipt.source_download_job_id
    preview = {
        "dry_run": not apply,
        "legacy_task_id": str(legacy_task_id),
        "candidate_count": len(candidates),
        "recorded_enqueued_count": len(recorded.get("job_ids", [])),
        "recorded_skipped_count": len(recorded.get("skipped", [])),
        "linked_count": sum(v is not None for v in candidates.values()),
        "unresolved_download_ids": unresolved,
    }
    if not apply:
        return preview
    expected = recorded.get("candidate_count")
    if unresolved or (expected is not None and len(candidates) != int(expected)):
        raise HTTPException(409, detail={"code": "legacy_identity_unresolved", **preview})
    # Admission's commit occurs only after all legacy mapping checks pass.
    accepted = await admit_batch(db, mode=mode, request_id=recovery_request, legacy_task_id=legacy_task_id, commit=False)
    batch = (await db.execute(select(SchedulerBatch).where(SchedulerBatch.task_id == UUID(accepted["task_id"])).with_for_update())).scalar_one()
    if batch.initialized_at is None:
        for source_id, job_id in candidates.items():
            item = SchedulerBatchItem(batch_id=batch.id, source_id=source_id, download_job_id=job_id, owns_download=False, status="pending")
            db.add(item)
            await db.flush()
            if job_id:
                await reconcile_item(db, item)
        batch.initialized_at = now()
        await db.commit()
    return {**preview, **accepted}
