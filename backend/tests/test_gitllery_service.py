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


@pytest.fixture(autouse=True)
def _clear_gitllery_status_cache():
    from app.services.cache import cache_delete_pattern
    cache_delete_pattern("gitllery:status:*")
    yield


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_clean_after_projection_and_behind_before(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    # Isolate creator_dir resolution from any real gallery-dl config mounted in
    # this environment (matches the other tests in this file); otherwise an
    # on-disk config.json with a stale directory template can change which
    # folder name resolve_creator_directory() produces.
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path / "gallerydl-config"))
    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()

            before = await GitlleryService(db).status()
            assert before["behind_total"] >= 1

            await GitlleryService(db).reconcile()
            after = await GitlleryService(db).status()
            assert after["behind_total"] == 0
            assert all(r["clean"] for r in after["repositories"])
            assert all(r["object_integrity_ok"] for r in after["repositories"])
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_projection_failure_does_not_break_caller(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import project_commit_safe, GitlleryService

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))

    async def _boom(self, commit_id):
        raise RuntimeError("disk full")
    monkeypatch.setattr(GitlleryService, "project_commit", _boom)

    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()
            # Best-effort wrapper must swallow the failure (no raise).
            await project_commit_safe(db, commit.id)

        # Recovery: real projector + catch-up makes status clean.
        monkeypatch.undo()
        monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
        async with async_session() as db:
            await GitlleryService(db).reconcile()
            assert (await GitlleryService(db).status())["behind_total"] == 0
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integrity_detects_corrupt_blob(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService
    from app.services.gitllery.slicing import RepoResolver

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()
            await GitlleryService(db).project_commit(commit.id)

            svc = GitlleryService(db)
            repos = await RepoResolver(db).all_repositories()
            assert len(repos) == 1
            repo = svc._repo_for(repos[0])
            assert svc._integrity_ok(repo) is True

            # Corrupt one tree-referenced blob's stored bytes.
            head = repo.head_commit()
            tree_hash = repo.objects.read(head)["tree"]
            blob_hash = next(iter(repo.objects.read(tree_hash)["entries"].values()))
            (repo.root / "objects" / blob_hash[:2] / blob_hash[2:]).write_bytes(b"corrupt-not-zlib")

            assert svc._integrity_ok(repo) is False
            # Light (default) status only probes the HEAD commit object — the
            # corrupt blob is invisible to it, so the repo still reports OK.
            light = await GitlleryService(db).status()
            assert light["deep"] is False
            assert all(r["object_integrity_ok"] for r in light["repositories"])
            # Deep verify catches the corrupt blob.
            s = await GitlleryService(db).status(deep=True)
            assert s["deep"] is True
            assert any(r["object_integrity_ok"] is False for r in s["repositories"])
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_uses_batched_changes_loader(tmp_path, monkeypatch):
    """status() must use the single-query batched loader, never the per-commit
    _changes_for_commit N+1 (711 sequential queries at current library size)."""
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()
            await GitlleryService(db).project_commit(commit.id)

            calls = {"n": 0}
            original = GitlleryService._changes_for_commit

            async def counting(self, commit_id):
                calls["n"] += 1
                return await original(self, commit_id)
            monkeypatch.setattr(GitlleryService, "_changes_for_commit", counting)

            s = await GitlleryService(db).status()
            assert calls["n"] == 0
            assert s["deep"] is False
            assert s["behind_total"] == 0
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_and_log_404_for_unknown_repository(tmp_path, monkeypatch):
    import uuid as _uuid
    from fastapi import HTTPException
    from app.database import async_session, engine
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            unknown = str(_uuid.uuid4())
            with pytest.raises(HTTPException) as ei:
                await GitlleryService(db).status(unknown)
            assert ei.value.status_code == 404
            with pytest.raises(HTTPException) as ei2:
                await GitlleryService(db).log(unknown)
            assert ei2.value.status_code == 404
            # Library-wide status (no id) must NOT 404.
            s = await GitlleryService(db).status()
            assert "repositories" in s
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_cached_and_invalidated_on_projection(tmp_path, monkeypatch):
    """status() is cached (TTL) and the cache is invalidated by projection."""
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            svc_cur = CurationService(db)
            commit = await svc_cur.trash_works([work.id], message="trash 1")
            await db.commit()
            await GitlleryService(db).project_commit(commit.id)

            calls = {"n": 0}
            original = GitlleryService._pending_count_by_repo

            async def counting(self, repository_id):
                calls["n"] += 1
                return await original(self, repository_id)
            monkeypatch.setattr(GitlleryService, "_pending_count_by_repo", counting)

            first = await GitlleryService(db).status()
            second = await GitlleryService(db).status()
            assert calls["n"] == 1          # second call served from cache
            assert second == first

            # A new projection invalidates the cache → third call recomputes.
            commit2 = await svc_cur.restore_works([work.id], message="restore 1")
            await db.commit()
            await GitlleryService(db).project_commit(commit2.id)
            third = await GitlleryService(db).status()
            assert calls["n"] == 2
            assert third["behind_total"] == 0
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
