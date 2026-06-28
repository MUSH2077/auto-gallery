import json

import pytest
from sqlalchemy import func, select, text, update


def test_reconcile_downloads_to_db_is_importable():
    # Contract smoke test: the service module exposes the entrypoint with the
    # documented signature. Behavioural coverage is via integration runs.
    from app.services.disk_import import reconcile_downloads_to_db
    import inspect

    sig = inspect.signature(reconcile_downloads_to_db)
    assert list(sig.parameters)[:2] == ["db", "options"]


async def _clear_pipeline_tables(db):
    await db.execute(text("""
        TRUNCATE
            storage_artifacts,
            import_jobs,
            download_jobs,
            work_source_tags,
            work_tags,
            asset_sources,
            assets,
            work_sources,
            works,
            creator_links,
            source_creators,
            subscription_sources,
            subscriptions,
            creators
        RESTART IDENTITY CASCADE
    """))
    await db.commit()


def _pixiv_metadata(work_id: int = 38362603, creator_id: int = 1980643) -> dict:
    return {
        "id": work_id,
        "title": "Fixture illustration",
        "description": "Synthetic disk import fixture",
        "date": "2024-01-01 12:00:00",
        "user": {"id": creator_id, "name": "Fixture Artist", "account": "fixture_artist"},
        "tags": [{"name": "fixture"}],
        "pages": [{"url": "https://example.test/img.jpg", "width": 16, "height": 16}],
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_downloads_to_db_registers_and_enqueues_idempotently(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services.disk_import import reconcile_downloads_to_db

    download_root = tmp_path / "downloads"
    work_dir = download_root / "pixiv" / "1980643" / "38362603"
    work_dir.mkdir(parents=True)
    (work_dir / "metadata.json").write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    (work_dir / "p0.jpg").write_bytes(b"not-a-real-jpeg-but-good-enough-for-ledger")
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr("app.services.disk_identity.danbooru_svc.search_and_extract", lambda **_: (None, []))

    enqueued: list[tuple[str, set[str] | None]] = []

    async def fake_enqueue(download_job_id: str, import_error: str | None = None, new_json_paths=None):
        enqueued.append((download_job_id, set(new_json_paths or [])))
        return "fake-import-job"

    monkeypatch.setattr("app.jobs.download._enqueue_import", fake_enqueue)

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
            creator = Creator(name="fixture_artist", display_name="Fixture Artist")
            db.add(creator)
            await db.flush()
            sub = Subscription(creator_id=creator.id, name="Fixture Artist", sync_enabled=True, schedule_mode="interval")
            db.add(sub)
            await db.flush()
            db.add(SubscriptionSource(
                subscription_id=sub.id,
                source="pixiv",
                source_creator_id="1980643",
                source_url="https://www.pixiv.net/users/1980643",
                is_enabled=True,
            ))
            await db.commit()

            result = await reconcile_downloads_to_db(db, {"source": "pixiv"})

            assert {k: result[k] for k in ("sources", "creators", "jobs", "skipped_done")} == {"sources": 1, "creators": 1, "jobs": 1, "skipped_done": 0}
            assert result["import_job_ids"] == ["fake-import-job"]
            assert len(enqueued) == 1
            assert enqueued[0][1] == {str(work_dir / "metadata.json")}
            assert sub.sync_enabled is True
            assert sub.schedule_mode == "interval"
            assert (await db.execute(select(func.count(DownloadJob.id)))).scalar_one() == 1
            ss = (await db.execute(select(SubscriptionSource))).scalar_one()
            assert ss.is_enabled is True
            assert (await db.execute(select(func.count(StorageArtifact.id)))).scalar_one() == 2

            await db.execute(update(StorageArtifact).values(state="done"))
            await db.commit()

            second = await reconcile_downloads_to_db(db, {"source": "pixiv"})

            assert {k: second[k] for k in ("sources", "creators", "jobs", "skipped_done")} == {"sources": 1, "creators": 0, "jobs": 0, "skipped_done": 1}
            assert second["import_job_ids"] == []
            assert len(enqueued) == 1
            assert (await db.execute(select(func.count(DownloadJob.id)))).scalar_one() == 1
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_downloads_to_db_provisions_metadata_creator_without_placeholder(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.source_creator import SourceCreator
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services.disk_import import reconcile_downloads_to_db

    download_root = tmp_path / "downloads"
    work_dir = download_root / "pixiv" / "1980643" / "38362603"
    work_dir.mkdir(parents=True)
    (work_dir / "metadata.json").write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    (work_dir / "p0.jpg").write_bytes(b"fixture")
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr("app.services.disk_identity.danbooru_svc.search_and_extract", lambda **_: (None, []))

    async def fake_enqueue(download_job_id: str, import_error: str | None = None, new_json_paths=None):
        return "fallback-import-job"

    monkeypatch.setattr("app.jobs.download._enqueue_import", fake_enqueue)

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)

            result = await reconcile_downloads_to_db(db, {"source": "pixiv"})

            assert result["jobs"] == 1
            assert result["metadata_fallback"] == 1
            job = (await db.execute(select(DownloadJob))).scalar_one()
            assert job.subscription_id is not None
            assert job.subscription_source_id is not None
            assert job.source_url == "https://www.pixiv.net/users/1980643"

            creator = (await db.execute(select(Creator))).scalar_one()
            assert creator.display_name == "Fixture Artist"
            assert creator.name != "recovered"
            assert "Imported from disk metadata" in (creator.description or "")

            sub = (await db.execute(select(Subscription))).scalar_one()
            assert sub.creator_id == creator.id
            assert sub.schedule_mode == "manual"
            assert sub.sync_enabled is False

            source = (await db.execute(select(SubscriptionSource))).scalar_one()
            assert source.subscription_id == sub.id
            assert source.source == "pixiv"
            assert source.is_enabled is False

            sc = (await db.execute(select(SourceCreator))).scalar_one()
            assert sc.creator_id == creator.id
            assert sc.source_creator_id == "1980643"
            assert sc.raw_metadata["_disk_import"]["needs_enrichment"] is True
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_downloads_to_db_applies_danbooru_enrichment(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.creator_link import CreatorLink
    from app.models.source_creator import SourceCreator
    from app.models.subscription_source import SubscriptionSource
    from app.services.disk_import import reconcile_downloads_to_db

    download_root = tmp_path / "downloads"
    work_dir = download_root / "pixiv" / "1980643" / "38362603"
    work_dir.mkdir(parents=True)
    (work_dir / "metadata.json").write_text(json.dumps(_pixiv_metadata()), encoding="utf-8")
    (work_dir / "p0.jpg").write_bytes(b"fixture")
    monkeypatch.setattr(settings, "download_root", str(download_root))
    artist = {
        "id": 12345,
        "name": "fixture_artist",
        "other_names": ["Fixture Artist"],
        "notes": "Known artist",
        "urls": [
            {"url": "https://www.pixiv.net/users/1980643", "normalized_url": "https://www.pixiv.net/users/1980643", "is_active": True},
            {"url": "https://x.com/fixture_artist", "normalized_url": "https://x.com/fixture_artist", "is_active": True},
            {"url": "https://weibo.com/u/123456", "normalized_url": "https://weibo.com/u/123456", "is_active": True},
            {"url": "https://weibo.com/n/fixture_artist", "normalized_url": "https://weibo.com/n/fixture_artist", "is_active": True},
        ],
    }
    links = [{
        "url": "https://www.pixiv.net/users/1980643",
        "link_type": "profile",
        "source": "pixiv",
        "confidence": 1.0,
        "is_verified": True,
        "notes": "Danbooru artist URL",
    }]
    monkeypatch.setattr("app.services.disk_identity.danbooru_svc.search_and_extract", lambda **_: (artist, links))

    async def fake_enqueue(download_job_id: str, import_error: str | None = None, new_json_paths=None):
        return "enriched-import-job"

    monkeypatch.setattr("app.jobs.download._enqueue_import", fake_enqueue)

    try:
        async with async_session() as db:
            await _clear_pipeline_tables(db)

            result = await reconcile_downloads_to_db(db, {"source": "pixiv"})

            assert result["jobs"] == 1
            assert result["danbooru_enriched"] == 1
            creator = (await db.execute(select(Creator))).scalar_one()
            assert creator.danbooru_artist_id == 12345
            assert (await db.execute(select(func.count(CreatorLink.id)))).scalar_one() == 1
            source_creators = (await db.execute(select(SourceCreator))).scalars().all()
            assert any(sc.source == "pixiv" and sc.source_creator_id == "1980643" for sc in source_creators)
            sources = (await db.execute(select(SubscriptionSource))).scalars().all()
            assert any(ss.source == "pixiv" and ss.is_enabled is False for ss in sources)
            assert any(ss.source == "danbooru" and ss.is_enabled is False for ss in sources)
            assert sum(1 for ss in sources if ss.source == "weibo") == 1
    finally:
        async with async_session() as db:
            await _clear_pipeline_tables(db)
        await engine.dispose()
