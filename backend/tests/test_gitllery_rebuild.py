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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuild_restores_state_and_manual_commits_idempotently(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.service import GitlleryService
    from app.models import Creator, SourceCreator, Subscription, SubscriptionSource, Work, WorkSource
    from app.models import CurationCommit, WorkCurationState
    from sqlalchemy import select, func

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗"); db.add(creator); await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            sub = Subscription(creator_id=creator.id, name="七诗"); db.add(sub); await db.flush()
            ss = SubscriptionSource(subscription_id=sub.id, source="pixiv",
                                    source_creator_id="123",
                                    source_url="https://www.pixiv.net/users/123")
            db.add(ss); await db.flush()
            work = Work(title="w"); db.add(work); await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={"user": {"name": "七诗"}}))
            await db.commit()
            # Mirror the real pipeline: the work enters via an import commit
            # (source_synced) whose repository change carries the
            # (work_id, source, source_work_id) anchor _build_maps needs,
            # then the operator manually trashes it.
            svc = CurationService(db)
            import_commit, _vis = await svc.record_imported_work(
                work, creator_id=creator.id, repository_id=ss.id,
                source="pixiv", source_work_id="9001")
            await db.commit()
            await GitlleryService(db).project_commit(import_commit.id)
            commit = await svc.trash_works([work.id], message="trash 1")
            await db.commit()
            await GitlleryService(db).project_commit(commit.id)

        async with async_session() as db:
            await _clear(db)
            creator2 = Creator(name="七诗"); db.add(creator2); await db.flush()
            db.add(SourceCreator(creator_id=creator2.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work2 = Work(title="w"); db.add(work2); await db.flush()
            db.add(WorkSource(work_id=work2.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={}))
            await db.commit()
            new_work_id = work2.id

        # dry_run must report the work it WOULD do but write NOTHING.
        async with async_session() as db:
            dry = await GitlleryService(db).rebuild_db_from_disk(dry_run=True)
            assert dry["dry_run"] is True and dry["commits_restored"] >= 1
        async with async_session() as db:
            assert (await db.execute(select(func.count(CurationCommit.id)))).scalar() == 0
            assert (await db.execute(select(func.count(WorkCurationState.id)))).scalar() == 0

        async with async_session() as db:
            report = await GitlleryService(db).rebuild_db_from_disk()
            assert report["commits_restored"] >= 1
        async with async_session() as db:
            st = (await db.execute(select(WorkCurationState).where(
                WorkCurationState.work_id == new_work_id))).scalar_one_or_none()
            assert st is not None and st.visibility == "trashed"
            manual = (await db.execute(select(func.count(CurationCommit.id)).where(
                CurationCommit.trigger == "work_trash"))).scalar()
            assert manual >= 1

        async with async_session() as db:
            await GitlleryService(db).rebuild_db_from_disk()
        async with async_session() as db:
            manual2 = (await db.execute(select(func.count(CurationCommit.id)).where(
                CurationCommit.trigger == "work_trash"))).scalar()
            assert manual2 == manual
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuild_remaps_reverts_chain(tmp_path, monkeypatch):
    """A rebuilt revert commit's reverts_commit_id points at the NEW id of the
    rebuilt commit it reverts (old→new commit re-mapping)."""
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.service import GitlleryService
    from app.models import Creator, SourceCreator, Subscription, SubscriptionSource, Work, WorkSource
    from app.models import CurationCommit, WorkCurationState
    from sqlalchemy import select

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗"); db.add(creator); await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            sub = Subscription(creator_id=creator.id, name="七诗"); db.add(sub); await db.flush()
            ss = SubscriptionSource(subscription_id=sub.id, source="pixiv",
                                    source_creator_id="123",
                                    source_url="https://www.pixiv.net/users/123")
            db.add(ss); await db.flush()
            work = Work(title="w"); db.add(work); await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={"user": {"name": "七诗"}}))
            await db.commit()
            svc = CurationService(db)
            import_commit, _vis = await svc.record_imported_work(
                work, creator_id=creator.id, repository_id=ss.id,
                source="pixiv", source_work_id="9001")
            await db.commit()
            await GitlleryService(db).project_commit(import_commit.id)
            trash_commit = await svc.trash_works([work.id], message="trash 1")
            await db.commit()
            await GitlleryService(db).project_commit(trash_commit.id)
            old_trash_id = str(trash_commit.id)
            # revert_commit self-commits; project the revert too.
            revert_result = await svc.revert_commit(trash_commit.id)
            await GitlleryService(db).project_commit(revert_result["commit"].id)

        # Simulate disk-import rebuild: wipe everything, recreate with NEW uuids.
        async with async_session() as db:
            await _clear(db)
            creator2 = Creator(name="七诗"); db.add(creator2); await db.flush()
            db.add(SourceCreator(creator_id=creator2.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work2 = Work(title="w"); db.add(work2); await db.flush()
            db.add(WorkSource(work_id=work2.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={}))
            await db.commit()
            new_work_id = work2.id

        async with async_session() as db:
            report = await GitlleryService(db).rebuild_db_from_disk()
            assert report["commits_restored"] >= 2  # trash + revert
        async with async_session() as db:
            new_trash = (await db.execute(select(CurationCommit).where(
                CurationCommit.dedupe_key == f"rebuild:{old_trash_id}"))).scalar_one()
            new_revert = (await db.execute(select(CurationCommit).where(
                CurationCommit.trigger == "commit_revert"))).scalar_one()
            assert new_revert.reverts_commit_id == new_trash.id
            # Final state reflects the revert: work is visible again.
            st = (await db.execute(select(WorkCurationState).where(
                WorkCurationState.work_id == new_work_id))).scalar_one_or_none()
            assert st is not None and st.visibility == "visible"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collector_skips_previously_rebuilt_commits(tmp_path, monkeypatch):
    """On-disk commits whose dedupe_key starts with 'rebuild:' (projected back
    to disk after a previous rebuild) must NOT be re-ingested — otherwise every
    rebuild→project→rebuild cycle doubles the manual history."""
    from app.database import async_session, engine
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.repo import GitlleryRepo

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    try:
        repo = GitlleryRepo(tmp_path, "pixiv", "alice")
        repo.init({"schema_version": 1, "source": "pixiv", "creator_dir": "alice",
                   "creator_id": str(uuid.uuid4()), "source_creator_id": "alice",
                   "repository_id": str(uuid.uuid4())}, "pixiv/alice")
        blob = repo.objects.write({"type": "blob", "subject_type": "work",
                                   "subject_id": "w-1", "state": {"visibility": "trashed"}})
        tree = repo.objects.write({"type": "tree", "entries": {"work/w-1": blob}})
        original = repo.objects.write({
            "type": "commit", "tree": tree, "parent": None,
            "db_commit_id": str(uuid.uuid4()), "actor_type": "admin", "actor_id": None,
            "message": "trash 1", "trigger": "work_trash", "dedupe_key": None,
            "reverts": None, "occurred_at": "2026-06-30T10:00:00+00:00", "stats": {},
            "changes": [{"subject_type": "work", "subject_id": "w-1",
                         "action": "work_trashed", "diff": {}, "impact": {}}]})
        rebuilt = repo.objects.write({
            "type": "commit", "tree": tree, "parent": original,
            "db_commit_id": str(uuid.uuid4()), "actor_type": "system", "actor_id": None,
            "message": "trash 1", "trigger": "work_trash",
            "dedupe_key": f"rebuild:{uuid.uuid4()}",
            "reverts": None, "occurred_at": "2026-07-01T10:00:00+00:00", "stats": {},
            "changes": [{"subject_type": "work", "subject_id": "w-1",
                         "action": "work_trashed", "diff": {}, "impact": {}}]})
        repo.set_head(rebuilt, actor="system", message="rebuilt")

        async with async_session() as db:
            await _clear(db)
            merged = rb.GitlleryRebuilder(db)._collect_merged_commits([(repo, {})])
            assert len(merged) == 1
            assert merged[0].trigger == "work_trash"
            assert merged[0].occurred_at == "2026-06-30T10:00:00+00:00"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuild_refuses_on_live_manual_curation(tmp_path, monkeypatch):
    """A real rebuild against a DB that already holds LIVE manual curation
    (non-auto, non-rebuild commits) must refuse with 409; dry_run stays allowed."""
    import pytest as _pytest
    from fastapi import HTTPException
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.service import GitlleryService
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
            # LIVE manual curation in the current DB (not from a rebuild).
            await CurationService(db).trash_works([work.id], message="live trash")
            await db.commit()

            with _pytest.raises(HTTPException) as ei:
                await GitlleryService(db).rebuild_db_from_disk()
            assert ei.value.status_code == 409
            # dry_run is read-only and not gated.
            report = await GitlleryService(db).rebuild_db_from_disk(dry_run=True)
            assert report["dry_run"] is True
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuild_repository_id_filter_matches_current_id(tmp_path, monkeypatch):
    """rebuild(repository_id=<CURRENT subscription_source id>) resolves via
    (source, source_creator_id) and matches the on-disk repo whose config holds
    the OLD id."""
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.service import GitlleryService
    from app.models import Creator, SourceCreator, Subscription, SubscriptionSource, Work, WorkSource

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗"); db.add(creator); await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            sub = Subscription(creator_id=creator.id, name="七诗"); db.add(sub); await db.flush()
            ss = SubscriptionSource(subscription_id=sub.id, source="pixiv",
                                    source_creator_id="123",
                                    source_url="https://www.pixiv.net/users/123")
            db.add(ss); await db.flush()
            work = Work(title="w"); db.add(work); await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={"user": {"name": "七诗"}}))
            await db.commit()
            svc = CurationService(db)
            import_commit, _vis = await svc.record_imported_work(
                work, creator_id=creator.id, repository_id=ss.id,
                source="pixiv", source_work_id="9001")
            await db.commit()
            await GitlleryService(db).project_commit(import_commit.id)
            commit = await svc.trash_works([work.id], message="trash 1")
            await db.commit()
            await GitlleryService(db).project_commit(commit.id)

        async with async_session() as db:
            await _clear(db)
            creator2 = Creator(name="七诗"); db.add(creator2); await db.flush()
            db.add(SourceCreator(creator_id=creator2.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            sub2 = Subscription(creator_id=creator2.id, name="七诗"); db.add(sub2); await db.flush()
            ss2 = SubscriptionSource(subscription_id=sub2.id, source="pixiv",
                                     source_creator_id="123",
                                     source_url="https://www.pixiv.net/users/123")
            db.add(ss2); await db.flush()
            work2 = Work(title="w"); db.add(work2); await db.flush()
            db.add(WorkSource(work_id=work2.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={}))
            await db.commit()
            new_repo_id = str(ss2.id)

        async with async_session() as db:
            report = await GitlleryService(db).rebuild_db_from_disk(repository_id=new_repo_id)
            assert report["commits_restored"] >= 1
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
