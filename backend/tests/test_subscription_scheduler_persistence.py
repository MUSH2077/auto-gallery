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


def test_subscription_setting_mutations_invalidate_persisted_due_time():
    from app.services.subscription import SubscriptionService
    from app.api.admin import settings

    update_subscription = inspect.getsource(SubscriptionService.update_subscription)
    update_source = inspect.getsource(SubscriptionService.update_source)
    put_setting = inspect.getsource(settings._put_setting)

    assert "SCHEDULE_FIELDS.intersection(data)" in update_subscription
    assert ".values(next_sync_at=None)" in update_subscription
    assert "ss.next_sync_at = None" in update_source
    assert 'key == "subscription_defaults" and changed' in put_setting
    assert ".values(next_sync_at=None)" in put_setting


def test_success_invalidates_and_enqueue_advances_persisted_due_time():
    from app.services import subscription_enqueue

    success = inspect.getsource(subscription_enqueue.mark_source_sync_success)
    enqueue = inspect.getsource(subscription_enqueue.enqueue_subscription_source_sync)
    assert "ss.next_sync_at = None" in success
    assert "scheduler_config: dict | None = None" in enqueue
    assert "ss.next_sync_at = next_subscription_check_at" in enqueue
