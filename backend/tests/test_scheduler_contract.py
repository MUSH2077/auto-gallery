import inspect


def test_scheduler_uses_source_enqueue_and_does_not_mark_success_on_enqueue():
    from app.jobs import subscription_sync

    src = inspect.getsource(subscription_sync.sync_subscriptions)
    locked_src = inspect.getsource(subscription_sync._sync_subscriptions_locked)
    assert "redis_lock" in src
    assert "enqueue_subscription_source_sync" in locked_src
    assert "last_synced_at = now" not in locked_src
    assert 'Queue(name="scheduled", connection=r).enqueue(' not in locked_src


def test_seed_sync_checks_scheduled_registry():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "seed_sync.py").read_text()
    assert "ScheduledJobRegistry" in src
