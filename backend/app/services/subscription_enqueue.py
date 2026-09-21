import logging
from datetime import datetime, timezone, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_job import DownloadJob
from app.models.remote_discovery import RemoteAccount, UserSubscription, UserSubscriptionSource
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.providers import registry
from app.services.job_manifest import append_manifest_event, update_manifest
from app.models.task_state import DOWNLOAD_RUNNING_STATUSES
from app.services.job_progress import apply_download_progress
from app.services.locks import redis_lock
from app.services.redis_client import get_redis
from app.services.settings import get_download_defaults, get_scheduler_config
from app.services.search_projection_outbox import request_search_projection
from app.services.auth_health import classify_source_health
from app.services.subscription_membership import (
    recompute_subscription_membership_cache,
    select_eligible_membership_source,
)

logger = logging.getLogger(__name__)

RQ_JOB_TIMEOUT = 7200
RUNNING_STATUSES = DOWNLOAD_RUNNING_STATUSES


def skip_result(source_id: UUID | str, code: str, message: str | None = None, **details) -> dict:
    reason = {
        "code": code,
        "message": message or code.replace("_", " "),
        "retryable": code in {
            "lock_busy",
            "membership_lock_busy",
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
        },
        "details": details or {},
    }
    result = {
        "status": "skipped",
        "source_id": str(source_id),
        "skip_reason": code,
        "reason": reason,
    }
    result.update(details)
    return result


async def refresh_subscription_last_synced_at(db: AsyncSession, subscription_id: UUID) -> datetime | None:
    result = await db.execute(
        select(func.max(SubscriptionSource.last_synced_at)).where(
            SubscriptionSource.subscription_id == subscription_id
        )
    )
    latest = result.scalar_one_or_none()
    sub = await db.get(Subscription, subscription_id)
    if sub:
        sub.last_synced_at = latest
        await db.flush()
    return latest


async def mark_source_sync_success(
    db: AsyncSession,
    subscription_source_id: UUID,
    when: datetime | None = None,
    *,
    triggering_user_subscription_id: UUID | None = None,
    triggering_remote_account_id: UUID | None = None,
    triggering_credential_generation: int | None = None,
) -> None:
    """Fan out a shared receipt without reviving stale private provenance.

    Every path that can mutate personal credential health uses the same row
    lock order: ``RemoteAccount`` (when present), then
    ``UserSubscriptionSource`` ordered by id, then the canonical aggregate.
    Account lifecycle operations use the same order.  Keeping the trigger
    account lock first prevents delete/revalidation from deadlocking with a
    fast download outcome.
    """

    when = when or datetime.now(timezone.utc)
    ss = await db.get(SubscriptionSource, subscription_source_id)
    if not ss:
        return
    trigger_account = None
    if triggering_remote_account_id is not None:
        with db.no_autoflush:
            trigger_account = (
                await db.execute(
                    select(RemoteAccount)
                    .where(RemoteAccount.id == triggering_remote_account_id)
                    .with_for_update(of=RemoteAccount)
                )
            ).scalar_one_or_none()
    sub = await db.get(Subscription, ss.subscription_id)
    config = await get_scheduler_config(db)
    from app.services.subscription_replan import next_user_subscription_check_at

    rows = (
        await db.execute(
            select(UserSubscriptionSource, UserSubscription)
            .join(
                UserSubscription,
                UserSubscription.id == UserSubscriptionSource.user_subscription_id,
            )
            .where(
                UserSubscriptionSource.subscription_source_id == ss.id,
                UserSubscription.is_active.is_(True),
            )
            .order_by(UserSubscriptionSource.id)
            .with_for_update(of=UserSubscriptionSource)
        )
    ).all()
    ss.last_synced_at = when
    ss.last_successful_auth = when
    await refresh_subscription_last_synced_at(db, ss.subscription_id)
    # A success changes the interval base.  NULL invalidates the old due time;
    # the next fair coverage pass recomputes interval/fixed/manual semantics in
    # its short claim transaction.
    ss.next_sync_at = None
    for binding, membership in rows:
        # Hard-deleted bindings intentionally retain a NULL account plus a
        # deleted health marker so they cannot masquerade as migrated legacy
        # global-auth bindings. Tombstones retain the FK with the same marker.
        if binding.auth_status == "deleted":
            continue
        binding.last_synced_at = when
        binding.last_attempted_at = when
        binding.next_sync_at = (
            next_user_subscription_check_at(membership, config, when, when, when)
            if membership.sync_enabled and binding.is_enabled
            else None
        )
        provenance_matches = (
            membership.id == triggering_user_subscription_id
            and binding.remote_account_id == triggering_remote_account_id
        )
        if provenance_matches and _binding_provenance_is_current(
            trigger_account,
            binding,
            triggering_credential_generation=triggering_credential_generation,
        ):
            binding.last_successful_auth = when
            binding.auth_healthy = True
            binding.auth_status = "healthy"
            binding.auth_error_reason = None
            binding.last_auth_checked_at = when
            if trigger_account is not None:
                if trigger_account.source == "x" and trigger_account.auth_method == "oauth2":
                    if trigger_account.download_auth_status == "personal":
                        trigger_account.download_auth_error_reason = None
                        trigger_account.last_download_auth_checked_at = when
                else:
                    trigger_account.auth_status = "healthy"
                    trigger_account.auth_error_reason = None
                    trigger_account.last_authenticated_at = when
    await recompute_subscription_membership_cache(db, ss.subscription_id)
    await request_search_projection(
        db,
        creator_ids=[sub.creator_id] if sub else (),
        repository_ids=[ss.id],
        subscription_ids=[ss.subscription_id],
    )


