from uuid import uuid4
import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("entry", ["http", "cli"])
async def test_visibility_rechecks_stale_loaded_state_and_rolls_back_audit_failure(entry, monkeypatch):
    from app.database import async_session, engine
    from app.models import Work
    from app.models.curation import WorkCurationState, CurationCommit
    from app.services.curation import CurationService
    from app.schemas.gitllery import GitlleryCommandRequest
    from tests.test_gitllery_api import _clear
    from sqlalchemy import func
    try:
        async with async_session() as db:
            await _clear(db)
            work = Work(title="visibility fence")
            db.add(work)
            await db.flush()
            identity = work.id
            db.add(WorkCurationState(work_id=identity, visibility="visible"))
            await db.commit()
        async with async_session() as old:
            cached = (await old.execute(select(WorkCurationState).where(WorkCurationState.work_id == identity))).scalar_one()
            async with async_session() as writer:
                await CurationService(writer).trash_works([identity])
            assert cached.visibility == "visible"
            service = CurationService(old)
            if entry == "http":
                await service.restore_works([identity])
            else:
                await service.execute_gitllery_command(GitlleryCommandRequest(message="restore", idempotency_key=uuid4(), operations=[{"work_id": identity, "action": "restore"}]), actor_id=None)
            assert cached.visibility == "visible"
        async with async_session() as db:
            count_before = await db.scalar(select(func.count()).select_from(CurationCommit))
        async def fail_audit(*args, **kwargs):
            raise RuntimeError("audit unavailable")
        monkeypatch.setattr(CurationService, "_add_change", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            async with async_session() as db:
                if entry == "http":
                    await CurationService(db).trash_works([identity])
                else:
                    await CurationService(db).execute_gitllery_command(GitlleryCommandRequest(message="trash", idempotency_key=uuid4(), operations=[{"work_id": identity, "action": "trash"}]), actor_id=None)
        async with async_session() as db:
            assert (await db.execute(select(WorkCurationState).where(WorkCurationState.work_id == identity))).scalar_one().visibility == "visible"
            assert await db.scalar(select(func.count()).select_from(CurationCommit)) == count_before
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.parametrize("action,initial,expected", [("trash", "visible", "trashed"), ("restore", "trashed", "visible"), ("trash", "purged", "purged"), ("restore", "visible", "visible"), ("restore", "purged", "purged")])
async def test_http_and_cli_share_visibility_rules_and_keep_noop_audit(action, initial, expected):
    from app.database import async_session, engine
    from app.models import Work
    from app.models.curation import WorkCurationState, CurationChange
    from app.services.curation import CurationService
    from app.schemas.gitllery import GitlleryCommandRequest
    from tests.test_gitllery_api import _clear
    try:
        async with async_session() as db:
            await _clear(db)
            works = [Work(title="HTTP"), Work(title="CLI")]
            db.add_all(works)
            await db.flush()
            ids = [row.id for row in works]
            db.add_all([WorkCurationState(work_id=identity, visibility=initial) for identity in ids])
            await db.commit()
        async with async_session() as db:
            service = CurationService(db)
            # HTTP ignores duplicate/missing ids when an existing work matches.
            commit = await getattr(service, "trash_works" if action == "trash" else "restore_works")([ids[0], ids[0], uuid4()], reason="fixture")
            http_commit = commit.id
            command = GitlleryCommandRequest(message="fixture", reason="fixture", idempotency_key=uuid4(), operations=[{"work_id": ids[1], "action": action}])
            result = await service.execute_gitllery_command(command, actor_id=None)
            for identity in ids:
                state = (await db.execute(select(WorkCurationState).where(WorkCurationState.work_id == identity))).scalar_one()
                assert state.visibility == expected
            changes = list((await db.execute(select(CurationChange).where(CurationChange.commit_id.in_([http_commit, result["commit_id"]])))).scalars())
            if initial == expected:
                assert [(change.commit_id, change.action) for change in changes] == [(result["commit_id"], "command_noop")]
            else:
                assert {change.action for change in changes} == {"work_trashed" if action == "trash" else "work_restored"}
                assert len(changes) == 2
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
