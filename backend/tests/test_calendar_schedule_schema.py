from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import JSONB


def test_subscription_schema_accepts_structured_weekly_and_monthly_rules():
    from app.schemas.subscription import SubscriptionUpdate

    weekly = SubscriptionUpdate(
        schedule_mode="calendar",
        schedule_rule={
            "frequency": "weekly",
            "weekdays": [5, 1, 5],
            "times": ["21:00", "09:00:00"],
        },
    )
    monthly = SubscriptionUpdate(
        schedule_mode="calendar",
        schedule_rule={
            "frequency": "monthly",
            "month_days": [31, 15],
            "times": ["22:00"],
            "overflow": "last_day",
        },
    )

    assert weekly.schedule_rule.model_dump() == {
        "frequency": "weekly",
        "weekdays": [1, 5],
        "times": ["09:00:00", "21:00:00"],
    }
    assert monthly.schedule_rule.model_dump() == {
        "frequency": "monthly",
        "month_days": [15, 31],
        "times": ["22:00:00"],
        "overflow": "last_day",
    }


def test_subscription_schema_normalizes_legacy_fixed_time_to_daily_calendar():
    from app.schemas.subscription import SubscriptionUpdate

    value = SubscriptionUpdate(
        schedule_mode="fixed_time",
        scheduled_times="21:00, 09:00",
    )

    assert value.schedule_mode == "calendar"
    assert value.schedule_rule.model_dump() == {
        "frequency": "daily",
        "times": ["09:00:00", "21:00:00"],
    }


def test_calendar_rule_rejects_missing_days_and_invalid_times():
    from app.schemas.subscription import SubscriptionUpdate

    with pytest.raises(ValidationError):
        SubscriptionUpdate(
            schedule_mode="calendar",
            schedule_rule={"frequency": "weekly", "weekdays": [], "times": ["09:00"]},
        )
    with pytest.raises(ValidationError):
        SubscriptionUpdate(
            schedule_mode="calendar",
            schedule_rule={"frequency": "daily", "times": ["25:00"]},
        )


def test_subscription_model_and_head_migration_store_schedule_rule_as_jsonb():
    from app.models.subscription import Subscription

    assert isinstance(Subscription.__table__.columns.schedule_rule.type, JSONB)
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "a7c9e1f3b5d7_add_calendar_subscription_rules.py"
    ).read_text()
    assert 'down_revision: Union[str, None] = "f4c6d8e0a2b3"' in migration
    assert "schedule_rule" in migration
    assert "'calendar'" in migration


def test_admin_subscription_defaults_normalize_legacy_fixed_time():
    from app.api.admin.settings import SubscriptionDefaults

    defaults = SubscriptionDefaults(
        schedule_mode="fixed_time",
        scheduled_times="22:00",
    )

    assert defaults.schedule_mode == "calendar"
    assert defaults.schedule_rule.model_dump() == {
        "frequency": "daily",
        "times": ["22:00:00"],
    }


def test_inherited_schedule_ignores_a_stale_subscription_rule():
    from app.services.subscription_calendar import effective_calendar_rule

    subject = SimpleNamespace(
        schedule_mode=None,
        schedule_rule={
            "frequency": "weekly",
            "weekdays": [1],
            "times": ["01:00:00"],
        },
        scheduled_times="01:00:00",
    )
    config = {
        "schedule_mode": "calendar",
        "schedule_rule": {
            "frequency": "monthly",
            "month_days": [15],
            "times": ["09:30:00"],
        },
    }

    assert effective_calendar_rule(subject, config) == config["schedule_rule"]
