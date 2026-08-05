import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.services.sync_outcome import build_sync_outcome


class FakeDB:
    def __init__(self, events):
        self.events = events

    async def commit(self):
        self.events.append("commit")


def test_successful_finalization_commits_before_publish(monkeypatch):
    from app.services import download_finalization as module

    events = []
    job = SimpleNamespace(
        id=uuid4(),
        status="downloaded",
        subscription_source_id=uuid4(),
        manifest=None,
        progress_data=None,
        pipeline_stage=None,
        error_log="old failure",
    )
    outcome = build_sync_outcome("no_changes", metadata_count=0, media_count=0)

    class Repo:
        def __init__(self, db):
            assert isinstance(db, FakeDB)

        async def update_status(self, target, status, error):
            events.append(("status", status, error))
            target.status = status
            target.error_log = error

    async def mark_synced(db, source_id):
        events.append(("synced", source_id))

    def publish(task_id, task_type, progress):
        events.append(("publish", progress))

    monkeypatch.setattr(module, "DownloadJobRepository", Repo)
    monkeypatch.setattr(module, "mark_source_sync_success", mark_synced)
    monkeypatch.setattr(module, "publish_progress", publish)

    progress = asyncio.run(
        module.finalize_download_job(
            FakeDB(events),
            job,
            status="complete",
            outcome=outcome,
            error=None,
            assets=0,
        )
    )

    assert job.status == "complete"
    assert job.error_log is None
    assert job.manifest["outcome"] == outcome
    assert progress == {"stage": "complete", "percent": 100, "assets": 0, "outcome_code": "no_changes"}
    assert events.index("commit") < next(index for index, event in enumerate(events) if isinstance(event, tuple) and event[0] == "publish")


def test_failure_finalization_clears_stale_success_outcome(monkeypatch):
    from app.services import download_finalization as module

    job = SimpleNamespace(
        id=uuid4(),
        status="downloaded",
        subscription_source_id=None,
        manifest={"outcome": build_sync_outcome("new_content", metadata_count=1, media_count=1)},
        progress_data=None,
        pipeline_stage=None,
        error_log=None,
    )

    class Repo:
        def __init__(self, _db):
            pass

        async def update_status(self, target, status, error):
            target.status = status
            target.error_log = error

    monkeypatch.setattr(module, "DownloadJobRepository", Repo)
    monkeypatch.setattr(module, "publish_progress", lambda *_args: None)

    asyncio.run(
        module.finalize_download_job(
            FakeDB([]),
            job,
            status="failed",
            error="auth failed",
            message="auth failed",
        )
    )
    assert job.status == "failed"
    assert job.error_log == "auth failed"
    assert "outcome" not in job.manifest
