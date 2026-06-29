import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def test_scheduler_uses_source_enqueue_and_does_not_mark_success_on_enqueue():
    from app.jobs import subscription_sync

    src = inspect.getsource(subscription_sync.sync_subscriptions)
    async_src = inspect.getsource(subscription_sync.sync_subscriptions_async)
    locked_src = inspect.getsource(subscription_sync._sync_subscriptions_locked)
    assert "asyncio.run" in src
    assert "redis_lock" in async_src
    assert "enqueue_subscription_source_sync" in locked_src
    assert "last_synced_at = now" not in locked_src
    assert 'Queue(name="scheduled", connection=r).enqueue(' not in locked_src


def test_scheduler_scan_does_not_return_before_reschedule_when_no_stale_candidates():
    from app.jobs import subscription_sync

    locked_src = inspect.getsource(subscription_sync._sync_subscriptions_locked)
    assert "if not candidates:\n                return" not in locked_src
    assert ".enqueue_in(" in locked_src
    assert "rescheduled_at" in locked_src


def test_seed_sync_checks_scheduled_registry():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "seed_sync.py").read_text()
    assert "ScheduledJobRegistry" in src
    assert "StartedJobRegistry" in src
    assert ".enqueue_in(" in src
    assert ".enqueue(\"app.jobs.subscription_sync.sync_subscriptions\")" not in src


def _sub(mode=None, interval=6, times=None):
    return SimpleNamespace(schedule_mode=mode, sync_interval_hours=interval, scheduled_times=times)


def test_fixed_time_never_synced_waits_until_window():
    from app.jobs.subscription_sync import _should_sync_now

    tz = ZoneInfo("Asia/Shanghai")
    config = {
        "schedule_mode": "fixed_time",
        "scheduled_times": "12:40, 18:00:00",
        "scheduler_scan_interval_minutes": 60,
    }
    now = datetime(2026, 6, 5, 12, 20, tzinfo=tz)
    assert _should_sync_now(_sub(), config, None, now, tz) is False


def test_fixed_time_window_triggers_once_and_uses_attempted_at():
    from app.jobs.subscription_sync import _should_sync_now

    tz = ZoneInfo("Asia/Shanghai")
    config = {
        "schedule_mode": "fixed_time",
        "scheduled_times": "12:40",
        "scheduler_scan_interval_minutes": 60,
    }
    now = datetime(2026, 6, 5, 12, 45, tzinfo=tz)
    assert _should_sync_now(_sub(), config, None, now, tz) is True

    attempted = datetime(2026, 6, 5, 12, 41, tzinfo=tz)
    assert _should_sync_now(_sub(), config, None, now, tz, last_attempted_at=attempted) is False


def test_fixed_time_does_not_trigger_before_scheduled_time():
    from app.jobs.subscription_sync import _should_sync_now

    tz = ZoneInfo("Asia/Shanghai")
    config = {
        "schedule_mode": "fixed_time",
        "scheduled_times": "18:00",
        "scheduler_scan_interval_minutes": 60,
    }
    last_synced = datetime(2026, 6, 4, 20, 0, tzinfo=tz)
    now = datetime(2026, 6, 5, 17, 59, tzinfo=tz)
    assert _should_sync_now(_sub(), config, last_synced, now, tz) is False


def test_manual_never_auto_syncs_and_interval_keeps_never_synced_behavior():
    from app.jobs.subscription_sync import _should_sync_now

    tz = ZoneInfo("UTC")
    now = datetime(2026, 6, 5, 8, 0, tzinfo=tz)
    assert _should_sync_now(_sub(mode="manual"), {"schedule_mode": "interval"}, None, now, tz) is False
    assert _should_sync_now(_sub(mode="interval"), {"schedule_mode": "fixed_time"}, None, now, tz) is True


def test_interval_respects_last_synced_cutoff():
    from app.jobs.subscription_sync import _should_sync_now

    tz = ZoneInfo("UTC")
    now = datetime(2026, 6, 5, 8, 0, tzinfo=tz)
    sub = _sub(mode="interval", interval=6)
    assert _should_sync_now(sub, {}, now - timedelta(hours=7), now, tz) is True
    assert _should_sync_now(sub, {}, now - timedelta(hours=2), now, tz) is False


def test_schedule_decision_snapshot_exposes_next_due_for_interval_and_fixed_time():
    from app.jobs.subscription_sync import schedule_decision_snapshot

    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 6, 5, 11, 30, tzinfo=tz)

    interval = schedule_decision_snapshot(
        _sub(mode="interval", interval=6),
        {},
        now - timedelta(hours=2),
        None,
        now,
        tz,
    )
    assert interval["due"] is False
    assert interval["next_due_at"] == datetime(2026, 6, 5, 15, 30, tzinfo=tz).isoformat()

    fixed = schedule_decision_snapshot(
        _sub(mode="fixed_time", times="12:40,18:00"),
        {"scheduler_scan_interval_minutes": 60},
        None,
        None,
        now,
        tz,
    )
    assert fixed["due"] is False
    assert fixed["reason"] == "outside_fixed_time_window"
    assert fixed["next_due_at"] == datetime(2026, 6, 5, 12, 40, tzinfo=tz).isoformat()
    assert fixed["window_start"] == datetime(2026, 6, 5, 12, 40, tzinfo=tz).isoformat()


def test_schedule_decision_snapshot_manual_has_no_next_due():
    from app.jobs.subscription_sync import schedule_decision_snapshot

    tz = ZoneInfo("UTC")
    now = datetime(2026, 6, 5, 8, 0, tzinfo=tz)
    decision = schedule_decision_snapshot(_sub(mode="manual"), {}, None, None, now, tz)
    assert decision["due"] is False
    assert decision["reason"] == "manual_mode"
    assert decision["next_due_at"] is None


def test_system_router_exposes_workbench_and_scheduler_decisions_routes():
    from app.api.system import router

    paths = {route.path for route in router.routes}
    assert "/system/workbench" in paths
    assert "/system/scheduler-decisions" in paths


def test_rq_can_import_scheduler_job_path():
    from rq.utils import import_attribute

    fn = import_attribute("app.jobs.subscription_sync.sync_subscriptions")
    assert callable(fn)
