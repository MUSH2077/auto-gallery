import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


class _FakeQueue:
    calls = []

    def __init__(self, name="default", connection=None):
        self.name = name
        self.connection = connection
        _FakeQueue.calls.append(("init", name))

    def enqueue(self, *args, **kwargs):
        _FakeQueue.calls.append(("enqueue", self.name, args, kwargs))
        return SimpleNamespace(id="rq-job")

    def enqueue_in(self, *args, **kwargs):
        _FakeQueue.calls.append(("enqueue_in", self.name, args, kwargs))
        return SimpleNamespace(id="rq-job")


class _FakeRedis:
    @staticmethod
    def from_url(_url):
        return object()


def _patch_queue(monkeypatch):
    _FakeQueue.calls = []
    monkeypatch.setattr("redis.from_url", _FakeRedis.from_url)
    monkeypatch.setattr("rq.Queue", _FakeQueue)


class _FakeDownloadRepo:
    def __init__(self, job):
        self.job = job

    async def get(self, _job_id):
        return self.job

    async def create(self, data):
        self.job = SimpleNamespace(
            id=uuid4(),
            subscription_id=None,
            status=data["status"],
            source_url=data["source_url"],
            manifest=None,
        )
        return self.job

    async def update_status(self, job, status):
        job.status = status
        return job


class _FakeDownloadDB:
    async def commit(self):
        pass


def test_manual_download_retry_and_resume_use_downloads_queue(monkeypatch):
    from app.services import download as download_module

    _patch_queue(monkeypatch)
    monkeypatch.setattr(download_module.registry, "get", lambda _source: SimpleNamespace(
        normalize_url=lambda url: url,
        validate_url=lambda _url: True,
    ))

    service = download_module.DownloadService(_FakeDownloadDB())
    service.repo = _FakeDownloadRepo(SimpleNamespace(
        id=uuid4(),
        subscription_id=None,
        status="failed",
        source_url="https://example.test/a",
        manifest=None,
        error_log=None,
    ))

    asyncio.run(service.create_job({"subscription_id": uuid4(), "source": "pixiv", "source_url": "https://example.test/a"}))
    service.repo.job.status = "failed"
    asyncio.run(service.retry_job(service.repo.job.id))
    service.repo.job.status = "paused"
    asyncio.run(service.resume_job(service.repo.job.id))

    enqueue_calls = [call for call in _FakeQueue.calls if call[0] == "enqueue"]
    assert enqueue_calls
    assert {call[1] for call in enqueue_calls} == {"downloads"}


def test_import_retry_uses_imports_queue(monkeypatch):
    from app.api import import_jobs

    _patch_queue(monkeypatch)
    job = SimpleNamespace(id=uuid4(), status="failed", error_log="boom")

    class Result:
        def scalar_one_or_none(self):
            return job

    class DB:
        async def execute(self, _stmt):
            return Result()

        async def commit(self):
            pass

    asyncio.run(import_jobs.retry_import_job(job.id, db=DB()))

    enqueue_calls = [call for call in _FakeQueue.calls if call[0] == "enqueue"]
    assert enqueue_calls[-1][1] == "imports"


def test_danbooru_url_batch_import_uses_imports_queue(monkeypatch):
    from app.api import reference

    _patch_queue(monkeypatch)
    monkeypatch.setattr(reference, "Queue", _FakeQueue)

    payload = asyncio.run(reference.url_batch_import_danbooru({"urls": ["https://www.pixiv.net/users/1980643"]}))

    assert payload["status"] == "ok"
    enqueue_calls = [call for call in _FakeQueue.calls if call[0] == "enqueue"]
    assert enqueue_calls[-1][1] == "imports"


def test_subscription_patch_can_clear_schedule_fields(monkeypatch):
    from app.api import subscriptions
    from app.schemas.subscription import SubscriptionUpdate

    captured = {}

    class FakeService:
        def __init__(self, _db):
            pass

        async def update_subscription(self, subscription_id, data):
            captured["subscription_id"] = subscription_id
            captured["data"] = data
            return SimpleNamespace(id=subscription_id)

    monkeypatch.setattr(subscriptions, "SubscriptionService", FakeService)
    sub_id = uuid4()

    asyncio.run(subscriptions.update_subscription(
        sub_id,
        SubscriptionUpdate(schedule_mode=None, scheduled_times=None),
        db=object(),
    ))

    assert captured["data"] == {"schedule_mode": None, "scheduled_times": None}


