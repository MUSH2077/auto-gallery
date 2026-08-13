import logging
import asyncio
from datetime import datetime, timezone, timedelta

from sqlalchemy import or_, select

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python < 3.9
from app.database import async_session
from app.config import settings
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.services.locks import redis_lock
from app.services.redis_client import get_redis
from app.services.queue_admission import (
    QueueAdmissionError,
    ensure_redis_enqueue_capacity,
)
from app.services.scheduler_loop import (
    ensure_next_subscription_scan,
    mark_scheduler_scan_error,
    mark_scheduler_scan_finished,
    mark_scheduler_scan_started,
)
from app.services.settings import get_scheduler_config
from app.services.subscription_enqueue import enqueue_subscription_source_sync

logger = logging.getLogger(__name__)

FALLBACK_INTERVAL_HOURS = 6
FALLBACK_SCAN_MINUTES = 60
SCHEDULER_COVERAGE_BATCH_SIZE = 100
SCHEDULER_RECONCILE_KEY = "scheduler:subscription-source:reconcile"


def _automatic_scan_batch_limit(
    configured_limit: int,
    remaining_slots: int,
    throughput_scale: float,
) -> int:
    """Apply the soft AIMD rate while preserving constrained forward progress."""

    if int(remaining_slots) <= 0:
        return 0
    base_limit = min(max(1, int(configured_limit)), int(remaining_slots))
    try:
        scale = max(0.0, min(1.0, float(throughput_scale)))
    except (TypeError, ValueError):
        scale = 1.0
    # A hard/critical controller never reaches this function because admission
    # already carries a reason.  Constrained mode is allowed at least one fair
    # keyset item so the queue cannot starve indefinitely under mild pressure.
    return max(1, int(base_limit * scale))


def _automatic_mode_predicate(system_config: dict):
    """Exclude effective manual schedules without losing inherited modes."""

    inherited_mode = system_config.get("schedule_mode", "interval") or "interval"
    if inherited_mode == "manual":
        return (
            Subscription.schedule_mode.is_not(None)
            & (Subscription.schedule_mode != "manual")
        )
    return or_(
        Subscription.schedule_mode.is_(None),
        Subscription.schedule_mode != "manual",
    )


async def _load_subscription_source_batch(
    db,
    *,
    now: datetime,
    system_config: dict,
    limit: int = SCHEDULER_COVERAGE_BATCH_SIZE,
):
    """Claim one durable, fair due page and eagerly join its subscription.

    NULL rows are invalidated/unseen schedule metadata.  Once evaluated, a due
    row receives the claim timestamp and remains due.  It therefore moves
    behind still-NULL rows and lets 194 fresh sources receive coverage in two
    100-row passes even if the AIMD enqueue budget is only one.
    """

    statement = (
        select(SubscriptionSource, Subscription)
        .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
        .where(
            Subscription.is_active.is_(True),
            Subscription.sync_enabled.is_(True),
            SubscriptionSource.is_enabled.is_(True),
            or_(
                SubscriptionSource.next_sync_at.is_(None),
                SubscriptionSource.next_sync_at <= now,
            ),
            _automatic_mode_predicate(system_config),
        )
        .order_by(
            SubscriptionSource.next_sync_at.asc().nullsfirst(),
            SubscriptionSource.id.asc(),
        )
        .limit(min(max(1, int(limit)), SCHEDULER_COVERAGE_BATCH_SIZE))
        .with_for_update(skip_locked=True, of=SubscriptionSource)
    )
    result = await db.execute(statement)
    return [(row[0], row[1]) for row in result.all()]


def _as_tz(value: datetime | None, tz) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def _parse_scheduled_times(times_str: str | None) -> list[tuple[int, int, int]]:
    scheduled: list[tuple[int, int, int]] = []
    for raw in (times_str or "").split(","):
        t = raw.strip()
        if not t:
            continue
        try:
            parts = t.split(":")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            sec = int(parts[2]) if len(parts) > 2 else 0
            if 0 <= h <= 23 and 0 <= m <= 59 and 0 <= sec <= 59:
                scheduled.append((h, m, sec))
        except (ValueError, IndexError):
            continue
    return sorted(set(scheduled))


