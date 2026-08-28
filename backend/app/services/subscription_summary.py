"""Authoritative, page-batched subscription operational summaries."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.repository_sync_receipt import RepositorySyncReceipt
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.task_run import TaskRun
from app.models.remote_discovery import UserSubscription, UserSubscriptionSource
from app.providers import registry
from app.services.operation_attention import ACTIVE_TASK_STATUSES
from app.services.tasks import task_visibility_condition
from app.services.settings import get_scheduler_config


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _source_blocked(source: SubscriptionSource) -> bool:
    if source.auth_healthy is False or not source.source_url:
        return True
    try:
        provider = registry.get(source.source)
    except KeyError:
        return True
    return (
        not provider.capabilities.can_download
        or not provider.validate_url(source.source_url)
    )


async def subscription_summaries(
    db: AsyncSession,
    subscription_ids: list[UUID],
    *,
    user_id: int | None = None,
) -> dict:
    """Load all state for one list page with four bounded batch queries."""

    ordered_ids = list(dict.fromkeys(subscription_ids))
    now = datetime.now(timezone.utc)
    if not ordered_ids:
        return {"items": [], "updated_at": now}

    config = await get_scheduler_config(db)
    subscriptions = list((await db.execute(
        select(Subscription).where(Subscription.id.in_(ordered_ids))
    )).scalars())
    by_id = {subscription.id: subscription for subscription in subscriptions}

    memberships_by_subscription: dict[UUID, UserSubscription] = {}
    bindings_by_source: dict[UUID, UserSubscriptionSource] = {}
    if user_id is not None:
        memberships = list((await db.execute(
            select(UserSubscription).where(
                UserSubscription.user_id == user_id,
                UserSubscription.subscription_id.in_(ordered_ids),
            )
        )).scalars())
        memberships_by_subscription = {
            membership.subscription_id: membership for membership in memberships
        }
        membership_ids = [membership.id for membership in memberships]
        if membership_ids:
            bindings = list((await db.execute(
                select(UserSubscriptionSource).where(
                    UserSubscriptionSource.user_subscription_id.in_(membership_ids)
                )
            )).scalars())
            bindings_by_source = {
                binding.subscription_source_id: binding for binding in bindings
            }

    source_rows = list((await db.execute(
        select(SubscriptionSource)
        .where(SubscriptionSource.subscription_id.in_(ordered_ids))
        .order_by(SubscriptionSource.subscription_id, SubscriptionSource.id)
    )).scalars())
    sources_by_subscription: dict[UUID, list[SubscriptionSource]] = defaultdict(list)
    for source in source_rows:
        sources_by_subscription[source.subscription_id].append(source)

    task_stmt = (
        select(TaskRun, DownloadJob.subscription_id, DownloadJob.subscription_source_id)
        .join(
            DownloadJob,
            and_(
                TaskRun.subject_type == "download_job",
                TaskRun.subject_id == DownloadJob.id,
            ),
        )
        .where(
            DownloadJob.subscription_id.in_(ordered_ids),
            or_(
                TaskRun.status.in_(ACTIVE_TASK_STATUSES),
                TaskRun.attention_state == "open",
            ),
        )
    )
    if user_id is not None:
        task_stmt = task_stmt.where(task_visibility_condition(user_id))
    task_rows = list((await db.execute(task_stmt)).all())
    tasks_by_subscription: dict[UUID, list[tuple[TaskRun, UUID | None]]] = defaultdict(list)
    for task, subscription_id, repository_id in task_rows:
        tasks_by_subscription[subscription_id].append((task, repository_id))
    import_task_stmt = (
        select(TaskRun, DownloadJob.subscription_id, DownloadJob.subscription_source_id)
        .join(
            ImportJob,
            and_(
                TaskRun.subject_type == "import_job",
                TaskRun.subject_id == ImportJob.id,
            ),
        )
        .join(DownloadJob, DownloadJob.id == ImportJob.download_job_id)
        .where(
            DownloadJob.subscription_id.in_(ordered_ids),
            or_(
                TaskRun.status.in_(ACTIVE_TASK_STATUSES),
                TaskRun.attention_state == "open",
            ),
        )
    )
    if user_id is not None:
        import_task_stmt = import_task_stmt.where(task_visibility_condition(user_id))
    import_task_rows = list((await db.execute(import_task_stmt)).all())
    for task, subscription_id, repository_id in import_task_rows:
        tasks_by_subscription[subscription_id].append((task, repository_id))

    # PostgreSQL DISTINCT ON returns one newest successful receipt per
    # subscription without loading long-lived receipt histories into Python.
    receipt_rows = list((await db.execute(
        select(
            SubscriptionSource.subscription_id,
            RepositorySyncReceipt,
        )
        .join(
            SubscriptionSource,
            SubscriptionSource.id == RepositorySyncReceipt.repository_id,
        )
        .where(
            SubscriptionSource.subscription_id.in_(ordered_ids),
            RepositorySyncReceipt.status == "complete",
        )
        .distinct(SubscriptionSource.subscription_id)
        .order_by(
            SubscriptionSource.subscription_id,
            RepositorySyncReceipt.finished_at.desc(),
            RepositorySyncReceipt.id.desc(),
        )
    )).all())
    receipts_by_subscription = {
        subscription_id: receipt
        for subscription_id, receipt in receipt_rows
    }

    timezone_name = str(config.get("timezone") or "UTC")
    scan_minutes = max(5, int(config.get("scheduler_scan_interval_minutes", 60)))
    overdue_cutoff = now - timedelta(minutes=scan_minutes * 2)
    items = []
    for subscription_id in ordered_ids:
        subscription = by_id.get(subscription_id)
        if subscription is None:
            continue
        membership = memberships_by_subscription.get(subscription_id)
        if user_id is not None and membership is None:
            continue
        policy = membership or subscription
        canonical_sources = sources_by_subscription.get(subscription_id, [])
        sources = (
            [source for source in canonical_sources if source.id in bindings_by_source]
            if user_id is not None
            else canonical_sources
        )
        enabled_sources = [
            source
            for source in sources
            if (
                bindings_by_source[source.id].is_enabled
                if user_id is not None
                else source.is_enabled
            )
        ]
        task_pairs = tasks_by_subscription.get(subscription_id, [])
        attention = [pair for pair in task_pairs if pair[0].attention_state == "open"]
        active = [pair for pair in task_pairs if pair[0].status in ACTIVE_TASK_STATUSES]
        receipt = receipts_by_subscription.get(subscription_id)

        if attention:
            task, repository_id = max(attention, key=lambda pair: pair[0].updated_at)
            latest_state = {
                "state": "attention",
                "status": task.status,
                "occurred_at": task.updated_at,
                "reason_code": task.reason_code,
                "repository_id": repository_id,
                "task_id": task.id,
            }
        elif active:
            task, repository_id = max(active, key=lambda pair: pair[0].updated_at)
            latest_state = {
                "state": "active",
                "status": task.status,
                "occurred_at": task.updated_at,
                "reason_code": task.resource_reason,
                "repository_id": repository_id,
                "task_id": task.id,
            }
        elif not policy.is_active:
            latest_state = {"state": "disabled", "status": "inactive"}
        else:
            configured_mode = policy.schedule_mode or "inherit"
            effective_mode = policy.schedule_mode or config.get("schedule_mode", "interval")
            if not policy.sync_enabled or effective_mode == "manual":
                latest_state = {"state": "manual", "status": "manual"}
            elif receipt is not None:
                latest_state = {
                    "state": "success",
                    "status": receipt.status,
                    "occurred_at": receipt.finished_at,
                    "outcome_code": receipt.outcome_code,
                    "repository_id": receipt.repository_id,
                    "task_id": receipt.source_task_id,
                }
            else:
                latest_state = {"state": "never_synced", "status": None}

        configured_mode = policy.schedule_mode or "inherit"
        effective_mode = policy.schedule_mode or str(config.get("schedule_mode") or "interval")
        automatic = (
            policy.is_active
            and policy.sync_enabled
            and effective_mode != "manual"
        )
        eligible_sources = enabled_sources if automatic else []
        next_times = [
            _utc(
                bindings_by_source[source.id].next_sync_at
                if user_id is not None
                else source.next_sync_at
            )
            for source in eligible_sources
            if (
                bindings_by_source[source.id].next_sync_at
                if user_id is not None
                else source.next_sync_at
            ) is not None
        ]
        due_times = [value for value in next_times if value is not None and value <= now]
        schedule = {
            "configured_mode": configured_mode,
            "effective_mode": effective_mode,
            "inherited": policy.schedule_mode in {None, "inherit"},
            "timezone": timezone_name,
            "scheduled_times": (
                policy.scheduled_times
                or str(config.get("scheduled_times") or "")
                or None
            ),
            "schedule_rule": (
                policy.schedule_rule
                if policy.schedule_mode == "calendar"
                else (
                    config.get("schedule_rule")
                    if policy.schedule_mode in {None, "inherit"}
                    and effective_mode == "calendar"
                    else None
                )
            ),
            "sync_interval_hours": int(
                policy.sync_interval_hours
                or config.get("default_sync_interval_hours", 6)
            ),
            "next_due_at": min(next_times) if next_times else None,
            "oldest_due_at": min(due_times) if due_times else None,
            "due_sources": len(due_times),
            "overdue_sources": sum(1 for value in due_times if value <= overdue_cutoff),
            "blocked_sources": sum(
                1
                for source in eligible_sources
                if _source_blocked(source)
                or (
                    user_id is not None
                    and not bindings_by_source[source.id].auth_healthy
                )
            ),
        }
        items.append({
            "subscription_id": subscription.id,
            "latest_state": latest_state,
            "active_count": len({task.id for task, _ in active}),
            "attention_count": len({task.id for task, _ in attention}),
            "source_count": len(sources),
            "enabled_source_count": len(enabled_sources),
            "schedule": schedule,
        })

    return {"items": items, "updated_at": now}
