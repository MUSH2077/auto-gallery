import uuid
from pathlib import Path

import pytest
from sqlalchemy import text


async def _clear(db):
    await db.execute(text(
        "TRUNCATE curation_changes, curation_commits, asset_sources, assets, "
        "work_sources, works, source_creators, subscription_sources, subscriptions, "
        "creators RESTART IDENTITY CASCADE"))
    await db.commit()


async def _seed_work(db):
    from app.models import Creator, SourceCreator, Work, WorkSource
    creator = Creator(name="七诗")
    db.add(creator)
    await db.flush()
    db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                         source_creator_id="123", display_name="七诗"))
    work = Work(title="w")
    db.add(work)
    await db.flush()
    db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                      source_creator_id="123", raw_metadata={"user": {"id": "七诗"}}))
    await db.commit()
    return creator, work


@pytest.mark.integration
@pytest.mark.asyncio
async def test_project_commit_writes_objects_refs_index(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService
    from app.services.gitllery.repo import GitlleryRepo

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    # Isolate creator_dir resolution from any real gallery-dl config mounted in
    # this environment (matches the convention in test_settings_effective_config.py);
    # otherwise an on-disk config.json with a stale directory template can change
    # which folder name resolve_creator_directory() produces.
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path / "gallerydl-config"))
    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()

            affected = await GitlleryService(db).project_commit(commit.id)
            assert len(affected) == 1

            repo = GitlleryRepo(tmp_path, "pixiv", "七诗")
            assert repo.exists()
            head = repo.head_commit()
            assert head is not None
            commit_obj = repo.objects.read(head)
            assert commit_obj["type"] == "commit"
            assert commit_obj["db_commit_id"] == str(commit.id)
            assert commit_obj["parent"] is None
            index = repo.read_index()
            assert index["entities"][f"work/{work.id}"]["visibility"] == "trashed"

            # Idempotent: re-project does not advance the ref.
            await GitlleryService(db).project_commit(commit.id)
            assert repo.head_commit() == head
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_project_pending_catches_up_missed_commit(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService
    from app.services.gitllery.repo import GitlleryRepo

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    # Isolate creator_dir resolution from any real gallery-dl config mounted in
    # this environment (matches test_project_commit_writes_objects_refs_index);
    # otherwise an on-disk config.json with a stale directory template can change
    # which folder name resolve_creator_directory() produces.
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path / "gallerydl-config"))
    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            c1 = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()
            c2 = await CurationService(db).restore_works([work.id], message="restore 1")
            await db.commit()
            # Simulate a missed projection: only project the SECOND commit.
            await GitlleryService(db).project_commit(c2.id)

            repo = GitlleryRepo(tmp_path, "pixiv", "七诗")
            assert str(c1.id) not in repo.projected_db_commit_ids()

            result = await GitlleryService(db).project_pending()
            assert sum(result.values()) >= 1
            ids = repo.projected_db_commit_ids()
            assert {str(c1.id), str(c2.id)} <= ids
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
