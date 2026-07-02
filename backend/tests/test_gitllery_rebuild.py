import uuid
import pytest
from sqlalchemy import text


async def _clear(db):
    await db.execute(text(
        "TRUNCATE curation_changes, curation_commits, asset_sources, assets, "
        "work_sources, works, source_creators, subscription_sources, subscriptions, "
        "creators, work_curation_states, creator_curation_states, asset_storage_states "
        "RESTART IDENTITY CASCADE"))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collect_merges_commits_by_db_commit_id(tmp_path, monkeypatch):
    """Two repos each holding a slice of the same DB commit merge into one
    MergedCommit with the union of changes; auto triggers are flagged."""
    from app.database import async_session, engine
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.repo import GitlleryRepo

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    try:
        db_cid = str(uuid.uuid4())
        for cdir, sid in [("alice", "w-a"), ("bob", "w-b")]:
            repo = GitlleryRepo(tmp_path, "pixiv", cdir)
            repo.init({"schema_version": 1, "source": "pixiv", "creator_dir": cdir,
                       "creator_id": str(uuid.uuid4()), "source_creator_id": cdir,
                       "repository_id": str(uuid.uuid4())}, f"pixiv/{cdir}")
            blob = repo.objects.write({"type": "blob", "subject_type": "work",
                                       "subject_id": sid, "state": {"visibility": "trashed"}})
            tree = repo.objects.write({"type": "tree", "entries": {f"work/{sid}": blob}})
            commit = repo.objects.write({
                "type": "commit", "tree": tree, "parent": None, "db_commit_id": db_cid,
                "actor_type": "admin", "actor_id": None, "message": "trash 1",
                "trigger": "work_trash", "dedupe_key": None, "reverts": None,
                "occurred_at": "2026-06-30T10:00:00+00:00", "stats": {},
                "changes": [{"subject_type": "work", "subject_id": sid,
                             "action": "work_trashed", "diff": {}, "impact": {}}]})
            repo.set_head(commit, actor="admin", message="trash 1")

        async with async_session() as db:
            await _clear(db)
            rebuilder = rb.GitlleryRebuilder(db)
            repos = [(GitlleryRepo(tmp_path, "pixiv", "alice"), {}),
                     (GitlleryRepo(tmp_path, "pixiv", "bob"), {})]
            merged = rebuilder._collect_merged_commits(repos)
            assert len(merged) == 1
            mc = merged[0]
            assert mc.db_commit_id == db_cid
            assert mc.trigger == "work_trash" and mc.is_manual is True
            assert sorted(c["subject_id"] for c in mc.changes) == ["w-a", "w-b"]
            assert all(c["after_state"] == {"visibility": "trashed"} for c in mc.changes)
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
