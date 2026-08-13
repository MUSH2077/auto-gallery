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


class _FakeRedisClient:
    def __init__(self):
        self.values = {}

    def setex(self, key, _ttl, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)


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
    """TaskEngine retry/resume persist and publish downloads dispatches."""
    from app.services import task_engine
    from app.services.task_engine import TaskEngine

    calls = []

    async def prepare(_db, _job, **kwargs):
        calls.append(("prepare", kwargs["queue_name"], kwargs["action"]))
        return SimpleNamespace(rq_job_id=f"download-{kwargs['action']}")

    async def publish(_db, _job, _prepared, **kwargs):
        calls.append(("publish", "downloads", kwargs["action"]))
        return SimpleNamespace(id="rq-job")

    monkeypatch.setattr(task_engine, "prepare_download_dispatch", prepare)
    monkeypatch.setattr(task_engine, "publish_prepared_download", publish)
    monkeypatch.setattr(
        task_engine.TaskEventPublisher,
        "publish_status_change",
        lambda *_args, **_kwargs: None,
    )

    job = SimpleNamespace(
        id=uuid4(),
        status="failed",
        error_log=None,
        subscription_id=None,
        source="pixiv",
        manifest=None,
        retry_count=0,
        user_note=None,
        operator_name=None,
        operator_action=None,
    )

    class Repo:
        async def get(self, _jid):
            return job

    class DB:
        async def flush(self):
            pass

        async def commit(self):
            pass

    engine = TaskEngine(DB())
    engine.repo = Repo()
    asyncio.run(engine.retry_download(job.id))
    job.status = "paused"
    asyncio.run(engine.resume_download(job.id))

    assert calls == [
        ("prepare", "downloads", "retry"),
        ("publish", "downloads", "retry"),
        ("prepare", "downloads", "resume"),
        ("publish", "downloads", "resume"),
    ]

def test_import_retry_and_resume_use_import_dispatch(monkeypatch):
    """TaskEngine retry/resume persist fixed-id imports dispatches."""
    from app.services import task_engine
    from app.services.task_engine import TaskEngine

    calls = []

    async def prepare(_db, _job, **kwargs):
        calls.append(("prepare", kwargs["action"]))
        return SimpleNamespace(rq_job_id=f"import-{kwargs['action']}-attempt-2")

    async def publish(_db, job_id, rq_job_id, **_kwargs):
        calls.append(("publish", job_id, rq_job_id))
        return "replayed"

    monkeypatch.setattr(task_engine, "prepare_import_dispatch", prepare)
    monkeypatch.setattr(task_engine, "publish_prepared_import", publish)
    monkeypatch.setattr(
        task_engine.TaskEventPublisher,
        "publish_status_change",
        lambda *_args, **_kwargs: None,
    )

    job = SimpleNamespace(
        id=uuid4(),
        status="failed",
        error_log=None,
        import_retry_count=0,
        max_import_retries=3,
        operator_name=None,
        operator_action=None,
    )

    class Repo:
        async def get_import(self, _jid):
            return job

    class DB:
        async def flush(self):
            pass

        async def commit(self):
            pass

    engine = TaskEngine(DB())
    engine.repo = Repo()
    asyncio.run(engine.retry_import(job.id))
    job.status = "paused"
    asyncio.run(engine.resume_import(job.id))

    assert calls == [
        ("prepare", "retry"),
        ("publish", job.id, "import-retry-attempt-2"),
        ("prepare", "resume"),
        ("publish", job.id, "import-resume-attempt-2"),
    ]
