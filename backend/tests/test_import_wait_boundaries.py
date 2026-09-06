from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.jobs import import_runner
from app.services import stage_metrics


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "cancel"])
async def test_artifact_contention_wakes_and_observes_control(monkeypatch, command):
    listener = SimpleNamespace(command=None, reason="operator request")
    job_id = str(uuid4())
    calls = []

    async def wake(workload, seconds, *, task_id):
        calls.append((workload, seconds, task_id))
        listener.command = command

    monkeypatch.setattr("app.services.heavy_io._wait_for_resource_event", wake)
    token = import_runner._import_control.set(listener)
    try:
        with pytest.raises(import_runner._ImportControlRequested) as stopped:
            await import_runner._wait_for_import_artifacts(f"{job_id}:{uuid4()}")
        assert stopped.value.command == command
        assert calls == [("import_db", 2.0, job_id)]
    finally:
        import_runner._import_control.reset(token)


def test_retry_queue_timing_uses_current_dispatch_eligibility():
    now = datetime.now(timezone.utc)
    job = SimpleNamespace(created_at=now - timedelta(hours=2), execution_attempt=1)
    dispatch = {"prepared_at": (now - timedelta(seconds=40)).isoformat(),
                "available_at": (now - timedelta(seconds=10)).isoformat()}
    assert import_runner._import_queue_wait_seconds(job, dispatch, now) == 10


def test_retry_without_a_current_dispatch_has_unknown_queue_time():
    now = datetime.now(timezone.utc)
    job = SimpleNamespace(created_at=now - timedelta(hours=2), execution_attempt=1)
    assert import_runner._import_queue_wait_seconds(job, {}, now) is None
    assert import_runner._import_queue_wait_seconds(
        job, {"prepared_at": job.created_at.isoformat(), "claimed_at": (now - timedelta(hours=1)).isoformat()}, now,
    ) is None
    with stage_metrics.import_execution_metrics(uuid4(), uuid4(), queue_wait_seconds=None) as metrics:
        assert "queue_wait" not in metrics.seconds


def test_legacy_first_attempt_uses_creation_boundary():
    now = datetime.now(timezone.utc)
    job = SimpleNamespace(created_at=(now - timedelta(seconds=5)).replace(tzinfo=None),
                          execution_attempt=0)
    assert import_runner._import_queue_wait_seconds(job, {}, now) == 5
