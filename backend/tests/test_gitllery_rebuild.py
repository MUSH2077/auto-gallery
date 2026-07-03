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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_maps_remaps_work_and_creator(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.rebuild import MergedCommit
    from app.models import Creator, SourceCreator, Work, WorkSource

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗"); db.add(creator); await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work = Work(title="w"); db.add(work); await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={}))
            await db.commit()

            old_work = str(uuid.uuid4()); old_creator = str(uuid.uuid4())
            cfg = {"creator_id": old_creator, "source": "pixiv", "source_creator_id": "123",
                   "repository_id": str(uuid.uuid4())}
            merged = [MergedCommit(
                db_commit_id=str(uuid.uuid4()), occurred_at=None, message="", trigger="work_trash",
                actor_type=None, actor_id=None, stats={}, reverts=None,
                changes=[{"subject_type": "work", "subject_id": old_work, "action": "work_trashed",
                          "diff": {}, "impact": {},
                          "after_state": {"source": "pixiv", "source_work_id": "9001"}}])]
            maps = await rb.GitlleryRebuilder(db)._build_maps([(None, cfg)], merged)
            assert maps["work"][old_work] == str(work.id)
            assert maps["creator"][old_creator] == str(creator.id)
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_maps_repository_map_survives_duplicate_subscription_sources(tmp_path, monkeypatch):
    """subscription_sources has no unique (source, source_creator_id) constraint,
    so two rows can share it (different subscriptions). The repository map must
    not crash with MultipleResultsFound — it maps to one of them."""
    from app.database import async_session, engine
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.models import Creator, Subscription, SubscriptionSource

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            ss_ids = []
            for i in range(2):
                c = Creator(name="七诗"); db.add(c); await db.flush()
                sub = Subscription(creator_id=c.id, name="七诗"); db.add(sub); await db.flush()
                ss = SubscriptionSource(subscription_id=sub.id, source="pixiv",
                                        source_creator_id="123",
                                        source_url=f"https://www.pixiv.net/users/123?s={i}")
                db.add(ss); await db.flush()
                ss_ids.append(str(ss.id))
            await db.commit()

            old_repo = str(uuid.uuid4())
            cfg = {"creator_id": str(uuid.uuid4()), "source": "pixiv",
                   "source_creator_id": "123", "repository_id": old_repo}
            maps = await rb.GitlleryRebuilder(db)._build_maps([(None, cfg)], [])
            assert maps["repository"][old_repo] in ss_ids
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
