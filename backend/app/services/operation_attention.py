"""Operation attention, reconciliation, receipts, and safe task compaction."""

from __future__ import annotations

import logging
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.repository_sync_receipt import RepositorySyncReceipt
from app.models.repository_sync_receipt import MaintenanceAuditEvent, SearchIndexState
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.task_run import TaskRun
from app.services.sync_outcome import download_job_outcome
from app.services.tasks import TaskService, task_payload

logger = logging.getLogger(__name__)

ACTIVE_TASK_STATUSES = frozenset({"enqueued", "running", "paused", "recovering"})
ATTENTION_TASK_STATUSES = frozenset({"failed", "stale"})
COMPACTABLE_DOWNLOAD_STATUSES = frozenset({"complete", "failed", "stale", "cancelled"})
RESOLVED_RETENTION = timedelta(days=7)
USER_OPERATION_RETENTION = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


async def _write_maintenance_audit(
    db: AsyncSession,
    event_type: str,
    identities: list[str],
    summary: dict[str, Any],
) -> None:
    if not identities:
        return
    digest = hashlib.sha256(
        json.dumps(
            {
                "event_type": event_type,
                "identities": sorted(identities),
                "changes": summary.get("issues") or summary.get("deleted_tasks"),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    await db.execute(
        insert(MaintenanceAuditEvent)
        .values(
            event_type=event_type,
            idempotency_key=f"{event_type}:{digest}",
            summary=summary,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )


def infer_reason_code(status: str, error: str | None, *, orphaned: bool = False) -> str | None:
    if orphaned:
        return "orphaned_subject"
    lowered = (error or "").lower()
    if "download staging conflict" in lowered:
        return "download_staging_conflict"
    if "download staging manifest" in lowered or "staging recovery manifest" in lowered:
        return "download_staging_manifest_error"
    if "out of memory" in lowered or "oom" in lowered or "code -9" in lowered:
        return "out_of_memory"
    if "heartbeat" in lowered or status == "stale":
        return "lost_heartbeat"
    if "auth" in lowered or "cookie" in lowered or "login" in lowered:
        return "auth_unhealthy"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "dns" in lowered or "network" in lowered or "connection" in lowered:
        return "network_error"
    if "32767" in lowered or "query arguments" in lowered:
        return "search_reindex_argument_limit"
    if "transition" in lowered:
        return "state_conflict"
    return "task_failed" if status == "failed" else None


def _rq_reference_is_active(task: TaskRun) -> bool:
    if not task.rq_job_id:
        return False
    try:
        from rq.exceptions import NoSuchJobError
        from rq.job import Job

        from app.services.redis_client import get_redis

        job = Job.fetch(task.rq_job_id, connection=get_redis())
        status = str(job.get_status(refresh=True)).lower()
        return any(
            value in status
            for value in ("queued", "started", "scheduled", "deferred")
        )
    except NoSuchJobError:
        return False
    except Exception:
        # Redis uncertainty must never authorize automatic resolution.
        logger.warning("Unable to verify RQ reference for task %s", task.id, exc_info=True)
        return True


async def upsert_repository_sync_receipt(
    db: AsyncSession,
    job: DownloadJob,
    *,
    status: str | None = None,
) -> RepositorySyncReceipt | None:
    """Materialize a compact domain receipt before operational rows disappear."""

    if not job.subscription_source_id:
        return None
    terminal_status = status or job.status
    if terminal_status not in {"complete", "failed", "stale", "cancelled"}:
        return None

    import_job = (
        await db.execute(
            select(ImportJob)
            .where(ImportJob.download_job_id == job.id)
            .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    task = (
        await db.execute(
            select(TaskRun)
            .where(
                TaskRun.subject_type == "download_job",
                TaskRun.subject_id == job.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    manifest = dict(job.manifest or {})
    outcome = download_job_outcome(job) or {}
    import_stats = manifest.get("import_stats") if isinstance(manifest.get("import_stats"), dict) else {}
    started_at = task.started_at if task else job.created_at
    finished_at = (task.finished_at if task else None) or job.updated_at or _now()
    duration_ms = None
    if started_at and finished_at:
        duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
    error = job.error_log or (task.error_log if task else None)
    reason_code = infer_reason_code(terminal_status, error)

    values = {
        "repository_id": job.subscription_source_id,
        "source_download_job_id": job.id,
        "source_import_job_id": import_job.id if import_job else None,
        "source_task_id": task.id if task else None,
        "source": job.source,
        "status": terminal_status,
        "outcome_code": outcome.get("code"),
        "metadata_count": _int(outcome.get("metadata_count") or manifest.get("metadata_json_count")),
        "media_count": _int(outcome.get("media_count") or manifest.get("image_count")),
        "works_imported": _int(import_stats.get("works")),
        "attempts": _int(job.retry_count),
        "error_code": reason_code,
        "error_excerpt": (error or "")[:500] or None,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "detail": {"outcome": outcome or None, "import_stats": import_stats or None},
        "updated_at": _now(),
    }
    stmt = insert(RepositorySyncReceipt).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_repository_sync_receipts_download_job",
        set_={
            key: value
            for key, value in values.items()
            if key not in {"source_download_job_id", "repository_id"}
        } | {
            "recovered": case(
                (
                    RepositorySyncReceipt.status.in_({"failed", "stale"})
                    & (terminal_status == "complete"),
                    True,
                ),
                else_=RepositorySyncReceipt.recovered,
            ),
            "recovered_at": case(
                (
                    RepositorySyncReceipt.status.in_({"failed", "stale"})
                    & (terminal_status == "complete"),
                    _now(),
                ),
                else_=RepositorySyncReceipt.recovered_at,
            ),
        },
    ).returning(RepositorySyncReceipt)
    receipt = (await db.execute(stmt)).scalar_one()
    from app.services.search_projection_outbox import request_search_projection

    await request_search_projection(db, subscription_ids=[job.subscription_id])
    return receipt


async def reconcile_task_truth(
    db: AsyncSession,
    *,
    dry_run: bool = True,
    limit: int = 500,
) -> dict[str, Any]:
    """Reconcile TaskRun projections with their authoritative domain rows."""

    tasks = list(
        (
            await db.execute(
                select(TaskRun)
                .where(
                    or_(
                        TaskRun.status.in_(ACTIVE_TASK_STATUSES),
                        TaskRun.attention_state == "open",
                    )
                )
                .order_by(TaskRun.created_at.asc(), TaskRun.id.asc())
                .limit(max(1, min(limit, 2000)))
            )
        ).scalars()
    )
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "scanned": len(tasks),
        "corrected": 0,
        "orphaned": 0,
        "parent_child_conflicts": 0,
        "recovered": 0,
        "issues": [],
    }
    projection_subscription_ids: set[UUID] = set()
    task_service = TaskService(db)

    expected_indexes = {
        "works",
        "creators",
        "subscriptions",
        "repositories",
        "tags",
    }
    search_states = list((await db.execute(select(SearchIndexState))).scalars())
    healthy_search_indexes = (
        {state.index_uid for state in search_states} == expected_indexes
        and all(
            state.status == "ready"
            and state.database_generation == state.indexed_generation
            and state.database_document_count == state.index_document_count
            and not state.last_error
            for state in search_states
        )
    )

    async def resolve_open_attention(
        task: TaskRun,
        *,
        resolution: str,
        receipt: RepositorySyncReceipt | None = None,
    ) -> None:
        issue = {
            "task_id": str(task.id),
            "subject_type": task.subject_type,
            "subject_id": str(task.subject_id) if task.subject_id else None,
            "from_status": task.status,
            "to_status": task.status,
            "reason_code": task.reason_code,
            "resolution": resolution,
        }
        report["issues"].append(issue)
        report["corrected"] += 1
        report["recovered"] += 1
        if dry_run:
            return
        recovered_at = _now()
        if receipt is not None:
            receipt.recovered = True
            receipt.recovered_at = receipt.recovered_at or recovered_at
            source = await db.get(SubscriptionSource, receipt.repository_id)
            if source is not None:
                projection_subscription_ids.add(source.subscription_id)
        await task_service.update_task(task, attention_state="resolved")
        await task_service.add_event(
            task,
            "recovered",
            from_status=task.status,
            to_status=task.status,
            message=resolution,
            payload={"source": "reconciliation", "resolution": resolution},
        )

    async def later_success_for(
        task: TaskRun,
        domain_job: DownloadJob | ImportJob | None,
    ) -> tuple[RepositorySyncReceipt | None, RepositorySyncReceipt | None]:
        parent = domain_job
        if isinstance(domain_job, ImportJob):
            parent = await db.get(DownloadJob, domain_job.download_job_id)
        if not isinstance(parent, DownloadJob) or not parent.subscription_source_id:
            return None, None
        current_receipt = (
            await db.execute(
                select(RepositorySyncReceipt).where(
                    RepositorySyncReceipt.source_download_job_id == parent.id
                )
            )
        ).scalar_one_or_none()
        if current_receipt is None and parent.status in {"failed", "stale", "cancelled"}:
            current_receipt = await upsert_repository_sync_receipt(
                db,
                parent,
                status=parent.status,
            )
        baseline = task.finished_at or task.updated_at or task.created_at
        # An import retry can succeed under the same parent DownloadJob.  In
        # that case the parent receipt is updated to the successful terminal
        # outcome, while the older child TaskRun correctly remains stale until
        # reconciliation closes its attention window.  Require both the later
        # successful child and the durable complete receipt so a prematurely
        # completed parent can never hide a real import failure.
        if isinstance(domain_job, ImportJob) and current_receipt is not None:
            later_successful_import = (
                await db.execute(
                    select(ImportJob.id)
                    .where(
                        ImportJob.download_job_id == parent.id,
                        ImportJob.status == "complete",
                        ImportJob.created_at > domain_job.created_at,
                    )
                    .order_by(ImportJob.created_at.asc(), ImportJob.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if (
                later_successful_import is not None
                and current_receipt.status == "complete"
                and current_receipt.finished_at > baseline
            ):
                return current_receipt, current_receipt
        later_receipt = (
            await db.execute(
                select(RepositorySyncReceipt)
                .where(
                    RepositorySyncReceipt.repository_id == parent.subscription_source_id,
                    RepositorySyncReceipt.status == "complete",
                    RepositorySyncReceipt.finished_at > baseline,
                    RepositorySyncReceipt.source_download_job_id != parent.id,
                )
                .order_by(RepositorySyncReceipt.finished_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return current_receipt, later_receipt

    for task in tasks:
        target_status: str | None = None
        reason_code: str | None = None
        message: str | None = None
        domain_job: DownloadJob | ImportJob | None = None

        if task.subject_type == "download_job" and task.subject_id:
            domain_job = await db.get(DownloadJob, task.subject_id)
        elif task.subject_type == "import_job" and task.subject_id:
            domain_job = await db.get(ImportJob, task.subject_id)

        if task.attention_state == "open":
            if (
                task.reason_code == "orphaned_subject"
                and domain_job is None
                and not _rq_reference_is_active(task)
            ):
                await resolve_open_attention(
                    task,
                    resolution="Referenced domain object no longer exists; no retry is possible",
                )
                continue
            if task.kind == "admin" and task.operation_type == "admin-search-reindex":
                if healthy_search_indexes:
                    await resolve_open_attention(
                        task,
                        resolution="Superseded by a healthy, generation-consistent five-index rebuild",
                    )
                    continue
            if task.status in ATTENTION_TASK_STATUSES and domain_job is not None:
                old_receipt, later_receipt = await later_success_for(task, domain_job)
                if later_receipt is not None:
                    await resolve_open_attention(
                        task,
                        resolution=(
                            "Superseded by successful repository sync "
                            f"{later_receipt.source_download_job_id}"
                        ),
                        receipt=old_receipt,
                    )
                    continue

        if task.subject_type == "download_job" and task.subject_id:
            if domain_job is None:
                target_status = "stale"
                reason_code = "orphaned_subject"
                message = "Task references a download job that no longer exists"
                report["orphaned"] += 1
            elif domain_job.status == "importing":
                child = (
                    await db.execute(
                        select(ImportJob)
                        .where(ImportJob.download_job_id == domain_job.id)
                        .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if child and child.status in {"failed", "stale", "cancelled"}:
                    target_status = "failed" if child.status == "failed" else "stale"
                    reason_code = "child_import_failed" if child.status == "failed" else "lost_heartbeat"
                    message = f"Child import job {child.id} is {child.status}"
                    report["parent_child_conflicts"] += 1
            elif domain_job.status not in {"enqueued", "downloading", "downloaded", "importing", "paused", "recovering"}:
                target_status = "complete" if domain_job.status == "complete" else domain_job.status
                reason_code = infer_reason_code(target_status, domain_job.error_log)
                message = f"Download job is authoritative: {domain_job.status}"
        elif task.subject_type == "import_job" and task.subject_id:
            if domain_job is None:
                target_status = "stale"
                reason_code = "orphaned_subject"
                message = "Task references an import job that no longer exists"
                report["orphaned"] += 1
            elif domain_job.status not in {"enqueued", "running", "importing", "paused", "recovering"}:
                target_status = "complete" if domain_job.status == "complete" else domain_job.status
                reason_code = infer_reason_code(target_status, domain_job.error_log)
                message = f"Import job is authoritative: {domain_job.status}"

        if not target_status or target_status == task.status:
            continue
        issue = {
            "task_id": str(task.id),
            "subject_type": task.subject_type,
            "subject_id": str(task.subject_id) if task.subject_id else None,
            "from_status": task.status,
            "to_status": target_status,
            "reason_code": reason_code,
            "message": message,
        }
        report["issues"].append(issue)
        report["corrected"] += 1
        if dry_run:
            continue

        if isinstance(domain_job, DownloadJob) and domain_job.status == "importing" and target_status in {"failed", "stale"}:
            domain_job.status = target_status
            domain_job.error_log = message
        await task_service.update_task(
            task,
            status=target_status,
            error=message,
            resource_state="yielded",
            resource_reason=None,
            reason_code=reason_code,
        )
        await task_service.add_event(
            task,
            "reconciled",
            from_status=issue["from_status"],
            to_status=target_status,
            message=message,
            payload={"source": "reconciliation", "reason_code": reason_code},
        )
        if isinstance(domain_job, DownloadJob) and target_status in {"failed", "stale", "complete"}:
            await upsert_repository_sync_receipt(db, domain_job, status=target_status)

    if dry_run:
        await db.rollback()
    else:
        if projection_subscription_ids:
            from app.services.search_projection_outbox import request_search_projection

            await request_search_projection(
                db,
                subscription_ids=projection_subscription_ids,
            )
        await _write_maintenance_audit(
            db,
            "task_reconciliation",
            [issue["task_id"] for issue in report["issues"]],
            report,
        )
        await db.commit()
    return report


async def compact_terminal_tasks(
    db: AsyncSession,
    *,
    dry_run: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """Delete compactable operational rows only after their domain receipt exists."""

    now = _now()
    receipt_exists = exists().where(
        RepositorySyncReceipt.source_download_job_id == DownloadJob.id
    )
    guarded_without_receipt = int(
        (
            await db.execute(
                select(func.count(TaskRun.id))
                .join(
                    DownloadJob,
                    and_(
                        TaskRun.subject_type == "download_job",
                        TaskRun.subject_id == DownloadJob.id,
                    ),
                )
                .where(
                    TaskRun.compactable_at.is_not(None),
                    TaskRun.compactable_at <= now,
                    DownloadJob.status.in_(COMPACTABLE_DOWNLOAD_STATUSES),
                    DownloadJob.subscription_source_id.is_not(None),
                    ~receipt_exists,
                )
            )
        ).scalar_one()
    )
    downloadable = exists().where(
        DownloadJob.id == TaskRun.subject_id,
        DownloadJob.status.in_(COMPACTABLE_DOWNLOAD_STATUSES),
        or_(
            DownloadJob.subscription_source_id.is_(None),
            exists().where(
                RepositorySyncReceipt.source_download_job_id == DownloadJob.id
            ),
        ),
    )
    download_missing = ~exists().where(DownloadJob.id == TaskRun.subject_id)
    tasks = list(
        (
            await db.execute(
                select(TaskRun)
                .where(
                    TaskRun.compactable_at.is_not(None),
                    TaskRun.compactable_at <= now,
                    or_(TaskRun.subject_type.is_(None), TaskRun.subject_type != "import_job"),
                    or_(
                        TaskRun.subject_type.is_(None),
                        TaskRun.subject_type != "download_job",
                        TaskRun.subject_id.is_(None),
                        download_missing,
                        downloadable,
                    ),
                )
                .order_by(TaskRun.compactable_at.asc(), TaskRun.id.asc())
                .limit(max(1, min(limit, 1000)))
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    report = {
        "dry_run": dry_run,
        "matched": len(tasks),
        "deleted_tasks": 0,
        "deleted_download_jobs": 0,
        "deleted_import_jobs": 0,
        "skipped_without_receipt": guarded_without_receipt,
    }

    selected_task_ids = {task.id for task in tasks}
    candidate_download_ids = {
        task.subject_id
        for task in tasks
        if task.subject_type == "download_job" and task.subject_id is not None
    }
    download_ids: set[UUID] = set()
    if candidate_download_ids:
        download_ids.update(
            (
                await db.execute(
                    select(DownloadJob.id).where(
                        DownloadJob.id.in_(candidate_download_ids),
                        DownloadJob.status.in_(COMPACTABLE_DOWNLOAD_STATUSES),
                        or_(
                            DownloadJob.subscription_source_id.is_(None),
                            exists().where(
                                RepositorySyncReceipt.source_download_job_id
                                == DownloadJob.id
                            ),
                        ),
                    )
                )
            ).scalars()
        )
    import_ids: set[UUID] = set()
    if download_ids:
        import_ids.update(
            (
                await db.execute(
                    select(ImportJob.id).where(ImportJob.download_job_id.in_(download_ids))
                )
            ).scalars()
        )
    child_task_ids: set[UUID] = set()
    if selected_task_ids or import_ids:
        child_task_ids.update(
            (
                await db.execute(
                    select(TaskRun.id).where(
                        or_(
                            TaskRun.parent_task_id.in_(selected_task_ids)
                            if selected_task_ids
                            else False,
                            and_(
                                TaskRun.subject_type == "import_job",
                                TaskRun.subject_id.in_(import_ids),
                            )
                            if import_ids
                            else False,
                        )
                    )
                )
            ).scalars()
        )

    all_task_ids = selected_task_ids | child_task_ids
    compacted_ids = [str(task_id) for task_id in sorted(all_task_ids, key=str)]
    report["deleted_tasks"] = len(all_task_ids)
    report["deleted_import_jobs"] = len(import_ids)
    report["deleted_download_jobs"] = len(download_ids)
    if not dry_run:
        # Set-based deletes avoid one FK/index scan per historical download.
        # SKIP LOCKED above also lets the periodic compactor and an operator
        # repair run safely make progress on disjoint batches.
        if all_task_ids:
            await db.execute(
                delete(TaskRun)
                .where(TaskRun.id.in_(all_task_ids))
                .execution_options(synchronize_session="fetch")
            )
        if import_ids:
            await db.execute(
                delete(ImportJob)
                .where(ImportJob.id.in_(import_ids))
                .execution_options(synchronize_session="fetch")
            )
        if download_ids:
            await db.execute(
                delete(DownloadJob)
                .where(DownloadJob.id.in_(download_ids))
                .execution_options(synchronize_session="fetch")
            )

    if dry_run:
        await db.rollback()
    else:
        await _write_maintenance_audit(
            db,
            "task_compaction",
            compacted_ids,
            report,
        )
        await db.commit()
    return report


async def restore_missed_subscription_slot(
    db: AsyncSession,
    *,
    slot_at: datetime,
    source_ids: list[UUID],
    dry_run: bool = True,
) -> dict[str, Any]:
    """Restore a known missed slot without reopening already-attempted work.

    Requiring an exported source-id allowlist keeps this maintenance operation
    incident-scoped. Repeating the same call is a no-op because restored rows no
    longer have ``next_sync_at > slot_at``.
    """

    slot = slot_at.replace(tzinfo=timezone.utc) if slot_at.tzinfo is None else slot_at.astimezone(timezone.utc)
    identities = list(dict.fromkeys(source_ids))
    if not identities:
        return {"dry_run": dry_run, "requested": 0, "matched": 0, "issues": []}
    rows = list((await db.execute(
        select(SubscriptionSource)
        .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
        .where(
            SubscriptionSource.id.in_(identities),
            Subscription.is_active.is_(True),
            Subscription.sync_enabled.is_(True),
            SubscriptionSource.is_enabled.is_(True),
            SubscriptionSource.next_sync_at.is_not(None),
            SubscriptionSource.next_sync_at > slot,
            or_(
                SubscriptionSource.last_attempted_at.is_(None),
                SubscriptionSource.last_attempted_at < slot,
            ),
            or_(
                SubscriptionSource.last_synced_at.is_(None),
                SubscriptionSource.last_synced_at < slot,
            ),
        )
        .order_by(SubscriptionSource.id)
        .with_for_update(skip_locked=True)
    )).scalars())
    issues = [
        {
            "source_id": str(source.id),
            "subscription_id": str(source.subscription_id),
            "from_next_sync_at": source.next_sync_at.isoformat() if source.next_sync_at else None,
            "to_next_sync_at": slot.isoformat(),
        }
        for source in rows
    ]
    report = {
        "dry_run": dry_run,
        "requested": len(identities),
        "matched": len(rows),
        "slot_at": slot.isoformat(),
        "issues": issues,
    }
    if dry_run:
        await db.rollback()
        return report
    for source in rows:
        source.next_sync_at = slot
    await _write_maintenance_audit(
        db,
        "subscription_slot_restore",
        [str(source.id) for source in rows],
        report,
    )
    await db.commit()
    return report


async def operations_overview(
    db: AsyncSession,
    *,
    view: str = "attention",
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Return one compact operations feed for desktop and mobile clients."""

    if view not in {"attention", "active", "resolved"}:
        raise ValueError("view must be attention, active, or resolved")
    source_anomalies: list[dict[str, Any]] = []
    if view == "attention":
        from app.providers import registry
        from app.services.settings import get_scheduler_config

        scheduler_config = await get_scheduler_config(db)
        scan_minutes = max(
            5,
            int(scheduler_config.get("scheduler_scan_interval_minutes") or 60),
        )
        overdue_cutoff = _now() - timedelta(minutes=scan_minutes * 2)
        source_rows = (
            await db.execute(
                select(SubscriptionSource, Subscription)
                .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
                .where(
                    SubscriptionSource.is_enabled.is_(True),
                    Subscription.is_active.is_(True),
                    Subscription.sync_enabled.is_(True),
                )
                .order_by(SubscriptionSource.updated_at.desc(), SubscriptionSource.id.desc())
                .limit(500)
            )
        ).all()
        for repository, subscription in source_rows:
            reason_code = None
            severity = "warning"
            summary = None
            occurred_at = repository.updated_at or repository.created_at or _now()
            if repository.auth_healthy is False:
                reason_code = "auth_unhealthy"
                severity = "critical"
                summary = repository.auth_error_reason or "Repository authentication is unhealthy"
                occurred_at = repository.last_auth_checked_at or occurred_at
            else:
                try:
                    provider = registry.get(repository.source)
                    normalized = (
                        provider.normalize_url(repository.source_url)
                        if repository.source_url
                        else None
                    )
                    candidate = normalized or repository.source_url
                    if (
                        provider.capabilities.can_download
                        and not (candidate and provider.validate_url(candidate))
                    ):
                        reason_code = "url_invalid"
                        summary = "Repository URL is invalid for its source provider"
                except Exception:
                    reason_code = "url_invalid"
                    summary = "Repository source provider or URL is invalid"
            if (
                reason_code is None
                and scheduler_config.get("scheduler_enabled", True)
                and repository.next_sync_at is not None
                and repository.next_sync_at < overdue_cutoff
            ):
                reason_code = "scheduler_overdue"
                summary = "Repository synchronization is overdue"
                occurred_at = repository.next_sync_at
            if reason_code is None:
                continue
            source_anomalies.append({
                "id": f"repository:{repository.id}:{reason_code}",
                "type": "repository",
                "severity": severity,
                "status": "open",
                "reason_code": reason_code,
                "title": subscription.name or repository.source,
                "summary": summary,
                "repository_id": str(repository.id),
                "task_id": None,
                "occurred_at": occurred_at.isoformat(),
                "source": repository.source,
                "available_actions": ["open_repository", "copy_diagnostics"],
                "task": None,
            })
    filters = [TaskRun.kind != "account"]
    if view == "attention":
        filters.append(TaskRun.attention_state == "open")
    elif view == "resolved":
        filters.extend([
            TaskRun.attention_state.in_({"resolved", "acknowledged"}),
            TaskRun.resolved_at >= _now() - RESOLVED_RETENTION,
        ])
    else:
        filters.append(TaskRun.status.in_(ACTIVE_TASK_STATUSES))

    task_total = int((await db.execute(select(func.count(TaskRun.id)).where(*filters))).scalar_one())
    tasks = list(
        (
            await db.execute(
                select(TaskRun)
                .where(*filters)
                .order_by(TaskRun.updated_at.desc(), TaskRun.id.desc())
                .offset(0 if view == "attention" else max(0, offset))
                .limit(
                    max(1, min(max(limit, offset + limit), 500))
                    if view == "attention"
                    else max(1, min(limit, 100))
                )
            )
        ).scalars()
    )
    counts = dict(
        (
            await db.execute(
                select(TaskRun.attention_state, func.count(TaskRun.id))
                .where(TaskRun.kind != "account")
                .group_by(TaskRun.attention_state)
            )
        ).all()
    )
    active_count = int(
        (
            await db.execute(
                select(func.count(TaskRun.id)).where(
                    TaskRun.kind != "account",
                    TaskRun.status.in_(ACTIVE_TASK_STATUSES),
                )
            )
        ).scalar_one()
    )
    severe_reason_codes = {
        "out_of_memory",
        "state_conflict",
        "orphaned_subject",
        "auth_unhealthy",
    }
    severe_count = int(
        (
            await db.execute(
                select(func.count(TaskRun.id)).where(
                    TaskRun.kind != "account",
                    TaskRun.attention_state == "open",
                    TaskRun.reason_code.in_(severe_reason_codes),
                )
            )
        ).scalar_one()
    )
    resource_limited_count = int(
        (
            await db.execute(
                select(func.count(TaskRun.id)).where(
                    TaskRun.kind != "account",
                    TaskRun.status.in_(ACTIVE_TASK_STATUSES),
                    TaskRun.resource_state == "waiting",
                    TaskRun.resource_reason.is_not(None),
                )
            )
        ).scalar_one()
    )

    items = []
    for task in tasks:
        payload = task_payload(task)
        meta = task.meta or {}
        items.append({
            "id": str(task.id),
            "type": "task",
            "severity": "critical" if task.reason_code in {"out_of_memory", "state_conflict", "orphaned_subject"} else "warning",
            "status": task.attention_state if view != "active" else "active",
            "reason_code": task.reason_code,
            "title": task.title or task.operation_type or task.kind,
            "summary": task.error_log or task.resource_reason or task.progress_stage,
            "repository_id": meta.get("subscription_source_id"),
            "task_id": str(task.id),
            "occurred_at": (task.updated_at or task.created_at).isoformat(),
            "source": task.source,
            "available_actions": (
                ["acknowledge", "open_repository", "copy_diagnostics"]
                if task.attention_state == "open" and task.reason_code in {
                    "download_staging_conflict",
                    "download_staging_manifest_error",
                }
                else ["retry", "acknowledge", "open_repository", "copy_diagnostics"]
                if task.attention_state == "open"
                else ["pause", "resume", "open_repository"]
            ),
            "task": payload,
        })
    if view == "attention":
        items.extend(source_anomalies)
        items.sort(key=lambda item: item["occurred_at"], reverse=True)
        items = items[max(0, offset):max(0, offset) + max(1, min(limit, 100))]
    total = task_total + (len(source_anomalies) if view == "attention" else 0)
    open_count = int(counts.get("open", 0)) + len(source_anomalies)
    source_critical = sum(1 for item in source_anomalies if item["severity"] == "critical")
    return {
        "view": view,
        "total": total,
        "summary": {
            "attention": open_count,
            "critical": severe_count + source_critical,
            "warning": max(0, open_count - severe_count - source_critical),
            "resolved": int(counts.get("resolved", 0)) + int(counts.get("acknowledged", 0)),
            "active": active_count,
            "resource_limited": resource_limited_count,
        },
        "items": items,
    }