async def mark_source_auth_failure(
    db: AsyncSession,
    job: DownloadJob,
    reason: str,
    *,
    when: datetime | None = None,
) -> None:
    """Damage only the selected private credential and recompute shared cache."""

    when = when or datetime.now(timezone.utc)
    source_id = getattr(job, "subscription_source_id", None)
    if source_id is None:
        return
    source = await db.get(SubscriptionSource, source_id)
    if source is None:
        return
    membership_id = getattr(job, "triggering_user_subscription_id", None)
    account_id = getattr(job, "triggering_remote_account_id", None)
    account = None
    if account_id is not None:
        with db.no_autoflush:
            account = (
                await db.execute(
                    select(RemoteAccount)
                    .where(RemoteAccount.id == account_id)
                    .with_for_update(of=RemoteAccount)
                )
            ).scalar_one_or_none()
        # A private job whose account was deleted, tombstoned, disabled, or
        # replaced has stale provenance. It must not fall through to legacy
        # global auth or damage a newly reconnected credential generation.
        if not _private_account_can_accept_outcome(
            account,
            triggering_credential_generation=getattr(
                job,
                "triggering_credential_generation",
                None,
            ),
        ):
            return
    binding = None
    if membership_id is not None:
        conditions = [
            UserSubscriptionSource.subscription_source_id == source.id,
            UserSubscriptionSource.user_subscription_id == membership_id,
        ]
        if account_id is not None:
            conditions.append(UserSubscriptionSource.remote_account_id == account_id)
        binding = (
            await db.execute(
                select(UserSubscriptionSource)
                .where(*conditions)
                .with_for_update(of=UserSubscriptionSource)
            )
        ).scalar_one_or_none()
    reason_text = str(reason).casefold()
    if "401" in reason_text:
        safe_reason = "HTTP 401 Unauthorized"
    elif "403" in reason_text:
        safe_reason = "HTTP 403 Forbidden"
    elif "cookie" in reason_text:
        safe_reason = "Cookie expired or missing"
    elif "token" in reason_text:
        safe_reason = "Token expired or invalid"
    elif "login" in reason_text:
        safe_reason = "Login required"
    else:
        safe_reason = "Authentication failed"
    if binding is not None:
        if _binding_provenance_is_current(
            account,
            binding,
            triggering_credential_generation=getattr(
                job,
                "triggering_credential_generation",
                None,
            ),
        ):
            binding.auth_healthy = False
            binding.auth_status = "unhealthy"
            binding.auth_error_reason = safe_reason
            binding.last_auth_checked_at = when
            if account is not None:
                if account.source == "x" and account.auth_method == "oauth2":
                    if account.download_auth_status == "anonymous_only":
                        binding.auth_error_reason = "download_cookie_required"
                    else:
                        account.download_auth_status = "unhealthy"
                        account.download_auth_error_reason = safe_reason
                        account.last_download_auth_checked_at = when
                        from app.services.remote_accounts import RemoteAccountService

                        await RemoteAccountService(
                            db,
                            account.user_id,
                        )._pause_protected_x_bindings(account)
                else:
                    account.auth_status = "unhealthy"
                    account.auth_error_reason = safe_reason
    if binding is None and membership_id is None and account_id is None:
        # Compatibility for a truly legacy job without member provenance.
        source.auth_healthy = False
        source.auth_status = "unhealthy"
        source.auth_error_reason = safe_reason
        source.last_auth_checked_at = when
    elif binding is not None:
        await recompute_subscription_membership_cache(db, source.subscription_id)