def test_settings_reschedule_replaces_pending_sync_scan(monkeypatch):
    from app.api import admin

    class FakeScheduledQueue:
        scheduled_jobs = {
            "old-sync": SimpleNamespace(id="old-sync", func_name="app.jobs.subscription_sync.sync_subscriptions"),
            "other": SimpleNamespace(id="other", func_name="app.jobs.backup.run_auto_backup"),
        }
        queued_jobs = [
            SimpleNamespace(id="queued-sync", func_name="app.jobs.subscription_sync.sync_subscriptions"),
            SimpleNamespace(id="queued-other", func_name="app.jobs.other"),
        ]
        removed = []
        enqueued = []

        def __init__(self, name="default", connection=None):
            self.name = name

        def fetch_job(self, job_id):
            return self.scheduled_jobs.get(job_id)

        def get_jobs(self):
            return list(self.queued_jobs)

        def remove(self, job_id):
            self.removed.append(("queue", job_id))

        def enqueue_in(self, delay, func):
            self.enqueued.append((delay, func))
            return SimpleNamespace(id="new-sync")

    class FakeScheduledRegistry:
        def __init__(self, queue):
            self.queue = queue

        def get_job_ids(self):
            return list(FakeScheduledQueue.scheduled_jobs)

        def remove(self, job_id, delete_job=False):
            FakeScheduledQueue.removed.append(("scheduled", job_id, delete_job))

    FakeScheduledQueue.removed = []
    FakeScheduledQueue.enqueued = []
    monkeypatch.setattr("redis.from_url", _FakeRedis.from_url)
    monkeypatch.setattr("rq.Queue", FakeScheduledQueue)
    monkeypatch.setattr("rq.registry.ScheduledJobRegistry", FakeScheduledRegistry)

    result = admin._reschedule_subscription_sync_scan({"scheduler_scan_interval_minutes": 7})

    assert result["job_id"] == "new-sync"
    assert result["interval_minutes"] == 7
    assert ("scheduled", "old-sync", True) in FakeScheduledQueue.removed
    assert ("queue", "queued-sync") in FakeScheduledQueue.removed
    assert len(FakeScheduledQueue.enqueued) == 1


def test_queue_stats_reports_all_runtime_queues(monkeypatch):
    from app.api import system

    class FakeQueue:
        counts = {
            "default": {"queued": 1, "scheduled": 0, "started": 0, "failed": 2},
            "downloads": {"queued": 3, "scheduled": 4, "started": 1, "failed": 5},
            "imports": {"queued": 6, "scheduled": 7, "started": 2, "failed": 8},
            "scheduled": {"queued": 0, "scheduled": 1, "started": 0, "failed": 1},
        }

        def __init__(self, name="default", connection=None):
            self.name = name

        def __len__(self):
            return self.counts[self.name]["queued"]

        def fetch_job(self, job_id):
            return SimpleNamespace(id=job_id, func_name="app.jobs.subscription_sync.sync_subscriptions")

    class FakeRegistry:
        field = ""

        def __init__(self, queue):
            self.queue = queue

        @property
        def count(self):
            return FakeQueue.counts[self.queue.name][self.field]

        def get_job_ids(self):
            return ["sync-job"] if self.queue.name == "scheduled" and self.field == "scheduled" else []

        def get_scheduled_time(self, _job):
            from datetime import datetime, timezone
            return datetime(2026, 6, 13, 1, 0, tzinfo=timezone.utc)

    class ScheduledRegistry(FakeRegistry):
        field = "scheduled"

    class StartedRegistry(FakeRegistry):
        field = "started"

    class FailedRegistry(FakeRegistry):
        field = "failed"

    monkeypatch.setattr("redis.from_url", _FakeRedis.from_url)
    monkeypatch.setattr("rq.Queue", FakeQueue)
    monkeypatch.setattr("rq.registry.ScheduledJobRegistry", ScheduledRegistry)
    monkeypatch.setattr("rq.registry.StartedJobRegistry", StartedRegistry)
    monkeypatch.setattr("rq.registry.FailedJobRegistry", FailedRegistry)

    payload = asyncio.run(system._queue_stats_payload())

    assert set(payload["queues"]) == {"default", "downloads", "imports", "scheduled"}
    assert payload["queues"]["downloads"]["scheduled"] == 4
    assert payload["queues"]["imports"]["failed"] == 8
    assert payload["failed_jobs"] == 16
    assert payload["scheduled_queue"] == 1
    assert payload["next_sync_scan_at"] == "2026-06-13T01:00:00+00:00"


def test_delayed_retries_use_named_runtime_queues():
    root = Path(__file__).resolve().parents[1]
    download_src = (root / "app" / "jobs" / "download.py").read_text()
    import_src = (root / "app" / "jobs" / "import_runner.py").read_text()

    assert 'Queue(name="downloads", connection=r).enqueue_in(' in download_src
    assert 'Queue(name="imports", connection=r).enqueue_in(' in import_src
