import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration


def _run(coro):
    return asyncio.run(coro)


def test_restore_committed_before_purge_is_not_deleted():
    async def scenario():
        from app.config import settings
        from app.database import async_session
        from app.models import Asset, Work
        from app.models.curation import WorkCurationState
        from app.services.curation import CurationService

        path = Path(settings.download_root) / f"purge-race-first-{uuid4()}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"restore first")
        async with async_session() as db:
            asset = Asset(file_path=path.name, file_name=path.name, file_size=path.stat().st_size)
            work = Work(title="restore first")
            db.add_all([asset, work])
            await db.flush()
            work.thumbnail_asset_id = asset.id
            db.add(WorkCurationState(work_id=work.id, visibility="trashed"))
            await db.commit()
            work_id = work.id

        async with async_session() as db:
            restored = await CurationService(db).restore_works([work_id])
            assert restored.stats["work_count"] == 1
        async with async_session() as db:
            try:
                await CurationService(db).purge([work_id])
            except Exception as exc:  # a restored work is no longer eligible
                assert getattr(exc, "status_code", None) == 400
            else:
                raise AssertionError("purge unexpectedly accepted restored work")
        assert path.exists()

    _run(scenario())


def test_purge_lock_blocks_restore_until_delete_commits(monkeypatch):
    async def scenario():
        from app.config import settings
        from app.database import async_session
        from app.models import Asset, Work
        from app.models.curation import WorkCurationState
        from app.services import curation
        from app.services.curation import CurationService

        async def no_projection(*args, **kwargs):
            return None

        monkeypatch.setattr(curation, "request_search_projection", no_projection)
        path = Path(settings.download_root) / f"purge-race-lock-{uuid4()}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"purge first")
        async with async_session() as db:
            asset = Asset(file_path=path.name, file_name=path.name, file_size=path.stat().st_size)
            work = Work(title="purge first")
            db.add_all([asset, work])
            await db.flush()
            work.thumbnail_asset_id = asset.id
            db.add(WorkCurationState(work_id=work.id, visibility="trashed"))
            await db.commit()
            work_id = work.id

        entered = asyncio.Event()
        proceed = asyncio.Event()
        original = CurationService._create_commit

        async def gate(self, **kwargs):
            if asyncio.current_task().get_name() == "purge":
                entered.set()
                await proceed.wait()
            return await original(self, **kwargs)

        monkeypatch.setattr(CurationService, "_create_commit", gate)

        async def run_purge():
            async with async_session() as db:
                return await CurationService(db).purge([work_id])

        async def run_restore():
            async with async_session() as db:
                return await CurationService(db).restore_works([work_id])

        purge_task = asyncio.create_task(run_purge(), name="purge")
        await asyncio.wait_for(entered.wait(), 5)
        restore_task = asyncio.create_task(run_restore(), name="restore")
        try:
            async with asyncio.timeout(0.2):
                await asyncio.shield(restore_task)
        except TimeoutError:
            pass
        else:
            raise AssertionError("restore bypassed purge row lock")
        proceed.set()
        purged = await asyncio.wait_for(purge_task, 5)
        restored = await asyncio.wait_for(restore_task, 5)
        assert purged.stats["work_count"] == 1
        assert restored.stats["work_count"] == 0
        assert not path.exists()
        async with async_session() as db:
            state = (
                await db.execute(
                    select(WorkCurationState).where(WorkCurationState.work_id == work_id)
                )
            ).scalar_one()
            assert state.visibility == "purged"

    _run(scenario())
