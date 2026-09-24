from __future__ import annotations

import pytest
from sqlalchemy import text


async def _clear(db):
    await db.execute(
        text(
            "TRUNCATE gitllery_builds, gitllery_projection_targets, "
            "gitllery_projection_outbox, "
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

            status = await service.status(repository_id, deep=True)
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
            corrupt = await service.status(repository_id, deep=True)
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_library_segment_status_is_database_only_and_reports_rollout_metadata(
    tmp_path, monkeypatch
):
    """The ordinary 804-repository status path must never touch NAS paths."""

    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService
    from gitllery_format import SegmentRepository

    _configure_shadow_projection(monkeypatch, gsvc.settings, tmp_path)

    def unexpected_filesystem_probe(_self):
        raise AssertionError("ordinary library status probed the filesystem")

    monkeypatch.setattr(SegmentRepository, "exists", unexpected_filesystem_probe)
    try:
        async with async_session() as db:
            await _clear(db)
            _creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works(
                [work.id], message="unplanned"
            )
            await db.commit()

            intent = await db.scalar(
                text(
                    "SELECT id FROM gitllery_projection_outbox "
                    "WHERE commit_id = :commit_id"
                ).bindparams(commit_id=commit.id)
            )
            assert intent is not None

            status = await GitlleryService(db).status()

            assert status["unplanned_intents"] == 1
            assert status["legacy_repositories"] == 1
            assert status["segment_repositories"] == 0
            assert status["projection_state"] == "shadow_unbuilt"
            assert status["last_verified_at"] is None
            assert status["repositories"][0]["exists"] is False
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_library_segment_status_reuses_its_thirty_second_cache(
    tmp_path, monkeypatch
):
    from sqlalchemy import event

    from app.database import async_session, engine
    from app.services import cache as cache_service
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService

    _configure_shadow_projection(monkeypatch, gsvc.settings, tmp_path)
    values = {}
    monkeypatch.setattr(cache_service, "cache_get", values.get)
    monkeypatch.setattr(
        cache_service,
        "cache_set",
        lambda key, value, _ttl: values.__setitem__(key, value),
    )
    observed: list[str] = []

    def record_query(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            observed.append(statement)

    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_work(db)
            service = GitlleryService(db)

            first = await service.status()
            observed.clear()
            event.listen(engine.sync_engine, "before_cursor_execute", record_query)
            try:
                second = await service.status()
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", record_query)

            assert second == first
            assert observed == []
            assert len(values) == 1
            assert next(iter(values)).startswith("cache:api:gitllery:status:")
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_library_deep_status_requires_async_verification(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.database import async_session, engine
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService

    _configure_shadow_projection(monkeypatch, gsvc.settings, tmp_path)
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_work(db)
            with pytest.raises(HTTPException) as error:
                await GitlleryService(db).status(deep=True)
            assert error.value.status_code == 409
            assert error.value.detail["code"] == "gitllery_async_verify_required"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
