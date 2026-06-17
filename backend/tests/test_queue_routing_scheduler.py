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
    """TaskEngine retry/resume enqueues to the downloads queue."""
    from app.services.task_engine import TaskEngine

    _patch_queue(monkeypatch)
    monkeypatch.setattr("app.services.task_engine.get_redis", lambda: object())

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

    enqueue_calls = [call for call in _FakeQueue.calls if call[0] == "enqueue"]
    assert enqueue_calls
    queues = {call[1] for call in enqueue_calls}
    assert queues == {"downloads"}, f"Expected only downloads queue, got {queues}"

def test_import_retry_uses_imports_queue(monkeypatch):
    """TaskEngine retry_import enqueues to the imports queue."""
    from app.services.task_engine import TaskEngine

    _patch_queue(monkeypatch)
    monkeypatch.setattr("app.services.task_engine.get_redis", lambda: object())

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

    enqueue_calls = [call for call in _FakeQueue.calls if call[0] == "enqueue"]
    assert enqueue_calls
    assert enqueue_calls[-1][1] == "imports"