def _private_account_can_accept_outcome(
    account: RemoteAccount | None,
    *,
    triggering_credential_generation: int | None,
) -> bool:
    """Check private credential generation while its account row is locked."""

    if account is None:
        return False
    return bool(
        account.is_enabled
        and account.auth_status != "deleted"
        and bool(account.credential_ciphertext)
        and triggering_credential_generation is not None
        and account.credential_generation == triggering_credential_generation
    )


def _binding_provenance_is_current(
    account: RemoteAccount | None,
    binding: UserSubscriptionSource,
    *,
    triggering_credential_generation: int | None,
) -> bool:
    if account is None:
        # ON DELETE SET NULL preserves the private job's generation.  Only a
        # job that was legacy from inception (and therefore has no generation)
        # may act on a legacy binding after the account row disappears.
        return bool(
            triggering_credential_generation is None
            and binding.remote_account_id is None
            and binding.auth_status != "deleted"
        )
    return bool(
        binding.auth_status != "deleted"
        and binding.remote_account_id == account.id
        and binding.user_id == account.user_id
        and _private_account_can_accept_outcome(
            account,
            triggering_credential_generation=triggering_credential_generation,
        )
    )


async def _latest_job_for_source(db: AsyncSession, source_id: UUID) -> DownloadJob | None:
    result = await db.execute(
        select(DownloadJob)
        .where(DownloadJob.subscription_source_id == source_id)
        .order_by(DownloadJob.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _restore_failed_publication_demand(
    db: AsyncSession,
    *,
    source_id: UUID,
    binding_id: UUID,
    membership_id: UUID,
    remote_account_id: UUID | None,
    credential_generation: int | None,
    claimed_at: datetime,
    claimed_next_sync_at: datetime | None,
    claimed_binding_updated_at: datetime,
    claimed_binding_is_enabled: bool,
    claimed_binding_auth_healthy: bool,
    claimed_binding_auth_status: str | None,
    claimed_account_auth_status: str | None,
    previous_source_attempted_at: datetime | None,
    previous_binding_attempted_at: datetime | None,
    previous_binding_next_sync_at: datetime | None,
) -> None:
    """CAS-restore one claim using the lifecycle/outcome lock order."""

    await db.rollback()
    account = None
    if remote_account_id is not None:
        account = (
            await db.execute(
                select(RemoteAccount)
                .where(RemoteAccount.id == remote_account_id)
                .with_for_update(of=RemoteAccount)
            )
        ).scalar_one_or_none()
        if not _private_account_can_accept_outcome(
            account,
            triggering_credential_generation=credential_generation,
        ) or account.auth_status != claimed_account_auth_status:
            await db.rollback()
            return
    binding = (
        await db.execute(
            select(UserSubscriptionSource)
            .where(UserSubscriptionSource.id == binding_id)
            .with_for_update(of=UserSubscriptionSource)
        )
    ).scalar_one_or_none()
    if (
        binding is None
        or binding.subscription_source_id != source_id
        or binding.user_subscription_id != membership_id
        or binding.remote_account_id != remote_account_id
        or _as_utc(binding.updated_at) != _as_utc(claimed_binding_updated_at)
        or binding.is_enabled != claimed_binding_is_enabled
        or binding.auth_healthy != claimed_binding_auth_healthy
        or binding.auth_status != claimed_binding_auth_status
        or (remote_account_id is None and binding.auth_status == "deleted")
        or (remote_account_id is None and credential_generation is not None)
    ):
        await db.rollback()
        return
    # Canonical rows are always acquired after the member binding, with the
    # Subscription row before its Source to match the sole aggregate helper.
    await db.execute(
        select(Subscription)
        .where(Subscription.id == binding.subscription_id)
        .with_for_update(of=Subscription)
    )
    source = (
        await db.execute(
            select(SubscriptionSource)
            .where(SubscriptionSource.id == source_id)
            .with_for_update(of=SubscriptionSource)
        )
    ).scalar_one_or_none()
    if source is None:
        await db.rollback()
        return
    if (
        _as_utc(binding.last_attempted_at) == _as_utc(claimed_at)
        and _as_utc(binding.next_sync_at) == _as_utc(claimed_next_sync_at)
    ):
        binding.last_attempted_at = previous_binding_attempted_at
        binding.next_sync_at = previous_binding_next_sync_at
        if _as_utc(source.last_attempted_at) == _as_utc(claimed_at):
            source.last_attempted_at = previous_source_attempted_at
        await recompute_subscription_membership_cache(db, source.subscription_id)
        await db.commit()
        return
    # A worker or another valid transition already advanced this binding.
    await db.rollback()


async def enqueue_subscription_source_sync(
    db: AsyncSession,
    subscription_source_id: UUID,
    trigger: str = "manual",
    force: bool = False,
    parent_task_id: UUID | None = None,
    force_reason: str | None = None,
    scheduler_config: dict | None = None,
    triggering_user_subscription_id: UUID | None = None,
    triggering_remote_account_id: UUID | None = None,
    batch_item_id: UUID | None = None,
    batch_mode: str | None = None,
    repeat_intent: dict | None = None,
) -> dict:
    if batch_mode is not None and (batch_item_id is None or parent_task_id is None):
        raise ValueError("Batch mode requires a durable item and parent")
    batch_manual = batch_item_id is not None and batch_mode == "manual_all_enabled"
    now = datetime.now(timezone.utc)
    explicit_private_manual = (
        trigger != "scheduler" and triggering_user_subscription_id is not None
    )
    ss = await db.get(SubscriptionSource, subscription_source_id)
    if not ss:
        return skip_result(subscription_source_id, "source_not_found")

    sub = await db.get(Subscription, ss.subscription_id)
    if not sub:
        return skip_result(ss.id, "subscription_not_found")
    if not sub.is_active:
        return skip_result(ss.id, "subscription_inactive")
    if not force and not ss.is_enabled and not explicit_private_manual and not batch_manual:
        return skip_result(ss.id, "source_disabled")
    if (
        not force
        and classify_source_health(ss, sub).actionable
        and not explicit_private_manual
        and not batch_manual
    ):
        return skip_result(ss.id, "auth_unhealthy", auth_status=ss.auth_status, auth_error_reason=ss.auth_error_reason)
    if not force:
        try:
            r_ph = get_redis()
            ph = r_ph.hgetall(f"proxy:health:{ss.source}")
            if ph and ph.get(b"status", b"").decode() == "degraded":
                return skip_result(ss.id, "proxy_degraded", warnings=ph.get(b"warnings", b"").decode())
        except Exception:
            pass
    if not ss.source_url:
        return skip_result(ss.id, "source_url_empty")

    try:
        provider = registry.get(ss.source)
    except KeyError:
        return skip_result(ss.id, "unknown_provider", source=ss.source)
    if not provider.capabilities.can_download:
        return skip_result(ss.id, "provider_not_downloadable", source=ss.source)

    # ``force`` may bypass schedule-frequency checks, never hard capacity.
    # Automatic scans stop producing under host pressure; manual requests may
    # remain in the bounded queue and will wait at the worker heavy-I/O gate.
    try:
        from app.services.backpressure import download_backpressure_reason

        pressure = await download_backpressure_reason(
            db,
            automatic=trigger == "scheduler",
            include_queue=True,
        )
        if pressure:
            code = str(pressure.get("code") or "download_unavailable")
            details = {key: value for key, value in pressure.items() if key != "code"}
            return skip_result(ss.id, code, **details)
    except Exception as exc:
        logger.warning("Unable to evaluate download admission", exc_info=True)
        return skip_result(
            ss.id,
            "admission_check_failed",
            message="Download admission checks are unavailable",
            error_type=type(exc).__name__,
        )

    normalized_url = provider.normalize_url(ss.source_url) or ss.source_url
    if not provider.validate_url(normalized_url):
        return skip_result(ss.id, "url_invalid", source_url=ss.source_url)
    if normalized_url != ss.source_url:
        ss.source_url = normalized_url

    lock_key = f"lock:subscription-source-sync:{ss.id}"
    async with redis_lock(lock_key, ttl_seconds=120) as acquired:
        if not acquired:
            return skip_result(ss.id, "lock_busy")

        ss = (
            await db.execute(
                select(SubscriptionSource)
                .where(SubscriptionSource.id == subscription_source_id)
                .with_for_update(of=SubscriptionSource)
            )
        ).scalar_one_or_none()
        if ss is None:
            return skip_result(subscription_source_id, "source_not_found")

        if batch_item_id is not None:
            from app.models.scheduler_batch import SchedulerBatch, SchedulerBatchItem
            from app.models.task_run import TaskRun
            from app.services.scheduler_batches import subsequent_sync_identity

            item = await db.get(SchedulerBatchItem, batch_item_id)
            batch = await db.get(SchedulerBatch, item.batch_id) if item else None
            if (item is None or batch is None or batch.task_id != parent_task_id
                    or item.source_id != subscription_source_id or item.download_job_id is not None):
                raise ValueError("Invalid or already-bound scheduler batch item")
            if batch.legacy_task_id:
                parent = await db.get(TaskRun, batch.task_id)
                cutoff = (parent.meta or {}).get("legacy_reconciliation_since")
                if cutoff is None:
                    raise ValueError("Legacy reconciliation cutoff is missing")
                # The same source row/Redis claim used by ordinary admission is
                # held through evidence lookup and creation/binding commit.
                linked = await subsequent_sync_identity(db, ss.id, datetime.fromisoformat(cutoff))
                if linked is not None:
                    item.download_job_id = linked
                    item.owns_download = False
                    return {"status": "linked", "job_id": str(linked)}
            # Mutable eligibility is checked again under the source claim lock.
            sub = await db.get(Subscription, ss.subscription_id, populate_existing=True)
            if sub is None or not sub.is_active:
                return skip_result(ss.id, "subscription_inactive")
            if not ss.is_enabled and not batch_manual:
                return skip_result(ss.id, "source_disabled")
            if classify_source_health(ss, sub).actionable and not batch_manual:
                return skip_result(ss.id, "auth_unhealthy")
            if batch_mode != "manual_all_enabled" and not sub.sync_enabled:
                return skip_result(ss.id, "subscription_sync_disabled")

        if scheduler_config is None:
            scheduler_config = await get_scheduler_config(db)
        if batch_mode == "due_scan" and not scheduler_config.get("scheduler_enabled", True):
            return skip_result(ss.id, "scheduler_disabled")

        selection_options = dict(
            now=now,
            preferred_membership_id=triggering_user_subscription_id,
            preferred_account_id=triggering_remote_account_id,
            require_due=trigger == "scheduler" and not force,
            system_schedule_mode=(
                scheduler_config.get("schedule_mode", "interval")
                if trigger == "scheduler" and not force
                else None
            ),
            require_sync_enabled=not explicit_private_manual and batch_mode != "manual_all_enabled",
            require_preferred_account_match=explicit_private_manual,
        )
        selection = await select_eligible_membership_source(db, ss, **selection_options)
        if selection is None:
            # A committed eligible binding hidden by SKIP LOCKED is pressure,
            # not a permanent eligibility decision. This read takes no row lock.
            if batch_item_id is not None and await select_eligible_membership_source(
                db, ss, **selection_options, acquire_lock=False
            ) is not None:
                return skip_result(ss.id, "membership_lock_busy")
            return skip_result(ss.id, "no_eligible_member_source")
        triggering_user_subscription_id = selection.membership.id
        triggering_remote_account_id = (
            selection.account.id if selection.account is not None else None
        )
        if (
            selection.account is not None
            and selection.account.user_id != selection.membership.user_id
        ):
            raise ValueError("download provenance resolves to mixed owners")
        triggering_credential_generation = (
            selection.account.credential_generation
            if selection.account is not None
            else None
        )

        running = await db.execute(
            select(DownloadJob)
            .where(
                DownloadJob.subscription_source_id == ss.id,
                DownloadJob.status.in_(RUNNING_STATUSES),
            )
            .order_by(DownloadJob.created_at.desc())
            .limit(1)
        )
        running_job = running.scalar_one_or_none()
        if running_job:
            return skip_result(ss.id, "already_running", job_id=str(running_job.id))

        if trigger == "scheduler" and not force:
            latest = await _latest_job_for_source(db, ss.id)
            latest_created_at = _as_utc(latest.created_at) if latest else None
            if latest and latest_created_at and latest.status in ("failed", "stale"):
                defaults = await get_download_defaults(db)
                backoff = int(defaults.get("retry_backoff_base_seconds", 60))
                max_retries = int(defaults.get("max_retries", 3))
                backoff_until = latest_created_at + timedelta(seconds=max(backoff, 1) * max(max_retries, 1))
                if now < backoff_until:
                    return skip_result(ss.id, "recent_failure_backoff", latest_job_id=str(latest.id), backoff_until=backoff_until.isoformat())

        job = DownloadJob(
            subscription_id=sub.id,
            subscription_source_id=ss.id,
            source=ss.source,
            source_url=normalized_url,
            status="enqueued",
            triggering_user_subscription_id=triggering_user_subscription_id,
            triggering_remote_account_id=triggering_remote_account_id,
            triggering_credential_generation=triggering_credential_generation,
            owner_user_id=selection.membership.user_id,
        )
        apply_download_progress(
            job,
            "enqueued",
            "Queued; waiting for download worker",
            publish=False,
        )
        update_manifest(job,
            trigger=trigger,
            force_reason=force_reason,
            had_sync_baseline=bool(ss.last_synced_at),
            subscription_source_id=str(ss.id),
            subscription_id=str(sub.id),
            source=ss.source,
            source_url=normalized_url,
        )
        append_manifest_event(job, "created", trigger=trigger)
        from app.services.subscription_replan import next_user_subscription_check_at

        next_sync_at = next_user_subscription_check_at(
            selection.membership,
            scheduler_config,
            selection.binding.last_synced_at,
            now,
            now,
        )
        # Flush caller-owned state before opening the SAVEPOINT. SQLAlchemy
        # flushes pending state when begin_nested() starts; doing it explicitly
        # keeps unrelated outer-transaction errors out of the candidate-job
        # IntegrityError handler below.
        await db.flush()
        try:
            # A source-level unique/running race must not roll back the caller's
            # parent batch TaskRun or progress updates.  Add/flush inside a
            # SAVEPOINT so only this candidate DownloadJob is discarded.
            async with db.begin_nested():
                db.add(job)
                await db.flush()
        except IntegrityError:
            running = await db.execute(
                select(DownloadJob).where(
                    DownloadJob.subscription_source_id == ss.id,
                    DownloadJob.status.in_(RUNNING_STATUSES),
                ).order_by(DownloadJob.created_at.desc()).limit(1)
            )
            running_job = running.scalar_one_or_none()
            if running_job:
                return skip_result(ss.id, "already_running", job_id=str(running_job.id))
            raise

        await request_search_projection(
            db,
            subscription_ids=[sub.id],
        )

        from app.services.download_dispatch import prepare_download_dispatch, publish_prepared_download

        queue_name = f"downloads:{job.source}"
        prepared = await prepare_download_dispatch(
            db,
            job,
            queue_name=queue_name,
            parent_task_id=parent_task_id,
            job_timeout=RQ_JOB_TIMEOUT,
            action=trigger,
        )
        previous_source_attempted_at = ss.last_attempted_at
        previous_binding_attempted_at = selection.binding.last_attempted_at
        previous_binding_next_sync_at = selection.binding.next_sync_at
        ss.last_attempted_at = now
        selection.binding.last_attempted_at = now
        selection.binding.next_sync_at = next_sync_at
        await recompute_subscription_membership_cache(db, sub.id)
        # ``updated_at`` is a server-side onupdate value and SQLAlchemy expires
        # it after the claim flush. Load it explicitly inside the async path so
        # the CAS snapshot never triggers implicit synchronous IO.
        await db.refresh(selection.binding, attribute_names=["updated_at"])
        claimed_binding_updated_at = selection.binding.updated_at
        claimed_binding_is_enabled = selection.binding.is_enabled
        claimed_binding_auth_healthy = selection.binding.auth_healthy
        claimed_binding_auth_status = selection.binding.auth_status
        claimed_account_auth_status = (
            selection.account.auth_status if selection.account is not None else None
        )
        if repeat_intent is not None:
            from app.models.download_repeat import DownloadRepeatIntent
            if not explicit_private_manual or selection.membership.user_id != repeat_intent["actor_user_id"]:
                raise ValueError("Repeat intent requires its acting membership")
            db.add(DownloadRepeatIntent(**repeat_intent, download_job_id=job.id, task_id=prepared.task.id))
            # New identity, actor/request receipt and ordinary dispatch outbox
            # commit together, before any Redis visibility or worker callback.
            await db.commit()
            return {"status": "enqueued", "job_id": str(job.id), "task_id": str(prepared.task.id)}
        if batch_item_id is not None:
            from app.models.scheduler_batch import SchedulerBatch, SchedulerBatchItem
            from app.services.operations import fence_current_admin_operation_transaction
            item = await db.get(SchedulerBatchItem, batch_item_id)
            batch = await db.get(SchedulerBatch, item.batch_id) if item else None
            if (item is None or batch is None or batch.task_id != parent_task_id
                    or item.source_id != subscription_source_id or item.download_job_id is not None):
                raise ValueError("Invalid or already-bound scheduler batch item")
            item.download_job_id = job.id
            item.child_task_id = prepared.task.id
            prepared.task.meta = {**(prepared.task.meta or {}), "scheduler_batch_task_id": str(parent_task_id)}
            item.owns_download = True
            item.status = "queued"
            await fence_current_admin_operation_transaction(db, task_id=parent_task_id)
            # Batch identity and deterministic child outbox become durable
            # together. The caller publishes through the cancellable outbox
            # recovery path immediately after this commit.
            await db.commit()
            return {"status": "enqueued", "source_id": str(subscription_source_id),
                    "job_id": str(job.id), "task_id": str(prepared.task.id)}

        try:
            await publish_prepared_download(
                db,
                job,
                prepared,
                job_timeout=RQ_JOB_TIMEOUT,
                action=trigger,
            )
        except Exception as exc:
            from app.services.backpressure import DownloadAdmissionError

            logger.error("Failed to enqueue download job %s: %s", job.id, exc)
            await _restore_failed_publication_demand(
                db,
                source_id=ss.id,
                binding_id=selection.binding.id,
                membership_id=selection.membership.id,
                remote_account_id=triggering_remote_account_id,
                credential_generation=triggering_credential_generation,
                claimed_at=now,
                claimed_next_sync_at=next_sync_at,
                claimed_binding_updated_at=claimed_binding_updated_at,
                claimed_binding_is_enabled=claimed_binding_is_enabled,
                claimed_binding_auth_healthy=claimed_binding_auth_healthy,
                claimed_binding_auth_status=claimed_binding_auth_status,
                claimed_account_auth_status=claimed_account_auth_status,
                previous_source_attempted_at=previous_source_attempted_at,
                previous_binding_attempted_at=previous_binding_attempted_at,
                previous_binding_next_sync_at=previous_binding_next_sync_at,
            )
            code = exc.code if isinstance(exc, DownloadAdmissionError) else "enqueue_failed"
            details = dict(exc.details) if isinstance(exc, DownloadAdmissionError) else {}
            return {
                "status": "error",
                "source_id": str(ss.id),
                "job_id": str(job.id),
                "error": str(exc),
                "skip_reason": code,
                "reason": {"code": code, "message": str(exc), "retryable": True, "details": details},
            }

        # The claim was part of the durable outbox transaction committed by
        # the publisher.  Do not write stale ORM snapshots after Redis makes a
        # fast worker visible; its success fan-out must win.
        await db.commit()

        try:
            from app.services.job_progress import publish_progress

            publish_progress(str(job.id), "download", job.progress_data)
        except Exception:
            logger.warning("Failed to publish queued progress for %s", job.id, exc_info=True)
        return {
            "status": "enqueued",
            "source_id": str(ss.id),
            "job_id": str(job.id),
            "task_id": str(prepared.task.id),
            "source_url": normalized_url,
        }
