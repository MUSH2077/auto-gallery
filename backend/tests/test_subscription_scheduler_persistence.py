import inspect
from pathlib import Path


def test_subscription_source_has_persistent_ordered_due_index():
    from app.models.subscription_source import SubscriptionSource

    assert "next_sync_at" in SubscriptionSource.__table__.columns
    index = next(
        item
        for item in SubscriptionSource.__table__.indexes
        if item.name == "ix_subscription_sources_next_sync_due"
    )
    assert [column.name for column in index.columns] == ["next_sync_at", "id"]


def test_next_sync_migration_follows_import_lease_head_and_is_concurrent():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "e8b0c2d4f6a9_add_subscription_next_sync_at.py"
    ).read_text()

    assert 'down_revision: Union[str, None] = "e7a9c1d3f5b8"' in migration
    assert "ADD COLUMN IF NOT EXISTS next_sync_at TIMESTAMPTZ" in migration
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in migration
    assert "next_sync_at ASC NULLS FIRST, id ASC" in migration
    assert "WHERE is_enabled IS TRUE" in migration


def test_subscription_setting_mutations_replan_only_schedule_changes():
    from app.services.subscription import SubscriptionService
    from app.api.admin import settings

    update_subscription = inspect.getsource(SubscriptionService.update_subscription)
    update_source = inspect.getsource(SubscriptionService.update_source)
    put_setting = inspect.getsource(settings._put_setting)

    assert "SCHEDULE_FIELDS.intersection(data)" in update_subscription
    assert "replan_subscription_sources" in update_subscription
    assert "ss.next_sync_at = None" in update_source
    assert "subscription_schedule_changed" in put_setting
    assert "replan_inherited_subscription_sources" in put_setting
    assert ".values(next_sync_at=None)" not in put_setting


def test_subscription_schedule_change_ignores_enablement_and_scan_cadence():
    from app.services.subscription_replan import subscription_schedule_changed

    original = {
        "scheduler_enabled": True,
        "scheduler_scan_interval_minutes": 5,
        "schedule_mode": "calendar",
        "schedule_rule": {"frequency": "daily", "times": ["22:00:00"]},
        "timezone": "Asia/Shanghai",
        "default_sync_interval_hours": 6,
    }
    disabled = {
        **original,
        "scheduler_enabled": False,
        "scheduler_scan_interval_minutes": 60,
    }

    assert subscription_schedule_changed(original, disabled) is False
    assert subscription_schedule_changed(
        original,
        {
            **disabled,
            "schedule_rule": {"frequency": "weekly", "weekdays": [1], "times": ["22:00:00"]},
        },
    ) is True
    assert subscription_schedule_changed(
        original,
        {**disabled, "timezone": "UTC"},
    ) is True


def test_success_invalidates_and_enqueue_claims_due_before_publication():
    from app.services import subscription_enqueue

    success = inspect.getsource(subscription_enqueue.mark_source_sync_success)
    enqueue = inspect.getsource(subscription_enqueue.enqueue_subscription_source_sync)
    assert "ss.next_sync_at = None" in success
    assert "scheduler_config: dict | None = None" in enqueue
    assert "next_sync_at = next_user_subscription_check_at" in enqueue
    assert enqueue.index("selection.binding.next_sync_at = next_sync_at") < enqueue.index(
        "await publish_prepared_download("
    )
    assert "sql_update" not in enqueue
