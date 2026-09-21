"""Real durable boundaries for background time, history cost, and ownership."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select


@pytest.fixture
def ample_slice(monkeypatch):
    from app.services import heavy_io

    @asynccontextmanager
    async def capacity(*_args, **kwargs):
        yield SimpleNamespace(work_units=25, slice_seconds=1.0)
        kwargs["cooldown_result"]["seconds"] = 2.0

    monkeypatch.setattr(heavy_io, "adaptive_resource_slice", capacity)


@pytest.fixture
async def clean_database():
    from app.database import async_session, engine
    from tests.test_gitllery_service import _clear
    async with async_session() as db:
        await _clear(db)
    try:
        yield async_session
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.asyncio
async def test_slow_creator_commit_yields_before_next_creator(monkeypatch, ample_slice, clean_database):
    from app.models import Creator, CurationCommit
    from app.services import curation

    clock = [0.0]
    monkeypatch.setattr(curation, "monotonic", lambda: clock[0], raising=False)
    original = curation.CurationService._add_change

    async def slow_change(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        clock[0] += 2.0
        return result

    monkeypatch.setattr(curation.CurationService, "_add_change", slow_change)
    async with clean_database() as db:
        db.add_all([Creator(name="first"), Creator(name="second")])
        await db.commit()
        result = await curation.CurationService(db).run_backfill()
        assert result["status"] == "pending"
        assert result["created"]["creators"] == 1
        assert len((await db.execute(select(CurationCommit))).scalars().all()) == 1


async def seed_history(db, count):
    from app.services.curation import CurationService
    from tests.test_gitllery_service import _seed_work
    _, work = await _seed_work(db)
    service = CurationService(db)
    ids = []
    for number in range(count):
        commit = await service._create_commit(message=f"version {number}", trigger="test")
        await service._add_change(commit, subject_type="work", subject_id=str(work.id),
                                  action="work_added", before_state=None,
                                  after_state={"id": str(work.id), "title": str(number)})
        ids.append(commit.id)
        await db.commit()
    return ids


@pytest.mark.asyncio
async def test_slow_git_commit_yields_after_its_durable_cursor(tmp_path, monkeypatch, ample_slice, clean_database):
    from app.config import settings
    from app.models.pipeline_outbox import GitlleryProjectionOutbox
    from app.services.gitllery import service

    monkeypatch.setattr(settings, "library_root", str(tmp_path))
    clock = [0.0]
    monkeypatch.setattr(service, "monotonic", lambda: clock[0], raising=False)
    original = service.GitlleryService._apply_commit_to_repo

    def slow_apply(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        clock[0] += 2.0
        return result

    monkeypatch.setattr(service.GitlleryService, "_apply_commit_to_repo", slow_apply)
    async with clean_database() as db:
        ids = await seed_history(db, 2)
        svc = service.GitlleryService(db)
        assert sum((await svc.project_pending()).values()) == 1
        assert svc.continuation["after"][1] == str(ids[0])
        states = dict((await db.execute(select(GitlleryProjectionOutbox.commit_id, GitlleryProjectionOutbox.state))).all())
        assert states[ids[0]] == "complete" and states[ids[1]] != "complete"
        assert sum((await service.GitlleryService(db).project_pending(continuation=svc.continuation)).values()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [4, 16])
async def test_completed_history_reconcile_does_not_walk_parent_chains(count, tmp_path, monkeypatch, ample_slice, clean_database):
    from app.config import settings
    from app.services.gitllery.service import GitlleryService
    from app.services.gitllery.repo import GitlleryRepo
    from app.services.gitllery.objects import ObjectStore

    monkeypatch.setattr(settings, "library_root", str(tmp_path))
    async with clean_database() as db:
        await seed_history(db, count)
        state = None
        while True:
            svc = GitlleryService(db)
            await svc.project_pending(continuation=state)
            state = svc.continuation
            if state is None:
                break
        reads = [0]
        original = ObjectStore.read

        def counted_read(self, *args, **kwargs):
            reads[0] += 1
            return original(self, *args, **kwargs)

        def no_history_walk(*_args, **_kwargs):
            pytest.fail("completed history traversed the parent chain")

        monkeypatch.setattr(ObjectStore, "read", counted_read)
        monkeypatch.setattr(GitlleryRepo, "has_projected_db_commit_id", no_history_walk)
        state = None
        while True:
            svc = GitlleryService(db)
            assert sum((await svc.project_pending(continuation=state)).values()) == 0
            state = svc.continuation
            if state is None:
                break
        assert reads[0] <= 8 * count + 8


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["curation", "git"])
async def test_superseded_admin_attempt_cannot_publish_domain_work(kind, tmp_path, monkeypatch, ample_slice, clean_database):
    from app.config import settings
    from app.models import Creator, CurationCommit, TaskRun
    from app.models.pipeline_outbox import GitlleryProjectionOutbox
    from app.services import operations
    from app.services.curation import CurationService
    from app.services.gitllery.service import GitlleryService
    from tests.test_admin_operation_dispatch import _clear_dispatch_rows

    monkeypatch.setattr(settings, "library_root", str(tmp_path))
    task_id = None
    try:
        async with clean_database() as db:
            await _clear_dispatch_rows(db)
            if kind == "git":
                await seed_history(db, 1)
            else:
                db.add(Creator(name="must stay unprojected"))
                await db.commit()
            prepared = await operations.prepare_admin_operation(db,
                operation_type="admin-curation-backfill" if kind == "curation" else "admin-gitllery-sync",
                scope_key="library:curation-backfill:active" if kind == "curation" else "library:gitllery-sync:active",
                title="Background fence", entity="test", options={},
                queue_name="maintenance", job_timeout=60)
            task_id = prepared.task.id
            dispatch = dict(prepared.task.meta[operations.ADMIN_DISPATCH_META_KEY])
            dispatch["attempt"] = 2
            prepared.task.meta = {**prepared.task.meta, operations.ADMIN_DISPATCH_META_KEY: dispatch}
            prepared.task.attempts = 2
            await db.commit()
        with operations.admin_operation_attempt_context(task_id, 1):
            async with clean_database() as db:
                with pytest.raises(operations.AdminOperationAttemptRejected):
                    if kind == "curation":
                        await CurationService(db).run_backfill()
                    else:
                        await GitlleryService(db).project_pending()
        async with clean_database() as db:
            if kind == "curation":
                assert not (await db.execute(select(CurationCommit))).first()
            else:
                assert not list(tmp_path.rglob("HEAD"))
                assert set((await db.execute(select(GitlleryProjectionOutbox.state))).scalars()) == {"pending"}
    finally:
        async with clean_database() as db:
            if task_id is not None:
                task = await db.get(TaskRun, task_id)
                if task is not None:
                    await db.delete(task)
                    await db.commit()


@pytest.mark.asyncio
async def test_repository_initialization_hydrates_only_a_page_and_yields_after_slow_init(tmp_path, monkeypatch, ample_slice, clean_database):
    from app.config import settings
    from app.models import Work, WorkSource
    from app.services.gitllery import service
    from app.services.gitllery.slicing import RepoResolver

    monkeypatch.setattr(settings, "library_root", str(tmp_path))
    clock, resolved = [0.0], []
    monkeypatch.setattr(service, "monotonic", lambda: clock[0])
    original_init = service.GitlleryService._ensure_init
    original_resolve = RepoResolver._descriptor_for_work_source

    def slow_init(self, *args):
        original_init(self, *args)
        clock[0] += 2

    async def resolve(self, ws):
        resolved.append(ws.source_creator_id)
        return await original_resolve(self, ws)

    monkeypatch.setattr(service.GitlleryService, "_ensure_init", slow_init)
    monkeypatch.setattr(RepoResolver, "_descriptor_for_work_source", resolve)
    async with clean_database() as db:
        for number in range(26):
            work = Work(title=str(number))
            db.add(work)
            await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id=str(number),
                              source_creator_id=f"{number:03}", raw_metadata={"user": {"name": f"repo{number:03}"}}))
        await db.commit()
        svc = service.GitlleryService(db)
        await svc.backfill()
        assert len(resolved) <= 25
        assert svc.continuation["repository_cursor"] == ["pixiv", "000"]
        first_resolved = set(resolved)
        resolved.clear()
        resumed = service.GitlleryService(db)
        await resumed.backfill(continuation=svc.continuation)
        assert "000" not in resolved
        assert resumed.continuation["repository_cursor"] == ["pixiv", "001"]
        assert len(first_resolved) == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["curation", "git"])
async def test_supersession_after_first_commit_preserves_prefix_and_rejects_next(kind, tmp_path, monkeypatch, ample_slice, clean_database):
    from app.config import settings
    from app.models import Creator, CurationCommit, TaskRun
    from app.models.pipeline_outbox import GitlleryProjectionOutbox
    from app.services import curation, operations
    from app.services.gitllery import service
    from tests.test_admin_operation_dispatch import _clear_dispatch_rows

    monkeypatch.setattr(settings, "library_root", str(tmp_path))
    monkeypatch.setattr(curation, "monotonic", lambda: 0.0)
    monkeypatch.setattr(service, "monotonic", lambda: 0.0)
    task_id = None
    cls = curation.CurationService if kind == "curation" else service.GitlleryService
    method = "_commit_backfill_unit" if kind == "curation" else "_complete_bulk_outbox"
    original = getattr(cls, method)

    async def supersede_after_commit(self, *args):
        result = await original(self, *args)
        async with clean_database() as superseding:
            task = await superseding.get(TaskRun, task_id)
            dispatch = dict(task.meta[operations.ADMIN_DISPATCH_META_KEY])
            dispatch["attempt"] = 2
            task.meta = {**task.meta, operations.ADMIN_DISPATCH_META_KEY: dispatch}
            task.attempts = 2
            await superseding.commit()
        return result

    monkeypatch.setattr(cls, method, supersede_after_commit)
    try:
        async with clean_database() as db:
            await _clear_dispatch_rows(db)
            if kind == "git":
                await seed_history(db, 2)
            else:
                db.add_all([Creator(name="first"), Creator(name="second")])
                await db.commit()
            operation = "curation-backfill" if kind == "curation" else "gitllery-sync"
            prepared = await operations.prepare_admin_operation(db,
                operation_type=f"admin-{operation}", scope_key=f"library:{operation}:active",
                title="Background fence", entity="test", options={}, queue_name="maintenance", job_timeout=60)
            task_id = prepared.task.id
            await db.commit()
        with operations.admin_operation_attempt_context(task_id, 1):
            async with clean_database() as db:
                with pytest.raises(operations.AdminOperationAttemptRejected):
                    if kind == "curation":
                        await cls(db).run_backfill()
                    else:
                        await cls(db).project_pending()
        async with clean_database() as db:
            if kind == "curation":
                assert len((await db.execute(select(CurationCommit))).scalars().all()) == 1
            else:
                states = list((await db.execute(select(GitlleryProjectionOutbox.state))).scalars())
                assert states.count("complete") == 1
                assert states.count("pending") == 1
    finally:
        async with clean_database() as db:
            if task_id is not None:
                task = await db.get(TaskRun, task_id)
                if task is not None:
                    await db.delete(task)
                    await db.commit()
