"""Bounded background administrator slices and durable delayed delivery."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select


@pytest.fixture
def bounded_capacity(monkeypatch):
    from app.services import heavy_io
    observed = []

    @asynccontextmanager
    async def capacity(workload, owner, **kwargs):
        observed.append((workload, kwargs))
        assert kwargs.get("wait_for_capacity") is False
        assert kwargs.get("cooldown_result") is not None
        yield SimpleNamespace(work_units=1, slice_seconds=20)
        kwargs["cooldown_result"]["seconds"] = 180.0

    monkeypatch.setattr(heavy_io, "adaptive_resource_slice", capacity)
    return observed


@pytest.mark.asyncio
async def test_adaptive_slice_rejects_unowned_cooldown_before_admission(monkeypatch):
    from app.services import heavy_io, resource_pressure

    async def unexpected(*args, **kwargs):
        pytest.fail("admission happened before checking continuation ownership")

    monkeypatch.setattr(resource_pressure, "current_profile_slice_limits", unexpected)
    with pytest.raises(ValueError, match="cooldown_result"):
        async with heavy_io.adaptive_resource_slice("git_projection", "test"):
            pytest.fail("unowned slice executed")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_git_history_resumes_keyset_and_completes_intents(bounded_capacity):
    from app.database import async_session, engine
    from app.models import CurationCommit
    from app.models.pipeline_outbox import GitlleryProjectionOutbox
    from app.services.gitllery.service import GitlleryService
    from tests.test_gitllery_service import _clear

    try:
        async with async_session() as db:
            await _clear(db)
            stamp = datetime.now(timezone.utc)
            ids = []
            for index in range(3):
                commit = CurationCommit(id=uuid4(), message="baseline", actor_type="system",
                    trigger="test", created_at=stamp + timedelta(seconds=index))
                db.add(commit)
                ids.append(commit.id)
                await db.flush()
                db.add(GitlleryProjectionOutbox(commit_id=commit.id))
            await db.commit()
        continuation = {}
        visited = []
        for expected in ids:
            async with async_session() as db:
                svc = GitlleryService(db)
                await svc.project_pending(continuation=continuation)
                continuation = svc.continuation
                assert continuation["after"][1] == str(expected)
                assert svc.successor_delay_seconds == 180
                visited.append(continuation["after"][1])
                states = (await db.execute(select(GitlleryProjectionOutbox.state).where(
                    GitlleryProjectionOutbox.commit_id == expected))).scalars().all()
                assert states == ["complete"]
        assert len(set(visited)) == 3
        async with async_session() as db:
            svc = GitlleryService(db)
            await svc.project_pending(continuation=continuation)
            assert svc.continuation is None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_curation_yields_after_one_creator_and_replay_does_not_add_history(bounded_capacity):
    from app.database import async_session, engine
    from app.models import Creator, CurationCommit
    from app.services.curation import CurationService
    from tests.test_gitllery_service import _clear

    try:
        async with async_session() as db:
            await _clear(db)
            db.add_all([Creator(name="one"), Creator(name="two")])
            await db.commit()
            first = await CurationService(db).run_backfill()
            assert first["status"] == "pending"
            assert first["created"]["creators"] == 1
            assert first["successor_delay_seconds"] == 180
            commits = (await db.execute(select(CurationCommit))).scalars().all()
            assert len(commits) == 1
            replay = await CurationService(db).run_backfill()
            assert replay["created"]["creators"] == 1
            commits = (await db.execute(select(CurationCommit))).scalars().all()
            assert len(commits) == 2
            assert all(call[1]["lane"] == "background" for call in bounded_capacity)
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("operation_type", ["admin-gitllery-sync", "admin-curation-backfill"])
async def test_background_admin_handoff_is_durable_delayed_and_attempt_fenced(monkeypatch, operation_type):
    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.models import TaskRun
    from app.services import operations
    from app.services.curation import CurationService
    from app.services.gitllery.service import GitlleryService
    from app.services.redis_client import get_redis
    from tests.test_admin_operation_dispatch import _clear_dispatch_rows, _clear_rq_task

    async def git_slice(self, *args, **kwargs):
        self.continuation = {"after": ["2026-09-06T00:00:00+00:00", str(uuid4())]}
        self.successor_delay_seconds = 180
        return {"repo": 1}

    async def curation_slice(self, **kwargs):
        return {"status": "pending", "created": {"work_groups": 1},
                "continuation": {"phase": "works"}, "successor_delay_seconds": 180}

    monkeypatch.setattr(GitlleryService, "project_pending", git_slice)
    monkeypatch.setattr(CurationService, "run_backfill", curation_slice)
    redis = get_redis()
    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(db,
                operation_type=operation_type, scope_key=f"library:{operation_type.removeprefix("admin-")}:active",
                title="Background slice", entity="test", options={}, queue_name="maintenance",
                job_timeout=60)
            task_id = prepared.task.id
            await db.commit()
        before = datetime.now(timezone.utc)
        result = await asyncio.to_thread(
            admin_operations.run_registered_admin_operation, str(task_id), 1)
        assert result["_admin_handoff"] is True
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert task.status == "enqueued"
            assert dispatch["attempt"] == 2
            assert datetime.fromisoformat(dispatch["next_retry_at"]) >= before + timedelta(seconds=150)
            assert dispatch["options"]["_background_continuation"]
            assert not redis.exists(f"rq:job:{dispatch['rq_job_id']}")
        recovery = await operations.recover_admin_operation_dispatches(redis_client=redis, grace_seconds=0)
        assert recovery["published"] == 0
        with pytest.raises(RuntimeError, match="attempt is no longer current"):
            await asyncio.to_thread(
                admin_operations.run_registered_admin_operation, str(task_id), 1)
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            assert task.attempts == 2
            assert task.status == "enqueued"
    finally:
        if task_id is not None:
            _clear_rq_task(redis, task_id)
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_denied_background_admission_returns_without_transaction_or_progress(monkeypatch):
    from app.database import async_session, engine
    from app.models import Creator, CurationCommit
    from app.services import resource_pressure
    from app.services.curation import CurationService
    from app.services.gitllery.service import GitlleryService
    from tests.test_gitllery_service import _clear

    async def denied(*args, **kwargs):
        return SimpleNamespace(allowed=False), {}

    monkeypatch.setattr(resource_pressure, "current_profile_slice_limits", denied)
    try:
        async with async_session() as db:
            await _clear(db)
            db.add(Creator(name="waiting"))
            db.add(CurationCommit(message="waiting", actor_type="system", trigger="test"))
            await db.commit()
            result = await CurationService(db).run_backfill()
            assert result["status"] == "pending"
            assert result["continuation"] == {}
            assert not db.in_transaction()
            svc = GitlleryService(db)
            assert await svc.project_pending() == {}
            assert "after" not in svc.continuation
            assert not db.in_transaction()
            assert svc.successor_delay_seconds == 2
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_curation_group_successors_keep_chunk_keys_and_skip_crash_replays(bounded_capacity):
    from app.database import async_session, engine
    from app.models import Work, WorkSource, CurationChange, CurationCommit
    from app.services.curation import CurationService
    from tests.test_gitllery_service import _clear

    try:
        async with async_session() as db:
            await _clear(db)
            for index in range(26):
                work = Work(title=f"work {index}")
                db.add(work)
                await db.flush()
                db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id=str(index),
                                  source_creator_id="artist"))
            await db.commit()
        async with async_session() as db:
            first = await CurationService(db).run_backfill()
            assert first["created"]["work_groups"] == 1
            first_cursor = first["continuation"]["work_cursor"]
            assert first_cursor["chunk_index"] == 0
            assert len((await db.execute(select(CurationChange))).scalars().all()) == 25
        # Crash before handoff: the first group is already durable, so a fresh
        # invocation skips it; its successor writes only the second chunk.
        async with async_session() as db:
            replay = await CurationService(db).run_backfill()
            assert replay["created"]["work_groups"] == 0
            assert replay["skipped"]["work_groups"] == 1
            assert replay["continuation"]["work_cursor"]["chunk_index"] == 0
            replay = await CurationService(db).run_backfill(continuation=replay["continuation"])
            assert replay["created"]["work_groups"] == 1
            assert replay["continuation"]["work_cursor"]["chunk_index"] == 1
            commits = (await db.execute(select(CurationCommit).order_by(CurationCommit.created_at))).scalars().all()
            assert len(commits) == 2
            assert commits[1].dedupe_key == commits[0].dedupe_key + ":chunk:1"
            original_changes = (await db.execute(select(CurationChange.id))).scalars().all()
            assert len(original_changes) == 26
        async with async_session() as db:
            resumed = await CurationService(db).run_backfill(continuation=first["continuation"])
            assert resumed["status"] == "pending"
            assert resumed["created"]["work_groups"] == 0
            resumed = await CurationService(db).run_backfill(continuation=resumed["continuation"])
            assert resumed["status"] == "ok"
            assert set((await db.execute(select(CurationChange.id))).scalars()) == set(original_changes)
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_git_disk_prefix_replay_is_idempotent_and_historical_gap_is_rejected(tmp_path, monkeypatch, bounded_capacity):
    from app.config import settings
    from app.database import async_session, engine
    from app.models import CurationCommit
    from app.services.curation import CurationService
    from app.services.gitllery.service import GitlleryService
    from app.services.gitllery.slicing import RepoResolver
    from tests.test_gitllery_service import _clear, _seed_work

    monkeypatch.setattr(settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            _, work = await _seed_work(db)
            curation = CurationService(db)
            ids = []
            for index in range(2):
                commit = await curation._create_commit(message=f"version {index}", trigger="test")
                await curation._add_change(commit, subject_type="work", subject_id=str(work.id),
                    action="work_added", before_state=None, after_state={"id": str(work.id), "title": str(index)})
                ids.append(commit.id)
                await db.commit()
            svc = GitlleryService(db)
            first = await svc.project_pending()
            assert sum(first.values()) == 1
            state = svc.continuation
            replay = GitlleryService(db)
            assert sum((await replay.project_pending()).values()) == 0
            second = GitlleryService(db)
            assert sum((await second.project_pending(continuation=state)).values()) == 1
            # Once a later commit is projected, replaying an already-present
            # prefix remains harmless even after the ordering lock is released.
            replay = GitlleryService(db)
            assert sum((await replay.project_pending()).values()) == 0
            # An older commit absent from disk must never be appended on HEAD.
            missing = CurationCommit(id=uuid4(), message="missing", actor_type="system", trigger="test",
                created_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
            db.add(missing)
            await db.flush()
            await curation._add_change(missing, subject_type="work", subject_id=str(work.id),
                action="work_added", before_state=None, after_state={"id": str(work.id), "title": "old"})
            await db.commit()
            desc = (await RepoResolver(db).all_repositories())[0]
            repo = svc._repo_for(desc)
            head = repo.head_commit()
            with pytest.raises(RuntimeError, match="pre-watermark projection gap"):
                await GitlleryService(db).project_pending()
            assert repo.head_commit() == head
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_git_repository_initialization_resumes_without_rewriting(tmp_path, monkeypatch, bounded_capacity):
    from app.config import settings
    from app.database import async_session, engine
    from app.services.gitllery.service import GitlleryService
    from app.services.gitllery.slicing import RepoResolver
    from tests.test_gitllery_service import _clear, _seed_work

    monkeypatch.setattr(settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_work(db)
            svc = GitlleryService(db)
            assert await svc.backfill() == {}
            state = svc.continuation
            assert state["repository_after"]
            assert svc.successor_delay_seconds == 180
            desc = (await RepoResolver(db).all_repositories())[0]
            repo = svc._repo_for(desc)
            assert repo.exists()
            replay = GitlleryService(db)
            await replay.backfill(continuation=state)
            assert replay.continuation["phase"] == "project"
            done = GitlleryService(db)
            await done.backfill(continuation=replay.continuation)
            assert done.continuation is None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
