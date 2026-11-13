"""Pure subscription schedule policy shared by queries and workers."""

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from app.services.subscription_calendar import (calendar_decision, effective_calendar_rule, next_calendar_occurrence)

FALLBACK_INTERVAL_HOURS = 6
FALLBACK_SCAN_MINUTES = 60

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
    persisted_next_sync_at: datetime | None = None,
) -> dict:
    """Return a scheduler decision with a reason and optional fixed-time window."""
    mode = sub.schedule_mode or system_config.get("schedule_mode", "interval")

    if mode == "manual":
        return {"due": False, "mode": mode, "reason": "manual_mode"}

    if mode in {"fixed_time", "calendar"}:
        rule = effective_calendar_rule(sub, system_config)
        if rule:
            return {
                "mode": "calendar" if mode == "calendar" else mode,
                **calendar_decision(
                    rule,
                    now=now,
                    tz=tz,
                    scan_minutes=int(system_config.get(
                        "scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES
                    )),
                    persisted_next_sync_at=persisted_next_sync_at,
                    last_synced_at=last_synced_at,
                    last_attempted_at=last_attempted_at,
                    created_at=getattr(sub, "created_at", None),
                    legacy_fixed_time=mode == "fixed_time",
                ),
            }

    persisted_due = _as_tz(persisted_next_sync_at, tz)
    if persisted_due is not None and persisted_due <= now:
        return {
            "due": True,
            "mode": mode,
            "reason": "interval_backlog_due",
            "scheduled_for": persisted_due.isoformat(),
        }

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

        last_synced = _as_tz(last_synced_at, tz)
        last_attempted = _as_tz(last_attempted_at, tz)
        created_at = _as_tz(getattr(sub, "created_at", None), tz)
        latest_slot = max(
            (
                datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=tz)
                for day_offset in (-1, 0)
                for day in (now.date() + timedelta(days=day_offset),)
                for hour, minute, second in scheduled
                if datetime(
                    day.year,
                    day.month,
                    day.day,
                    hour,
                    minute,
                    second,
                    tzinfo=tz,
                ) <= now
                and (
                    created_at is None
                    or datetime(
                        day.year,
                        day.month,
                        day.day,
                        hour,
                        minute,
                        second,
                        tzinfo=tz,
                    ) >= created_at
                )
            ),
            default=None,
        )
        if latest_slot is None:
            return {"due": False, "mode": mode, "reason": "fixed_time_not_reached"}
        payload = {
            "mode": mode,
            "scheduled_for": latest_slot.isoformat(),
            "scheduled_time": latest_slot.time().isoformat(),
        }
        if last_synced and last_synced >= latest_slot:
            return {**payload, "due": False, "reason": "already_synced_in_slot"}
        if last_attempted and last_attempted >= latest_slot:
            return {**payload, "due": False, "reason": "already_attempted_in_slot"}
        return {**payload, "due": True, "reason": "fixed_time_backlog_due"}

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


