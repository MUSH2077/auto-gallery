from __future__ import annotations

import pytest
from sqlalchemy import text


async def _clear(db):
    await db.execute(
        text(
            "TRUNCATE gitllery_projection_targets, gitllery_projection_outbox, "
            "gitllery_repository_state, curation_changes, curation_commits, "
            "asset_sources, assets, work_sources, works, source_creators, "
            "subscription_sources, subscriptions, creators RESTART IDENTITY CASCADE"
        )
    )
    await db.commit()


async def _seed_work(db):
    from app.models import Creator, SourceCreator, Work, WorkSource

    creator = Creator(name="七诗")
    db.add(creator)
    await db.flush()
    db.add(
        SourceCreator(
            creator_id=creator.id,
            source="pixiv",
            source_creator_id="123",
            display_name="七诗",
        )
    )
    work = Work(title="w")
    db.add(work)
    await db.flush()
    db.add(
        WorkSource(
            work_id=work.id,
            source="pixiv",
            source_work_id="9001",
            source_creator_id="123",
            raw_metadata={"user": {"id": "七诗"}},
        )
    )
    await db.commit()
    return creator, work


def _configure_shadow_projection(monkeypatch, settings, tmp_path) -> None:
    monkeypatch.setattr(settings, "library_root", str(tmp_path))
    monkeypatch.setattr(settings, "gitllery_projection_mode", "shadow")
    monkeypatch.setattr(settings, "gitllery_build_generation", "test-r1")
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path / "gallerydl-config"))


def _segment_repo(tmp_path):
    from gitllery_format import SegmentRepository

    return SegmentRepository(
        tmp_path / "pixiv" / "七诗" / ".gitllery.build-test-r1"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_project_commit_writes_segment_repository_idempotently(
    tmp_path, monkeypatch
):
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService

    _configure_shadow_projection(monkeypatch, gsvc.settings, tmp_path)
    try:
        async with async_session() as db:
            await _clear(db)
            _creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works(
                [work.id], message="trash 1"
            )
            await db.commit()

            affected = await GitlleryService(db).project_commit(commit.id)
            assert len(affected) == 1

            repo = _segment_repo(tmp_path)
            assert repo.exists()
            manifest = repo.read_manifest()
            assert manifest["format_id"] == "gitllery-segment"
            assert manifest["format_revision"] == 1
            assert manifest["last_complete_commit_id"] == str(commit.id)
            assert manifest["commit_count"] == 1

            commits = list(repo.iter_commits())
            assert len(commits) == 1
            assert commits[0]["commit_id"] == str(commit.id)
            assert commits[0]["parent_commit_id"] is None
            assert commits[0]["changes"][0]["after_state"]["visibility"] == "trashed"

            # Re-projecting the durable watermark is a no-op.
            assert await GitlleryService(db).project_commit(commit.id) == []
            assert repo.read_manifest() == manifest
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_segment_status_log_and_deep_verification(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService

    _configure_shadow_projection(monkeypatch, gsvc.settings, tmp_path)
    try:
        async with async_session() as db:
            await _clear(db)
            _creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works(
                [work.id], message="trash 1"
            )
            await db.commit()

            service = GitlleryService(db)
            affected = await service.project_commit(commit.id)
            repository_id = affected[0]

            status = await service.status(deep=True)
            assert status["product_version"] == "v1"
            assert status["format_id"] == "gitllery-segment"
            assert status["format_revision"] == 1
            assert status["projection_mode"] == "shadow"
            assert status["behind_total"] == 0
            assert status["missing_repos"] == 0
            assert status["repositories"][0]["clean"] is True
            assert status["repositories"][0]["object_integrity_ok"] is True

            log = await service.log(repository_id, limit=10)
            assert log["total"] == 1
            assert log["entries"][0]["commit"] == str(commit.id)
            assert log["entries"][0]["message"] == "trash 1"
            assert log["entries"][0]["change_count"] == 1

            repo = _segment_repo(tmp_path)
            manifest = repo.read_manifest()
            repo._segment_path(manifest["head_segment"]).write_bytes(b"corrupt")
            corrupt = await service.status(deep=True)
            assert corrupt["repositories"][0]["object_integrity_ok"] is False
            assert corrupt["repositories"][0]["clean"] is False
            assert corrupt["repositories"][0]["drift"]
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_segment_status_and_log_reject_unknown_repository(
    tmp_path, monkeypatch
):
    from fastapi import HTTPException

    from app.database import async_session, engine
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService

    _configure_shadow_projection(monkeypatch, gsvc.settings, tmp_path)
    try:
        async with async_session() as db:
            await _clear(db)
            service = GitlleryService(db)
            with pytest.raises(HTTPException) as status_error:
                await service.status("missing")
            assert status_error.value.status_code == 404

            with pytest.raises(HTTPException) as log_error:
                await service.log("missing")
            assert log_error.value.status_code == 404
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
