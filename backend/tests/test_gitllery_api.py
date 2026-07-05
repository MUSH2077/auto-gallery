import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text


async def _clear(db):
    await db.execute(text(
        "TRUNCATE curation_changes, curation_commits, work_sources, works, "
        "source_creators, creators RESTART IDENTITY CASCADE"))
    await db.commit()


@pytest.fixture(autouse=True)
def _clear_gitllery_status_cache():
    from app.services.cache import cache_delete_pattern
    cache_delete_pattern("gitllery:status:*")
    yield


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_and_reconcile_endpoints(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.main import app
    from app.auth import get_admin_key
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.models import Creator, SourceCreator, Work, WorkSource

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path / "gallerydl-config"))
    app.dependency_overrides[get_admin_key] = lambda: "admin"
    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗"); db.add(creator); await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work = Work(title="w"); db.add(work); await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={"user": {"name": "七诗"}}))
            await db.commit()
            await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/curation/gitllery/status")
            assert r.status_code == 200
            assert r.json()["behind_total"] >= 1

            r = await client.post("/api/v1/curation/gitllery/reconcile")
            assert r.status_code == 200
            assert r.json()["status"]["behind_total"] == 0
    finally:
        app.dependency_overrides.pop(get_admin_key, None)
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuild_endpoint_dry_run(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.main import app
    from app.auth import get_admin_key
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.service import GitlleryService
    from app.models import Creator, SourceCreator, Subscription, SubscriptionSource, Work, WorkSource

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path / "gallerydl-config"))
    app.dependency_overrides[get_admin_key] = lambda: "admin"
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

        async with async_session() as db:
            await _clear(db)
            creator2 = Creator(name="七诗"); db.add(creator2); await db.flush()
            db.add(SourceCreator(creator_id=creator2.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work2 = Work(title="w"); db.add(work2); await db.flush()
            db.add(WorkSource(work_id=work2.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={}))
            await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/v1/curation/gitllery/rebuild?dry_run=true")
            assert r.status_code == 200
            body = r.json()
            assert body["dry_run"] is True
            assert body["commits_restored"] >= 1
    finally:
        app.dependency_overrides.pop(get_admin_key, None)
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
