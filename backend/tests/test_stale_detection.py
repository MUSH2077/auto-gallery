"""Low-load contracts for the single Redis-TTL stale detector."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _Database:
    def __init__(self, responses):
        self.responses = list(responses)
        self.execute_count = 0
        self.commit_count = 0
        self.added = []

    async def execute(self, _statement):
        self.execute_count += 1
        return _Rows(self.responses.pop(0))

    async def flush(self):
        return None

    async def commit(self):
        self.commit_count += 1

    def add(self, value):
        self.added.append(value)


class _Pipeline:
    def __init__(self, values=None, error: Exception | None = None):
        self.values = list(values or [])
        self.error = error
        self.keys = []

    def exists(self, key):
        self.keys.append(key)
        return self

    def execute(self):
        if self.error is not None:
            raise self.error
        return list(self.values)


class _Redis:
    def __init__(self, values=None, error: Exception | None = None):
        self.pipeline_instance = _Pipeline(values, error)

    def pipeline(self, *, transaction):
        assert transaction is False
        return self.pipeline_instance


def test_heartbeat_presence_is_one_pipeline_round_trip():
    from app.services.task_engine import _heartbeat_presence

    identities = [uuid4(), uuid4(), uuid4()]
    redis = _Redis([1, 0, 1])
    assert _heartbeat_presence(redis, identities) == [True, False, True]
    assert redis.pipeline_instance.keys == [
        f"task:{identity}:heartbeat_ts" for identity in identities
    ]


def test_startup_grace_uses_started_or_created_time():
    from app.services.task_engine import _outside_start_grace

    now = datetime.now(timezone.utc)
    assert not _outside_start_grace(now - timedelta(seconds=30), now, grace_seconds=90)
    assert _outside_start_grace(now - timedelta(seconds=91), now, grace_seconds=90)
    assert not _outside_start_grace(None, now, grace_seconds=90)


@pytest.mark.asyncio
async def test_unreadable_redis_never_marks_an_old_active_task_stale():
    from app.services.task_engine import TaskEngine

    now = datetime.now(timezone.utc)
    job_id = uuid4()
    db = _Database((
        ((job_id, now - timedelta(minutes=10)),),
        (),
    ))
    count = await TaskEngine(db).detect_stale_tasks(
        redis_client=_Redis(error=ConnectionError("redis unavailable")),
        now=now,
    )
    assert count == 0
    assert db.execute_count == 2
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_import_stale_transition_updates_parent_tasks_and_outbox(monkeypatch):
    import app.services.task_engine as task_engine
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.models.task_run import TaskRun
    from app.services.task_engine import TaskEngine

    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=10)
    subscription_id = uuid4()
    parent = DownloadJob(
        id=uuid4(),
        subscription_id=subscription_id,
        source="test",
        source_url="https://example.invalid/source",
        status="importing",
        created_at=old,
        updated_at=old,
    )
    import_job = ImportJob(
        id=uuid4(),
        download_job_id=parent.id,
        status="running",
        created_at=old,
        updated_at=old,
    )
    import_task = TaskRun(
        id=uuid4(),
        kind="import",
        operation_type="import",
        subject_type="import_job",
        subject_id=import_job.id,
        status="running",
        resource_state="running",
        created_at=old,
        updated_at=old,
        started_at=old,
    )
    parent_task = TaskRun(
        id=uuid4(),
        kind="download",
        operation_type="download",
        subject_type="download_job",
        subject_id=parent.id,
        status="running",
        resource_state="running",
        created_at=old,
        updated_at=old,
        started_at=old,
    )
    db = _Database((
        (),
        ((import_job.id, old),),
        ((import_job, import_task, parent, parent_task),),
    ))

    projection_requests = []

    async def request_projection(db_arg, *args, **kwargs):
        projection_requests.append((db_arg, args, kwargs))
        return 1

    notifications = []

    def publish(*args, **_kwargs):
        # Pub/sub must happen only after all relational projections commit.
        assert db.commit_count == 1
        notifications.append(args)

    monkeypatch.setattr(task_engine, "request_search_projection", request_projection)
    monkeypatch.setattr(
        task_engine.TaskEventPublisher,
        "publish_status_change",
        publish,
    )

    count = await TaskEngine(db).detect_stale_tasks(
        redis_client=_Redis([0]),
        now=now,
    )

    assert count == 2
    assert import_job.status == "stale"
    assert parent.status == "stale"
    assert import_task.status == "stale"
    assert parent_task.status == "stale"
    assert db.commit_count == 1
    assert projection_requests[0][2]["subscription_ids"] == {subscription_id}
    assert {(event[1], event[3]) for event in notifications} == {
        ("import", "stale"),
        ("download", "stale"),
    }


def test_only_task_engine_owns_running_task_stale_detection():
    backend = Path(__file__).resolve().parents[1]
    subscription_sync = (backend / "app/jobs/subscription_sync.py").read_text()
    import_recovery = (backend / "app/services/import_recovery.py").read_text()
    main = (backend / "app/main.py").read_text()

    assert "heartbeat_ts" not in subscription_sync
    assert "Marked download job %s as stale" not in subscription_sync
    assert 'ImportJob.status == "running"' not in import_recovery
    assert "await asyncio.sleep(90)" in main