def _should_sync_now(
    sub,
    system_config: dict,
    last_synced_at,
    now: datetime,
    tz,
    last_attempted_at=None,
    persisted_next_sync_at=None,
) -> bool:
    """Check whether a sync is due, respecting per-subscription schedule strategy.

    Priority: subscription value > system default > hardcoded fallback.
    sub.schedule_mode=None means inherit from system_config.

    Modes: 'manual'=never, 'interval'=every N hours, 'fixed_time'=at scheduled times.
    """
    return bool(_schedule_decision(
        sub,
        system_config,
        last_synced_at,
        last_attempted_at,
        now,
        tz,
        persisted_next_sync_at,
    )["due"])


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

    synced = _as_tz(last_synced_at, tz)
    attempted = _as_tz(last_attempted_at, tz)
    created_at = _as_tz(getattr(sub, "created_at", None), tz)
    candidates: list[datetime] = []
    for day_offset in (-1, 0, 1):
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
            if created_at is None or start >= created_at:
                candidates.append(start)

    elapsed = sorted((candidate for candidate in candidates if candidate <= now), reverse=True)
    if elapsed:
        latest_slot = elapsed[0]
        published = bool(
            (synced and synced >= latest_slot)
            or (attempted and attempted >= latest_slot)
        )
        if not published:
            # Persist the logical slot itself, rather than the scan time. This
            # makes backlog age and ordering truthful after downtime/pressure.
            return _utc(latest_slot)

    future = sorted(candidate for candidate in candidates if candidate > now)
    if future:
        return _utc(future[0])

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
    if mode in {"fixed_time", "calendar"}:
        rule = effective_calendar_rule(sub, system_config)
        if rule:
            decision = calendar_decision(
                rule,
                now=local_now,
                tz=tz,
                scan_minutes=int(system_config.get(
                    "scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES
                )),
                last_synced_at=last_synced_at,
                last_attempted_at=last_attempted_at,
                created_at=getattr(sub, "created_at", None),
            )
            next_due = decision.get("next_due_at")
            return _utc(datetime.fromisoformat(next_due)) if next_due else None
        return _next_fixed_check_at(
            sub, system_config, last_synced_at, last_attempted_at, local_now, tz
        )
    return _next_interval_check_at(
        sub,
        system_config,
        last_synced_at,
        last_attempted_at,
        local_now,
        tz,
    )


def next_future_subscription_check_at(
    sub,
    system_config: dict,
    now: datetime,
    tz=None,
) -> datetime | None:
    """Return the first future slot when a manual subscription becomes automatic."""

    if tz is None:
        try:
            tz = ZoneInfo(system_config.get("timezone", "UTC"))
        except Exception:
            tz = timezone.utc
    local_now = _as_tz(now, tz) or datetime.now(tz)
    mode = sub.schedule_mode or system_config.get("schedule_mode", "interval")
    if mode == "manual":
        return None
    if mode not in {"fixed_time", "calendar"}:
        interval_hours = sub.sync_interval_hours or int(
            system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS)
        )
        return _utc(local_now + timedelta(hours=max(1, int(interval_hours))))
    rule = effective_calendar_rule(sub, system_config)
    if not rule:
        interval_hours = sub.sync_interval_hours or int(
            system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS)
        )
        return _utc(local_now + timedelta(hours=max(1, int(interval_hours))))
    occurrence = next_calendar_occurrence(rule, local_now, tz)
    return _utc(occurrence) if occurrence else None


def schedule_decision_snapshot(
    sub,
    system_config: dict,
    last_synced_at,
    last_attempted_at,
    now: datetime,
    tz,
    persisted_next_sync_at: datetime | None = None,
) -> dict:
    """Read-only schedule explanation used by APIs and tests.

    This mirrors the enqueue scanner decision without mutating any source state.
    """
    decision = _schedule_decision(
        sub,
        system_config,
        last_synced_at,
        last_attempted_at,
        now,
        tz,
        persisted_next_sync_at,
    )
    mode = decision.get("mode") or sub.schedule_mode or system_config.get("schedule_mode", "interval")
    scan_minutes = int(system_config.get("scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES))

    persisted_due = _as_tz(persisted_next_sync_at, tz)
    next_due_at = persisted_due.isoformat() if persisted_due is not None else None
    window_start = decision.get("window_start")
    window_end = decision.get("window_end")

    if decision.get("next_due_at"):
        next_due_at = str(decision["next_due_at"])
    elif decision.get("due") and decision.get("scheduled_for"):
        next_due_at = str(decision["scheduled_for"])
    elif mode == "interval":
        if last_synced_at:
            interval_hours = sub.sync_interval_hours or int(
                system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
            next_due = _as_tz(last_synced_at, tz) + timedelta(hours=interval_hours)
            next_due_at = next_due.isoformat()
        else:
            next_due_at = now.isoformat()
    elif mode in {"fixed_time", "calendar"}:
        if decision.get("next_due_at"):
            next_due_at = decision["next_due_at"]
            window_start = window_start or decision.get("window_start")
            window_end = window_end or decision.get("window_end")
        else:
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
