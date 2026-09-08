"""One task action policy; HTTP visibility remains a prerequisite, never inferred here."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, or_

from app.models import DownloadJob, ImportJob, TaskRun, StorageArtifact, User, SubscriptionSource, Subscription, RemoteAccount
from app.models.remote_discovery import UserSubscription, UserSubscriptionSource
from app.models.task_state import DOWNLOAD_PAUSABLE_STATUSES, DOWNLOAD_CANCELLABLE_STATUSES, IMPORT_PAUSABLE_STATUSES, IMPORT_CANCELLABLE_STATUSES
from app.schemas.task_actions import TaskCapabilities

ACTIONS = ("retry", "repeat_sync", "pause", "resume", "cancel", "delete", "acknowledge")
TERMINAL = {"complete", "failed", "stale", "cancelled"}
ACTIVE = {"pending", "enqueued", "running", "downloading", "downloaded", "importing", "paused", "recovering"}



def _dispatch_options(task):
    dispatch = (task.meta or {}).get("admin_dispatch")
    options = dispatch.get("options") if isinstance(dispatch, dict) else None
    return options if isinstance(options, dict) else {}


def decide(kind, subject, *, task=None, permitted=True, active_child=False, leased=False, repeat_member=False):
    reasons = {action: "operation_not_supported" for action in ACTIONS}
    allowed = set()
    if not permitted:
        return TaskCapabilities(disabled_reasons={action: "permission_denied" for action in ACTIONS})
    if subject is None:
        # A missing domain row prevents execution, not acknowledging the
        # independently visible TaskRun anomaly.
        acknowledge = task is not None and task.attention_state in {"open", "resolved"}
        return TaskCapabilities(available_actions=["acknowledge"] if acknowledge else [],
            disabled_reasons={action: "subject_missing" for action in ACTIONS if not (acknowledge and action == "acknowledge")})
    status = subject.status
    if kind in {"download", "import"}:
        reasons.update({action: "invalid_task_state" for action in ACTIONS})
        if status in {"failed", "stale"}:
            allowed.add("retry")
        if status == "complete":
            reasons["retry"] = "completed_sync_requires_repeat" if kind == "download" else "already_terminal"
            if kind == "download":
                if repeat_member:
                    allowed.add("repeat_sync")
                else:
                    reasons["repeat_sync"] = "membership_unavailable"
        if status in (DOWNLOAD_PAUSABLE_STATUSES if kind == "download" else IMPORT_PAUSABLE_STATUSES):
            allowed.add("pause")
        if status == "paused":
            allowed.add("resume")
        if status in (DOWNLOAD_CANCELLABLE_STATUSES if kind == "download" else IMPORT_CANCELLABLE_STATUSES):
            allowed.add("cancel")
        if status in TERMINAL and not active_child and not leased:
            allowed.add("delete")
        else:
            reasons["delete"] = "active_import" if active_child else "execution_unsettled" if leased else "active_work_cancel_first"
        if leased or active_child:
            allowed.discard("retry")
            reasons["retry"] = "execution_unsettled" if leased else "active_import"
        if kind == "download":
            events = (subject.manifest or {}).get("events") or []
            conflict = -1
            resolved = -1
            for index, event in enumerate(events):
                if isinstance(event, dict) and event.get("event") == "staging_conflict" and event.get("conflict_details"):
                    conflict = index
                if isinstance(event, dict) and event.get("event") == "staging_conflict_resolved":
                    resolved = index
            if conflict > resolved:
                allowed.discard("retry")
                reasons["retry"] = "conflict_resolution_required"
        reason = getattr(task, "reason_code", None)
        if reason in {"import_completion_evidence_missing", "import_completion_identity_mismatch"}:
            allowed.discard("retry")
            reasons["retry"] = reason
    elif kind == "admin":
        from app.services.operations import ADMIN_OPERATION_REGISTRY, _registered_spec

        operation = subject.operation_type
        dispatch = (subject.meta or {}).get("admin_dispatch")
        valid = False
        if (operation in ADMIN_OPERATION_REGISTRY and isinstance(dispatch, dict)
                and isinstance(dispatch.get("scope_key"), str) and isinstance(dispatch.get("queue_name"), str)):
            try:
                _registered_spec(operation, scope_key=dispatch.get("scope_key", ""), queue_name=dispatch.get("queue_name", ""))
                valid = (
                    dispatch.get("operation_type") == operation
                    and isinstance(dispatch.get("options"), dict)
                    and int(dispatch.get("attempt") or 0) == subject.attempts
                    and subject.attempts > 0
                    and bool(subject.rq_job_id)
                    and dispatch.get("rq_job_id") == subject.rq_job_id
                )
            except (ValueError, TypeError, HTTPException):
                pass
        if operation == "subscription-sync-batch":
            from app.services.tasks import is_global_subscription_batch

            reasons = {action: "batch_results_preserved" for action in ACTIONS}
            if valid and is_global_subscription_batch(subject) and status in ACTIVE:
                allowed.add("cancel")
        elif valid:
            if status in {"failed", "stale", "cancelled"}:
                allowed.add("retry")
            else:
                reasons["retry"] = "invalid_task_state"
        else:
            reasons["retry"] = "legacy_operation_read_only"
    if task is not None and task.attention_state in {"open", "resolved"}:
        allowed.add("acknowledge")
    return TaskCapabilities(available_actions=[a for a in ACTIONS if a in allowed], disabled_reasons={a: reasons[a] for a in ACTIONS if a not in allowed})


async def enrich_actions(db, rows, *, user=None, user_id=None, domain_kind=None, trusted=False):
    """Load page facts with set-based queries; attach only transient response fields."""
    rows = list(rows)
    if not rows:
        return rows
    if user is None and user_id is not None:
        user = await db.get(User, user_id)
    permissions = set(getattr(user, "permissions", None) or [])
    privileged = bool(getattr(user, "is_admin", False))
    domain_allowed = trusted or privileged or "tasks" in permissions
    downloads, imports, tasks = {}, {}, {}
    if domain_kind:
        target_ids = [row.id for row in rows]
        task_rows = list((await db.execute(select(TaskRun).where(TaskRun.subject_type == f"{domain_kind}_job", TaskRun.subject_id.in_(target_ids)))).scalars())
    else:
        task_rows = rows
    tasks = {(t.subject_type, t.subject_id): t for t in task_rows}
    download_ids = {row.id for row in rows} if domain_kind == "download" else {t.subject_id for t in task_rows if t.subject_type == "download_job"}
    import_ids = {row.id for row in rows} if domain_kind == "import" else {t.subject_id for t in task_rows if t.subject_type == "import_job"}
    if import_ids:
        imports = {
            j.id: j for j in (await db.execute(select(ImportJob).where(ImportJob.id.in_(import_ids)).execution_options(populate_existing=True))).scalars()
        }
        download_ids.update(j.download_job_id for j in imports.values())
    if download_ids:
        downloads = {
            j.id: j for j in (await db.execute(select(DownloadJob).where(DownloadJob.id.in_(download_ids)).execution_options(populate_existing=True))).scalars()
        }
        children = list(
            (await db.execute(select(ImportJob).where(ImportJob.download_job_id.in_(download_ids)).execution_options(populate_existing=True))).scalars()
        )
        imports.update({j.id: j for j in children})
    active_parents = {j.download_job_id for j in imports.values() if j.status in ACTIVE or j.execution_token is not None}
    leased_parents = (
        set(
            (
                await db.execute(
                    select(StorageArtifact.download_job_id)
                    .where(StorageArtifact.download_job_id.in_(download_ids), StorageArtifact.lease_expires_at > datetime.now(timezone.utc))
                    .distinct()
                )
            ).scalars()
        )
        if download_ids
        else set()
    )
    repeat_sources = set()
    if user is not None and downloads:
        from app.services.subscription_membership import membership_source_is_usable
        from app.providers import registry

        candidates = (
            await db.execute(
                select(UserSubscriptionSource, SubscriptionSource, RemoteAccount)
                .join(UserSubscription, UserSubscription.id == UserSubscriptionSource.user_subscription_id)
                .join(SubscriptionSource, SubscriptionSource.id == UserSubscriptionSource.subscription_source_id)
                .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
                .outerjoin(RemoteAccount, RemoteAccount.id == UserSubscriptionSource.remote_account_id)
                .where(
                    UserSubscription.user_id == user.id,
                    UserSubscription.is_active.is_(True),
                    Subscription.is_active.is_(True),
                    SubscriptionSource.id.in_([j.subscription_source_id for j in downloads.values() if j.subscription_source_id]),
                )
            )
        ).all()
        for binding, source, account in candidates:
            try:
                downloadable = registry.get(source.source).capabilities.can_download
            except KeyError:
                downloadable = False
            if downloadable and source.source_url and membership_source_is_usable(binding, account, source=source.source):
                repeat_sources.add(source.id)
    scan_ids = set()
    for task in task_rows:
        if task.operation_type == "asset-dedup-scan":
            try:
                scan_ids.add(UUID(str(_dispatch_options(task).get("scan_id"))))
            except (TypeError, ValueError):
                pass
    scans = {}
    if scan_ids:
        from app.models.asset_dedup import AssetDedupScan

        scans = {str(scan.id): scan.status for scan in (await db.execute(select(AssetDedupScan).where(AssetDedupScan.id.in_(scan_ids)))).scalars()}
    child_tasks = {}
    if imports:
        child_tasks = {
            t.subject_id: t for t in (await db.execute(select(TaskRun).where(TaskRun.subject_type == "import_job", TaskRun.subject_id.in_(imports)))).scalars()
        }
    for row in rows:
        task = row if domain_kind is None else tasks.get((f"{domain_kind}_job", row.id))
        kind = domain_kind or row.kind
        subject = (
            row
            if domain_kind
            else downloads.get(row.subject_id)
            if row.subject_type == "download_job"
            else imports.get(row.subject_id)
            if row.subject_type == "import_job"
            else row
        )
        if task and task.subject_type in {"download_job", "import_job"}:
            kind = "download" if task.subject_type == "download_job" else "import"
        permitted = domain_allowed
        if kind == "admin":
            from app.services.operations import admin_operation_required_permission

            required = admin_operation_required_permission(row.operation_type)
            permitted = trusted or privileged or required in permissions or (row.operation_type == "subscription-sync-batch" and "system" in permissions)
        parent_id = subject.id if subject is not None and kind == "download" else getattr(subject, "download_job_id", None)
        caps = decide(
            kind,
            subject,
            task=task,
            permitted=permitted,
            active_child=kind == "download" and parent_id in active_parents,
            leased=parent_id in leased_parents or (kind == "import" and getattr(subject, "execution_token", None) is not None),
            repeat_member=getattr(subject, "subscription_source_id", None) in repeat_sources,
        )
        if kind == "download" and subject is not None:
            failed_children = sorted(
                (child for child in imports.values() if child.download_job_id == subject.id and child.status in {"failed", "stale"}),
                key=lambda child: (child.created_at, child.id),
                reverse=True,
            )
            if failed_children and "retry" in caps.available_actions:
                child = failed_children[0]
                delegated = decide("import", child, task=child_tasks.get(child.id), leased=child.execution_token is not None or parent_id in leased_parents)
                if "retry" not in delegated.available_actions:
                    caps.available_actions.remove("retry")
                    caps.disabled_reasons["retry"] = delegated.disabled_reasons["retry"]
        if task and task.operation_type == "asset-dedup-scan":
            scan_id = str(_dispatch_options(task).get("scan_id"))
            if scans.get(scan_id) not in {"pending", "running", "failed"}:
                caps.available_actions = [a for a in caps.available_actions if a != "retry"]
                caps.disabled_reasons["retry"] = "retry_evidence_unavailable"
        if not domain_allowed and "acknowledge" in caps.available_actions:
            caps.available_actions.remove("acknowledge")
            caps.disabled_reasons["acknowledge"] = "permission_denied"
        row.available_actions = caps.available_actions
        row.disabled_reasons = caps.disabled_reasons
    return rows


def require_action(subject, action):
    if action not in subject.available_actions:
        raise HTTPException(
            409,
            detail={
                "code": "invalid_task_action",
                "action": action,
                "status": subject.status,
                "reason": subject.disabled_reasons.get(action, "operation_not_supported"),
                "available_actions": subject.available_actions,
                "disabled_reasons": subject.disabled_reasons,
            },
        )


async def require_job_action(db, job, kind, action, *, already_locked=False):
    if hasattr(db, "execute"):
        # Retry resets artifact assignments, so its locks precede the parent.
        # Pause/resume/cancel do not mutate artifacts: parent -> children is
        # also the existing batch cleanup order; do not reverse that order by
        # taking unrelated artifact locks after a cleanup already has a parent.
        parent_id = job.id if kind == "download" else job.download_job_id
        if not already_locked:
            if action in {"retry", "delete"}:
                await db.execute(select(StorageArtifact.id).where(StorageArtifact.download_job_id == parent_id).order_by(StorageArtifact.id).with_for_update())
            await db.execute(select(DownloadJob).where(DownloadJob.id == parent_id).with_for_update().execution_options(populate_existing=True))
            await db.execute(
                select(ImportJob)
                .where(ImportJob.download_job_id == parent_id)
                .order_by(ImportJob.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        await enrich_actions(db, [job], domain_kind=kind, trusted=True)
    else:
        caps = decide(kind, job)
        job.available_actions, job.disabled_reasons = caps.available_actions, caps.disabled_reasons
    require_action(job, action)


async def require_admin_surface_access(db, task, user):
    """Admin routes retain module permissions and the same private visibility."""
    from app.services.operations import require_admin_operation_access
    from app.services.tasks import task_surface_visibility_condition, can_access_global_subscription_batch

    require_admin_operation_access(user, task.operation_type)
    visible = (
        await db.execute(
            select(TaskRun.id).where(
                TaskRun.id == task.id, task_surface_visibility_condition(user.id, include_global_system_tasks=can_access_global_subscription_batch(user))
            )
        )
    ).scalar_one_or_none()
    if visible is None:
        raise HTTPException(404, detail="Operation not found")
