import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

# These endpoints live in app.api.curation, whose router dependency is
# RequirePermission("curation") (Task 6). RequirePermission calls
# app.auth.get_admin_key as a plain function inside its own closure rather
# than via FastAPI's Depends() resolution, so app.dependency_overrides can no
# longer intercept it — these tests authenticate with a real admin JWT instead.
ADMIN_USERNAME = "gitllery_test_admin"


async def _clear(db):
    await db.execute(text(
        "TRUNCATE curation_changes, curation_commits, work_sources, works, "
        "source_creators, creators RESTART IDENTITY CASCADE"))
    await db.commit()


async def _seed_admin(db):
    from sqlalchemy import select
    from app.auth import hash_password
    from app.models.user import User

    existing = (await db.execute(select(User).where(User.username == ADMIN_USERNAME))).scalar_one_or_none()
    if existing:
        return existing
    user = User(
        username=ADMIN_USERNAME, password_hash=hash_password("hunter22"),
        is_admin=True, is_active=True, must_change_password=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _clear_admin(db):
    await db.execute(text(f"DELETE FROM users WHERE username = '{ADMIN_USERNAME}'"))
    await db.commit()


def _admin_headers():
    from app.auth import create_access_token
    token = create_access_token(ADMIN_USERNAME, must_change_password=False)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _clear_gitllery_status_cache():
    from app.services.cache import cache_delete_pattern
    from app.services.gitllery.service import _reset_checkpoint
    cache_delete_pattern("gitllery:status:*")
    _reset_checkpoint()
    yield


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_and_reconcile_endpoints_are_shadow_safe(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.main import app
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.models import Creator, SourceCreator, Work, WorkSource

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path / "gallerydl-config"))
    headers = _admin_headers()
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_admin(db)
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
            # Shadow mode reports the bounded v1 segment projection without a
            # legacy full-history checkpoint walk.
            r = await client.get("/api/v1/curation/gitllery/status", headers=headers)
            assert r.status_code == 200
            assert r.json()["needs_reconcile"] is False

            # Production rollout keeps Gitllery v1 visible but refuses every
            # full-history/projection maintenance mutation in shadow mode.
            r = await client.post("/api/v1/curation/gitllery/reconcile", headers=headers)
            assert r.status_code == 409
            assert r.json()["detail"]["code"] == "gitllery_shadow_only"
            for command in ("push", "pull"):
                r = await client.post(
                    f"/api/v1/curation/gitllery/{command}",
                    headers=headers,
                    json={"repository_id": "fixture", "product_version": "v1"},
                )
                assert r.status_code == 409
                assert r.json()["detail"]["code"] == "gitllery_shadow_only"
    finally:
        async with async_session() as db:
            await _clear(db)
            await _clear_admin(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuild_endpoint_dry_run(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.main import app
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.service import GitlleryService
    from app.models import Creator, SourceCreator, Subscription, SubscriptionSource, Work, WorkSource

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(tmp_path / "gallerydl-config"))
    headers = _admin_headers()
    try:
        async with async_session() as db:
            await _clear(db)
            await _seed_admin(db)
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
            r = await client.post("/api/v1/curation/gitllery/rebuild?dry_run=true", headers=headers)
            assert r.status_code == 200
            body = r.json()
            assert body["dry_run"] is True
            assert body["projection_mode"] == "shadow"
            assert body["legacy_layout_will_remain_read_only"] is True
    finally:
        async with async_session() as db:
            await _clear(db)
            await _clear_admin(db)
        await engine.dispose()
