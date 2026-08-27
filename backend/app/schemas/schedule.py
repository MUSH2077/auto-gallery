from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator


def normalize_clock_times(values: Any) -> list[str]:
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, (list, tuple, set)):
        raise ValueError("times must be a non-empty list")
    normalized: set[str] = set()
    for raw in values:
        parts = str(raw).strip().split(":")
        if len(parts) not in {2, 3}:
            raise ValueError("times must use HH:MM or HH:MM:SS")
        try:
            hour, minute = int(parts[0]), int(parts[1])
            second = int(parts[2]) if len(parts) == 3 else 0
        except ValueError as exc:
            raise ValueError("times must use HH:MM or HH:MM:SS") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            raise ValueError("times contain an invalid clock value")
        normalized.add(f"{hour:02d}:{minute:02d}:{second:02d}")
    if not normalized:
        raise ValueError("times must contain at least one value")
    return sorted(normalized)


class _TimedRule(BaseModel):
    times: list[str]

    @field_validator("times", mode="before")
    @classmethod
    def validate_times(cls, value: Any) -> list[str]:
        return normalize_clock_times(value)


class DailyScheduleRule(_TimedRule):
    frequency: Literal["daily"] = "daily"


class WeeklyScheduleRule(_TimedRule):
    frequency: Literal["weekly"] = "weekly"
    weekdays: list[int] = Field(min_length=1)

    @field_validator("weekdays", mode="before")
    @classmethod
    def validate_weekdays(cls, value: Any) -> list[int]:
        days = sorted({int(day) for day in (value or [])})
        if not days or any(day < 1 or day > 7 for day in days):
            raise ValueError("weekdays must contain ISO weekday values 1 through 7")
        return days


class MonthlyScheduleRule(_TimedRule):
    frequency: Literal["monthly"] = "monthly"
    month_days: list[int] = Field(min_length=1)
    overflow: Literal["last_day"] = "last_day"

    @field_validator("month_days", mode="before")
    @classmethod
    def validate_month_days(cls, value: Any) -> list[int]:
        days = sorted({int(day) for day in (value or [])})
        if not days or any(day < 1 or day > 31 for day in days):
            raise ValueError("month_days must contain values 1 through 31")
        return days


CalendarScheduleRule = Annotated[
    DailyScheduleRule | WeeklyScheduleRule | MonthlyScheduleRule,
    Field(discriminator="frequency"),
]


def normalize_legacy_schedule_payload(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    payload = dict(value)
    if payload.get("schedule_mode") == "fixed_time":
        payload["schedule_mode"] = "calendar"
        if not payload.get("schedule_rule"):
            payload["schedule_rule"] = {
                "frequency": "daily",
                "times": normalize_clock_times(payload.get("scheduled_times") or []),
            }
    elif payload.get("schedule_rule") and not payload.get("schedule_mode"):
        payload["schedule_mode"] = "calendar"
    return payload