def _schedule_decision(
    sub,
    system_config: dict,
    last_synced_at,
    last_attempted_at,
    now: datetime,
    tz,
) -> dict:
    """Return a scheduler decision with a reason and optional fixed-time window."""
    mode = sub.schedule_mode or system_config.get("schedule_mode", "interval")

    if mode == "manual":
        return {"due": False, "mode": mode, "reason": "manual_mode"}

    if mode == "interval":
        if not last_synced_at:
            return {"due": True, "mode": mode, "reason": "never_synced_interval"}
        interval_hours = sub.sync_interval_hours or int(
            system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
        cutoff = now - timedelta(hours=interval_hours)
        last_synced = _as_tz(last_synced_at, tz)
        return {
            "due": bool(last_synced and last_synced < cutoff),
            "mode": mode,
            "reason": "interval_due" if last_synced and last_synced < cutoff else "interval_not_due",
        }

    if mode == "fixed_time":
        times_str = sub.scheduled_times or system_config.get("scheduled_times", "")
        scheduled = _parse_scheduled_times(times_str)
        if not scheduled:
            if not last_synced_at:
                return {"due": True, "mode": mode, "reason": "never_synced_interval_fallback"}
            interval_hours = sub.sync_interval_hours or int(
                system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
            cutoff = now - timedelta(hours=interval_hours)
            last_synced = _as_tz(last_synced_at, tz)
            return {
                "due": bool(last_synced and last_synced < cutoff),
                "mode": mode,
                "reason": "interval_fallback_due" if last_synced and last_synced < cutoff else "interval_fallback_not_due",
            }

        scan_minutes = int(system_config.get("scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES))
        window = timedelta(minutes=max(scan_minutes, 5) + 5)
        last_synced = _as_tz(last_synced_at, tz)
        last_attempted = _as_tz(last_attempted_at, tz)

        for day_offset in (0, -1):
            day = now.date() + timedelta(days=day_offset)
            for h, m, s_val in scheduled:
                st = datetime(day.year, day.month, day.day, h, m, s_val, tzinfo=tz)
                window_end = st + window
                if not (st <= now <= window_end):
                    continue
                payload = {
                    "mode": mode,
                    "window_start": st.isoformat(),
                    "window_end": window_end.isoformat(),
                    "scheduled_time": st.time().isoformat(),
                }
                if last_synced and last_synced >= st:
                    return {**payload, "due": False, "reason": "already_synced_in_window"}
                if last_attempted and last_attempted >= st:
                    return {**payload, "due": False, "reason": "already_attempted_in_window"}
                return {**payload, "due": True, "reason": "fixed_time_window_due"}

        return {"due": False, "mode": mode, "reason": "outside_fixed_time_window"}

    if not last_synced_at:
        return {"due": True, "mode": mode, "reason": "never_synced_interval_fallback"}
    interval_hours = sub.sync_interval_hours or int(
        system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
    cutoff = now - timedelta(hours=interval_hours)
    last_synced = _as_tz(last_synced_at, tz)
    return {
        "due": bool(last_synced and last_synced < cutoff),
        "mode": mode,
        "reason": "unknown_mode_interval_due" if last_synced and last_synced < cutoff else "unknown_mode_interval_not_due",
    }


def _should_sync_now(sub, system_config: dict, last_synced_at, now: datetime, tz, last_attempted_at=None) -> bool:
    """Check whether a sync is due, respecting per-subscription schedule strategy.

    Priority: subscription value > system default > hardcoded fallback.
    sub.schedule_mode=None means inherit from system_config.

    Modes: 'manual'=never, 'interval'=every N hours, 'fixed_time'=at scheduled times.
    """
    return bool(_schedule_decision(sub, system_config, last_synced_at, last_attempted_at, now, tz)["due"])


def _next_fixed_time_window(
    times_str: str | None,
    now: datetime,
    tz,
    scan_minutes: int,
) -> dict:
    scheduled = _parse_scheduled_times(times_str)
    if not scheduled:
        return {"next_due_at": None, "window_start": None, "window_end": None}

    window = timedelta(minutes=max(scan_minutes, 5) + 5)
    candidates = []
    for day_offset in (0, 1):
        day = now.date() + timedelta(days=day_offset)
        for h, m, s_val in scheduled:
            start = datetime(day.year, day.month, day.day, h, m, s_val, tzinfo=tz)
            end = start + window
            if now <= end:
                candidates.append((start, end))
    if not candidates:
        return {"next_due_at": None, "window_start": None, "window_end": None}

    start, end = min(candidates, key=lambda item: item[0])
    return {
        "next_due_at": start.isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _next_interval_check_at(
    sub,
    system_config: dict,
    last_synced_at: datetime | None,
    last_attempted_at: datetime | None,
    now: datetime,
    tz,
) -> datetime:
    interval_hours = sub.sync_interval_hours or int(
        system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS)
    )
    interval = timedelta(hours=max(1, int(interval_hours)))
    scan_delay = timedelta(minutes=max(
        int(system_config.get("scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES)),
        5,
    ))
    synced = _as_tz(last_synced_at, tz)
    attempted = _as_tz(last_attempted_at, tz)
    due_at = synced + interval if synced else now

    # A just-published/running attempt must not be reconsidered on every scan.
    # Once the short retry horizon passes, a failed attempt is eligible again;
    # a successful attempt will have moved the base via last_synced_at.
    if due_at <= now and attempted and (synced is None or attempted > synced):
        retry_at = attempted + scan_delay
        if retry_at > now:
            return _utc(retry_at)
    return _utc(max(due_at, now))


def _next_fixed_check_at(
    sub,
    system_config: dict,
    last_synced_at: datetime | None,
    last_attempted_at: datetime | None,
    now: datetime,
    tz,
) -> datetime:
    scheduled = _parse_scheduled_times(
        sub.scheduled_times or system_config.get("scheduled_times", "")
    )
    if not scheduled:
        return _next_interval_check_at(
            sub,
            system_config,
            last_synced_at,
            last_attempted_at,
            now,
            tz,
        )

    window = timedelta(minutes=max(
        int(system_config.get("scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES)),
        5,
    ) + 5)
    synced = _as_tz(last_synced_at, tz)
    attempted = _as_tz(last_attempted_at, tz)
    candidates: list[tuple[datetime, datetime]] = []
    for day_offset in (-1, 0, 1, 2):
        day = now.date() + timedelta(days=day_offset)
        for hour, minute, second in scheduled:
            start = datetime(
                day.year,
                day.month,
                day.day,
                hour,
                minute,
                second,
                tzinfo=tz,
            )
            candidates.append((start, start + window))

    for start, end in sorted(candidates, key=lambda item: item[0]):
        if end < now:
            continue
        completed = bool(
            (synced and synced >= start)
            or (attempted and attempted >= start)
        )
        if start <= now <= end:
            if not completed:
                return _utc(now)
            continue
        if start > now:
            return _utc(start)

    # The two-day horizon above is deliberately ample for daily fixed times;
    # retain a safe retry if malformed timezone data ever defeats it.
    return _utc(now + timedelta(days=1))


def next_subscription_check_at(
    sub,
    system_config: dict,
    last_synced_at: datetime | None,
    last_attempted_at: datetime | None,
    now: datetime,
    tz=None,
) -> datetime | None:
    """Calculate the durable next scheduler check for one source.

    Manual schedules intentionally return NULL because the joined due query
    excludes their effective mode.  A settings change invalidates affected
    rows back to NULL so they are promptly re-evaluated under the new mode.
    """

    if tz is None:
        try:
            tz = ZoneInfo(system_config.get("timezone", "UTC"))
        except Exception:
            tz = timezone.utc
    local_now = _as_tz(now, tz) or datetime.now(tz)
    mode = sub.schedule_mode or system_config.get("schedule_mode", "interval")
    if mode == "manual":
        return None
    if mode == "fixed_time":
        return _next_fixed_check_at(
            sub,
            system_config,
            last_synced_at,
            last_attempted_at,
            local_now,
            tz,
        )
    return _next_interval_check_at(
        sub,
        system_config,
        last_synced_at,
        last_attempted_at,
        local_now,
        tz,
    )


def schedule_decision_snapshot(
    sub,
    system_config: dict,
    last_synced_at,
    last_attempted_at,
    now: datetime,
    tz,
) -> dict:
    """Read-only schedule explanation used by APIs and tests.

    This mirrors the enqueue scanner decision without mutating any source state.
    """
    decision = _schedule_decision(sub, system_config, last_synced_at, last_attempted_at, now, tz)
    mode = decision.get("mode") or sub.schedule_mode or system_config.get("schedule_mode", "interval")
    scan_minutes = int(system_config.get("scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES))

    next_due_at = None
    window_start = decision.get("window_start")
    window_end = decision.get("window_end")

    if mode == "interval":
        if last_synced_at:
            interval_hours = sub.sync_interval_hours or int(
                system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
            next_due = _as_tz(last_synced_at, tz) + timedelta(hours=interval_hours)
            next_due_at = next_due.isoformat()
        else:
            next_due_at = now.isoformat()
    elif mode == "fixed_time":
        fixed = _next_fixed_time_window(
            sub.scheduled_times or system_config.get("scheduled_times", ""),
            now,
            tz,
            scan_minutes,
        )
        next_due_at = fixed["next_due_at"]
        window_start = window_start or fixed["window_start"]
        window_end = window_end or fixed["window_end"]
    elif mode == "manual":
        next_due_at = None
    else:
        if last_synced_at:
            interval_hours = sub.sync_interval_hours or int(
                system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
            next_due_at = (_as_tz(last_synced_at, tz) + timedelta(hours=interval_hours)).isoformat()
        else:
            next_due_at = now.isoformat()

    return {
        **decision,
        "next_due_at": next_due_at,
        "window_start": window_start,
        "window_end": window_end,
    }


def sync_subscriptions():
    scan_interval_minutes = FALLBACK_SCAN_MINUTES
    current_job_id = None
    try:
        from rq import get_current_job

        current_job = get_current_job()
        current_job_id = current_job.id if current_job else None
        mark_scheduler_scan_started(scan_interval_minutes=scan_interval_minutes)
        # Do not run an automatic scan that cannot durably publish its work or
        # its successor.  The same RQ job is retained below until recovery.
        ensure_redis_enqueue_capacity(get_redis())
        result = asyncio.run(sync_subscriptions_async())
        scan_interval_minutes = int(
            result.pop("_scan_interval_minutes", scan_interval_minutes)
        )
        ensured = ensure_next_subscription_scan(
            scan_interval_minutes,
            exclude_job_id=current_job_id,
        )
        result["rescheduled_at"] = ensured.get("next_scan_at")
        mark_scheduler_scan_finished(
            scan_interval_minutes=scan_interval_minutes,
            next_scan_at=(
                datetime.fromisoformat(ensured["next_scan_at"])
                if ensured.get("next_scan_at")
                else None
            ),
        )
        return result
    except QueueAdmissionError as exc:
        # Preserve this same scheduler job until Redis falls below the hard
        # admission line.  Creating a new scan is exactly what the guard blocks;
        # RQ's interval Retry keeps the existing job recoverable instead.
        from rq import Retry

        logger.warning(
            "Subscription scan reschedule deferred by Redis admission code=%s",
            exc.code,
        )
        mark_scheduler_scan_error(exc, scan_interval_minutes=scan_interval_minutes)
        return Retry(max=1_000_000, interval=60)
    except Exception as exc:
        # A provider/DB/business failure must not sever the scheduling chain.
        # Best effort is sufficient here because the supervisor watchdog runs
        # independently every minute and repeats this idempotent ensure.
        try:
            ensure_next_subscription_scan(
                scan_interval_minutes,
                exclude_job_id=current_job_id,
            )
        except Exception:
            logger.exception("Failed to preserve subscription scan after error")
        mark_scheduler_scan_error(exc, scan_interval_minutes=scan_interval_minutes)
        raise


async def sync_subscriptions_async(parent_task_id=None):
    async with redis_lock("lock:subscription-sync-scan", ttl_seconds=300) as acquired:
        if not acquired:
            logger.info("Subscription auto-sync scan already running; skipping")
            return {"created": 0, "skipped": 1, "errors": 0, "rescheduled_at": None, "status": "skipped", "reason": "lock_busy"}
        return await _sync_subscriptions_locked(parent_task_id=parent_task_id)


async def _sync_subscriptions_locked(parent_task_id=None):
    logger.info("Starting subscription auto-sync scan")
    jobs_created = 0
    skipped_count = 0
    error_count = 0
    pressure_paused = False
    pressure_snapshot = None
    coverage_count = 0
    due_count = 0
    budget_deferred_count = 0

    async with async_session() as db:
        config = await get_scheduler_config(db)

        # Use configured timezone for schedule evaluation
        tz_name = config.get("timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        now = datetime.now(tz)

        default_interval = int(config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
        scan_minutes = int(config.get("scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES))
        scheduler_enabled = bool(config.get("scheduler_enabled", True))

        from app.services.backpressure import download_admission_batch

        # Disk, artifact backlog, pressure profile and Redis/queue capacity are
        # sampled once for the producer cycle.  Each actual publish retains the
        # serialized hard recount in enqueue_download_rq.
        async with download_admission_batch(db, automatic=True) as admission:
            if admission.reason:
                pressure_paused = admission.reason.get("code") == "resource_pressure"
                pressure_snapshot = admission.reason if pressure_paused else None
                logger.warning(
                    "Automatic subscription scan admission constrained code=%s details=%s",
                    admission.reason.get("code"),
                    admission.reason,
                )
            enqueue_budget = (
                0
                if admission.reason or not scheduler_enabled
                else _automatic_scan_batch_limit(
                    settings.scheduler_scan_batch_size,
                    admission.remaining_slots,
                    admission.throughput_scale,
                )
            )

            source_rows = []
            decisions: list[dict] = []
            if scheduler_enabled:
                source_rows = await _load_subscription_source_batch(
                    db,
                    now=_utc(now),
                    system_config=config,
                    limit=SCHEDULER_COVERAGE_BATCH_SIZE,
                )
                coverage_count = len(source_rows)

                # This short transaction is the only row-locking phase.  Due
                # rows retain an expired timestamp as a persistent backlog;
                # non-due rows move directly to their computed next check.
                # Commit before any provider/Redis enqueue I/O.
                for ss, sub in source_rows:
                    decision = _schedule_decision(
                        sub,
                        config,
                        ss.last_synced_at,
                        ss.last_attempted_at,
                        now,
                        tz,
                    )
                    decisions.append(decision)
                    if decision["due"]:
                        due_count += 1
                        ss.next_sync_at = _utc(now)
                    else:
                        ss.next_sync_at = next_subscription_check_at(
                            sub,
                            config,
                            ss.last_synced_at,
                            ss.last_attempted_at,
                            now,
                            tz,
                        )
                if source_rows:
                    await db.commit()

            retry_check_at = _utc(now) + timedelta(minutes=max(scan_minutes, 5))
            retry_later: list[SubscriptionSource] = []
            for (ss, sub), decision in zip(source_rows, decisions):
                log_context = {"source_id": str(ss.id), "source": ss.source, "timezone": tz_name}
                if not decision["due"]:
                    skipped_count += 1
                    logger.debug("Auto-sync skipped source: not due", extra={**log_context, **decision})
                    continue

                if ss.auth_healthy is False:
                    skipped_count += 1
                    retry_later.append(ss)
                    logger.debug("Auto-sync skipped source: auth unhealthy", extra={**log_context, "decision": "auth_unhealthy"})
                    continue

                if jobs_created >= enqueue_budget:
                    # Do not advance this timestamp into the future.  Its due
                    # intent stays durable, while the claim time places it
                    # behind unseen NULL rows and older due claims.
                    skipped_count += 1
                    budget_deferred_count += 1
                    logger.debug(
                        "Auto-sync retained due source for a later AIMD slot",
                        extra={**log_context, **decision, "enqueue_budget": enqueue_budget},
                    )
                    continue

                result = await enqueue_subscription_source_sync(
                    db,
                    ss.id,
                    trigger="scheduler",
                    parent_task_id=parent_task_id,
                    scheduler_config=config,
                )
                if result["status"] == "enqueued":
                    jobs_created += 1
                    logger.info("Auto-sync created download job",
                                extra={**log_context, **decision, "job_id": result["job_id"], "source_url": result.get("source_url")})
                else:
                    skipped_count += 1
                    retry_later.append(ss)
                    if result.get("status") == "error":
                        error_count += 1
                    logger.debug("Auto-sync skipped source after enqueue check",
                                 extra={**log_context, **decision, "skip_reason": result.get("skip_reason") or result})

            if retry_later:
                for source in retry_later:
                    source.next_sync_at = retry_check_at
                await db.commit()

    mode = config.get("schedule_mode", "interval")
    logger.info("Auto-sync scan complete: %d jobs created, %d skipped (enabled=%s, mode=%s, timezone=%s, scan_every=%dm, default_interval=%dh)",
                jobs_created, skipped_count, config.get("scheduler_enabled", True), mode, config.get("timezone", "UTC"), scan_minutes, default_interval)

    # Self-heal: link any orphaned source_creators to their creator so imported
    # works always surface on the creator page (cheap; usually 0 rows).
    try:
        should_reconcile = not pressure_paused and bool(
            await asyncio.to_thread(
                get_redis().set,
                SCHEDULER_RECONCILE_KEY,
                "1",
                nx=True,
                ex=3600,
            )
        )
        if should_reconcile:
            from app.services.creator_reconcile import reconcile_unlinked_source_creators
            async with async_session() as relink_db:
                res = await reconcile_unlinked_source_creators(relink_db)
                if res["linked"]:
                    await relink_db.commit()
                    logger.info("Auto-sync relinked %d orphaned source_creators", res["linked"])
    except Exception:
        logger.debug("source_creator reconcile skipped", exc_info=True)

    # SQLite maintenance owns a separate exact-time schedule.  Subscription
    # scans only ensure that schedule exists; they never VACUUM inline.
    try:
        from app.jobs.sqlite_maintenance import ensure_sqlite_maintenance_scheduled

        await asyncio.to_thread(ensure_sqlite_maintenance_scheduled)
    except Exception:
        logger.warning("Failed to ensure SQLite maintenance schedule", exc_info=True)

    return {
        "created": jobs_created,
        "skipped": skipped_count,
        "errors": error_count,
        "covered": coverage_count,
        "due": due_count,
        "enqueue_budget": enqueue_budget,
        "due_backlog_deferred": budget_deferred_count,
        # The synchronous RQ wrapper fills the durable successor timestamp.
        "rescheduled_at": None,
        "_scan_interval_minutes": max(scan_minutes, 5),
        "status": "ok" if error_count == 0 else "partial_error",
        "resource_pressure": pressure_snapshot if pressure_paused else None,
    }
