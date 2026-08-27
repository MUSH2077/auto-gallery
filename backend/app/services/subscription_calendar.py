"""Pure calendar recurrence helpers for subscription scheduling.

Calendar schedules are deliberately bounded to daily, weekly, and monthly
rules.  They are evaluated in the configured IANA timezone and never replay a
slot after its lateness window has elapsed.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone
from typing import Any


def _parse_time(value: str) -> time | None:
    try:
        parts = [int(part) for part in str(value).strip().split(":")]
    except (TypeError, ValueError):
        return None
    if len(parts) not in {2, 3}:
        return None
    hour, minute = parts[:2]
    second = parts[2] if len(parts) == 3 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    return time(hour, minute, second)


def daily_rule_from_legacy(times: str | None) -> dict[str, Any] | None:
    parsed = sorted({
        parsed_time.isoformat()
        for raw in (times or "").split(",")
        if (parsed_time := _parse_time(raw)) is not None
    })
    if not parsed:
        return None
    return {"frequency": "daily", "times": parsed}


def effective_calendar_rule(subject: Any, config: dict[str, Any]) -> dict[str, Any] | None:
    subject_mode = getattr(subject, "schedule_mode", None)
    uses_own_rule = subject_mode in {"calendar", "fixed_time"}
    rule = (
        getattr(subject, "schedule_rule", None)
        if uses_own_rule
        else config.get("schedule_rule")
    )
    if isinstance(rule, dict) and rule.get("frequency") in {"daily", "weekly", "monthly"}:
        return dict(rule)
    return daily_rule_from_legacy(
        (
            getattr(subject, "scheduled_times", None)
            if uses_own_rule
            else config.get("scheduled_times")
        )
    )


def _valid_local(day: date, value: time, tz) -> datetime | None:
    """Return one real local instant; skip spring-forward wall-clock gaps."""

    candidate = datetime.combine(day, value, tzinfo=tz).replace(fold=0)
    round_trip = candidate.astimezone(timezone.utc).astimezone(tz)
    if round_trip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
        return None
    return candidate


def _days_for_date(rule: dict[str, Any], day: date) -> bool:
    frequency = rule.get("frequency")
    if frequency == "daily":
        return True
    if frequency == "weekly":
        weekdays = {int(value) for value in rule.get("weekdays") or []}
        return day.isoweekday() in weekdays
    if frequency == "monthly":
        last_day = calendar.monthrange(day.year, day.month)[1]
        selected = set()
        for value in rule.get("month_days") or []:
            requested = int(value)
            if 1 <= requested <= 31:
                selected.add(min(requested, last_day))
        return day.day in selected
    return False


def occurrences_on(rule: dict[str, Any], day: date, tz) -> tuple[datetime, ...]:
    if not _days_for_date(rule, day):
        return ()
    values = []
    for raw in rule.get("times") or []:
        parsed = _parse_time(str(raw))
        if parsed is None:
            continue
        candidate = _valid_local(day, parsed, tz)
        if candidate is not None:
            values.append(candidate)
    return tuple(sorted(set(values)))


def next_calendar_occurrence(
    rule: dict[str, Any],
    after: datetime,
    tz,
) -> datetime | None:
    local_after = after.astimezone(tz)
    for offset in range(0, 800):
        day = local_after.date() + timedelta(days=offset)
        for candidate in occurrences_on(rule, day, tz):
            if candidate > local_after:
                return candidate
    return None


def _latest_within(
    rule: dict[str, Any],
    now: datetime,
    tz,
    grace: timedelta,
) -> datetime | None:
    local_now = now.astimezone(tz)
    earliest = local_now - grace
    days = max(1, grace.days + 2)
    candidates = [
        candidate
        for offset in range(days + 1)
        for candidate in occurrences_on(rule, local_now.date() - timedelta(days=offset), tz)
        if earliest <= candidate <= local_now
    ]
    return max(candidates, default=None)


def _latest_occurrence(rule: dict[str, Any], now: datetime, tz) -> datetime | None:
    local_now = now.astimezone(tz)
    for offset in range(0, 800):
        day = local_now.date() - timedelta(days=offset)
        candidates = [
            candidate
            for candidate in occurrences_on(rule, day, tz)
            if candidate <= local_now
        ]
        if candidates:
            return max(candidates)
    return None


def calendar_decision(
    rule: dict[str, Any],
    *,
    now: datetime,
    tz,
    scan_minutes: int,
    persisted_next_sync_at: datetime | None = None,
    last_synced_at: datetime | None = None,
    last_attempted_at: datetime | None = None,
    created_at: datetime | None = None,
    legacy_fixed_time: bool = False,
) -> dict[str, Any]:
    local_now = now.astimezone(tz)
    grace = timedelta(minutes=max(5, int(scan_minutes)) + 5)

    def local(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(tz)

    persisted = local(persisted_next_sync_at)
    synced = local(last_synced_at)
    attempted = local(last_attempted_at)
    created = local(created_at)

    if persisted is not None and persisted > local_now:
        return {
            "due": False,
            "reason": "fixed_time_not_reached" if legacy_fixed_time else "calendar_not_reached",
            "next_due_at": persisted.isoformat(),
        }

    slot = persisted if persisted is not None else _latest_within(rule, local_now, tz, grace)
    if slot is not None and created is not None and slot < created:
        slot = None

    if slot is not None and local_now <= slot + grace:
        payload = {
            "scheduled_for": slot.isoformat(),
            "scheduled_time": slot.time().isoformat(),
            "next_due_at": slot.isoformat(),
            "window_start": slot.isoformat(),
            "window_end": (slot + grace).isoformat(),
        }
        if synced is not None and synced >= slot:
            next_slot = next_calendar_occurrence(rule, local_now, tz)
            return {
                **payload,
                "due": False,
                "reason": "already_synced_in_slot",
                "next_due_at": next_slot.isoformat() if next_slot else None,
            }
        if attempted is not None and attempted >= slot:
            next_slot = next_calendar_occurrence(rule, local_now, tz)
            return {
                **payload,
                "due": False,
                "reason": "already_attempted_in_slot",
                "next_due_at": next_slot.isoformat() if next_slot else None,
            }
        return {
            **payload,
            "due": True,
            "reason": "fixed_time_backlog_due" if legacy_fixed_time else "calendar_due",
        }

    next_slot = next_calendar_occurrence(rule, local_now, tz)
    previous_slot = persisted or _latest_occurrence(rule, local_now, tz)
    missed = bool(
        previous_slot is not None
        and previous_slot <= local_now - grace
        and (created is None or previous_slot >= created)
        and (synced is None or synced < previous_slot)
        and (attempted is None or attempted < previous_slot)
    )
    return {
        "due": False,
        "reason": (
            "calendar_missed_skipped"
            if missed
            else ("fixed_time_not_reached" if legacy_fixed_time else "calendar_not_reached")
        ),
        "next_due_at": next_slot.isoformat() if next_slot else None,
        "missed_slot": previous_slot.isoformat() if missed and previous_slot else None,
        "window_start": next_slot.isoformat() if next_slot else None,
        "window_end": (next_slot + grace).isoformat() if next_slot else None,
    }
