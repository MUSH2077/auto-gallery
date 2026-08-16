"""Task 8 (B1): manual upload backend.

Follows tests/test_disk_import.py's fixture pattern: monkeypatch
settings.download_root to a tmp_path, truncate the pipeline tables, and run
the service against the real (isolated) test PostgreSQL/Redis.
"""
import io
import json
import uuid

import pytest
from sqlalchemy import select, text

PREFIX = "manual_upload_test_"


async def _clear(db):
    await db.execute(text(f"DELETE FROM users WHERE username LIKE '{PREFIX}%'"))
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


async def _make_user(db, username, *, is_admin=False, permissions=None, quota=None, used=0):
    from app.auth import hash_password
    from app.models.user import User

    user = User(
        username=username,
        password_hash=hash_password("hunter22"),
        is_admin=is_admin,
        is_active=True,
        permissions=permissions or [],
        upload_quota_bytes=quota,
        upload_used_bytes=used,
        must_change_password=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _make_upload_file(directory, name, content):
    from app.services.manual_upload import UploadFileInput
    p = directory / name
    p.write_bytes(content)
    return UploadFileInput(temp_path=p, original_name=name)


async def _fake_enqueue(download_job_id, import_error=None, new_json_paths=None):
    return "fake-import-job"


async def _no_import_cooldown(*_args, **_kwargs):
    """Keep upload integration tests independent of host pressure sampling."""
    return 0.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_happy_path(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.download_job import DownloadJob
    from app.models.storage_artifact import StorageArtifact
    from app.services.manual_upload import ManualUploadService

    download_root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(download_root))

    enqueued: list[tuple[str, set]] = []

    async def fake_enqueue(download_job_id, import_error=None, new_json_paths=None):
        enqueued.append((download_job_id, set(new_json_paths or [])))
        return "fake-import-job"

    monkeypatch.setattr("app.jobs.download._enqueue_import", fake_enqueue)

    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, f"{PREFIX}alice", quota=10 * 1024 * 1024)

            incoming = tmp_path / "incoming"
            incoming.mkdir()
            f = _make_upload_file(incoming, "photo.png", _png_bytes())
            size = f.temp_path.stat().st_size

            result = await ManualUploadService().save_upload(
                db, user, [f], {"title": "My upload", "tags": "a, b", "is_nsfw": False},
            )

            assert result.work_id
            assert result.download_job_id
            assert result.import_job_id == "fake-import-job"
            assert result.used_bytes == size
            assert result.quota_bytes == 10 * 1024 * 1024
            assert len(enqueued) == 1
            assert enqueued[0][0] == result.download_job_id

            work_dir = download_root / "manual" / f"user_{user.username}" / result.work_id
            assert work_dir.is_dir()
            saved_files = sorted(p.name for p in work_dir.iterdir())
            assert "metadata.json" in saved_files
            assert len(saved_files) == 2  # image + metadata.json

            metadata = json.loads((work_dir / "metadata.json").read_text())
            assert metadata["category"] == "manual"
            assert metadata["id"] == result.work_id
            assert metadata["uploaded_by"] == user.username
            assert metadata["target_creator_id"] is None
            assert metadata["tags"] == ["a", "b"]
            assert len(metadata["files"]) == 1
            assert metadata["files"][0]["original_name"] == "photo.png"
            assert metadata["files"][0]["size"] == size

            job = (await db.execute(
                select(DownloadJob).where(DownloadJob.id == uuid.UUID(result.download_job_id))
            )).scalar_one()
            assert job.source == "manual"
            assert job.status == "downloaded"
            assert job.subscription_id is not None
            # Deliberately NULL -- see the comment in manual_upload.py next to
            # DownloadJob's construction (uq_download_jobs_active_source would
            # otherwise block a second upload sharing the same identity).
            assert job.subscription_source_id is None
            assert job.source_url == f"manual://{result.work_id}"

            artifacts = (await db.execute(select(StorageArtifact))).scalars().all()
            assert len(artifacts) == 2  # image + metadata.json
            assert {a.artifact_type for a in artifacts} == {"image", "metadata_json"}

            await db.refresh(user)
            assert user.upload_used_bytes == size
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_rejects_bad_magic_bytes(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.services.manual_upload import ManualUploadService

    download_root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr("app.jobs.download._enqueue_import", _fake_enqueue)

    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, f"{PREFIX}bob")

            incoming = tmp_path / "incoming"
            incoming.mkdir()
            f = _make_upload_file(incoming, "fake.jpg", b"this is not a real jpeg file at all")

            with pytest.raises(ValueError):
                await ManualUploadService().save_upload(db, user, [f], {})

            # No side effects on validation failure: nothing written to disk,
            # quota untouched.
            assert not (download_root / "manual").exists()
            await db.refresh(user)
            assert user.upload_used_bytes == 0
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_rejects_disallowed_extension(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.services.manual_upload import ManualUploadService

    download_root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(download_root))

    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, f"{PREFIX}zoe")

            incoming = tmp_path / "incoming"
            incoming.mkdir()
            f = _make_upload_file(incoming, "payload.exe", b"MZ" + b"\x00" * 30)

            with pytest.raises(ValueError):
                await ManualUploadService().save_upload(db, user, [f], {})
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_quota_exceeded(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.services.manual_upload import ManualUploadService, QuotaExceededError

    download_root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(download_root))

    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, f"{PREFIX}carol", quota=10)  # tiny quota

            incoming = tmp_path / "incoming"
            incoming.mkdir()
            f = _make_upload_file(incoming, "photo.png", _png_bytes())
            assert f.temp_path.stat().st_size > 10

            with pytest.raises(QuotaExceededError):
                await ManualUploadService().save_upload(db, user, [f], {})

            assert not (download_root / "manual").exists()
            await db.refresh(user)
            assert user.upload_used_bytes == 0
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_creator_id_without_curation_is_forbidden(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.services.manual_upload import ManualUploadService

    download_root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(download_root))

    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, f"{PREFIX}dave", permissions=["upload"])  # no curation
            target = Creator(name="target_creator", display_name="Target Creator")
            db.add(target)
            await db.flush()
            await db.commit()

            incoming = tmp_path / "incoming"
            incoming.mkdir()
            f = _make_upload_file(incoming, "photo.png", _png_bytes())

            with pytest.raises(PermissionError):
                await ManualUploadService().save_upload(
                    db, user, [f], {"creator_id": str(target.id)},
                )

            assert not (download_root / "manual").exists()
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_creator_id_with_curation_links_target_creator(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.source_creator import SourceCreator
    from app.models.subscription import Subscription
    from app.services.manual_upload import ManualUploadService

    download_root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr("app.jobs.download._enqueue_import", _fake_enqueue)

    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, f"{PREFIX}erin_curator", permissions=["curation"])
            target = Creator(name="target_creator2", display_name="Target Creator 2")
            db.add(target)
            await db.flush()
            await db.commit()

            incoming = tmp_path / "incoming"
            incoming.mkdir()
            f = _make_upload_file(incoming, "photo.png", _png_bytes())

            result = await ManualUploadService().save_upload(
                db, user, [f], {"creator_id": str(target.id)},
            )
            assert result.work_id

            metadata = json.loads(
                (download_root / "manual" / f"user_{user.username}" / result.work_id / "metadata.json").read_text()
            )
            assert metadata["target_creator_id"] == str(target.id)

            subs = (await db.execute(select(Subscription).where(Subscription.creator_id == target.id))).scalars().all()
            assert len(subs) == 1
            scs = (await db.execute(select(SourceCreator).where(
                SourceCreator.source == "manual",
                SourceCreator.source_creator_id == f"creator:{target.id}",
            ))).scalars().all()
            assert len(scs) == 1
            assert scs[0].creator_id == target.id
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_personal_space_reused_across_two_uploads(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.source_creator import SourceCreator
    from app.models.subscription import Subscription
    from app.services.manual_upload import ManualUploadService

    download_root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr("app.jobs.download._enqueue_import", _fake_enqueue)

    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, f"{PREFIX}frank", quota=100 * 1024 * 1024)

            incoming = tmp_path / "incoming"
            incoming.mkdir()
            f1 = _make_upload_file(incoming, "one.png", _png_bytes())
            r1 = await ManualUploadService().save_upload(db, user, [f1], {})

            f2 = _make_upload_file(incoming, "two.png", _png_bytes())
            r2 = await ManualUploadService().save_upload(db, user, [f2], {})

            assert r1.work_id != r2.work_id

            creators = (await db.execute(select(Creator))).scalars().all()
            assert len(creators) == 1

            subs = (await db.execute(select(Subscription))).scalars().all()
            assert len(subs) == 1

            scs = (await db.execute(select(SourceCreator).where(
                SourceCreator.source == "manual",
                SourceCreator.source_creator_id == f"user:{user.username}",
            ))).scalars().all()
            assert len(scs) == 1
            assert scs[0].creator_id == creators[0].id
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_runs_through_real_import_runner(tmp_path, monkeypatch):
    """The one required integration test: run the REAL import runner (not a
    monkeypatched _enqueue_import) over the synthesized manual metadata.json
    and confirm Work/Asset/WorkSource(source="manual") land in the DB."""
    from app.config import settings
    from app.database import async_session, engine
    from app.jobs.import_runner import run_import_job
    from app.models import Asset, Work, WorkSource
    from app.services.manual_upload import ManualUploadService

    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr(settings, "library_root", str(library_root))
    monkeypatch.setattr(
        "app.jobs.import_runner.sleep_for_profile_slice_cooldown",
        _no_import_cooldown,
    )

    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, f"{PREFIX}grace", quota=100 * 1024 * 1024)

            incoming = tmp_path / "incoming"
            incoming.mkdir()
            f = _make_upload_file(incoming, "art.png", _png_bytes())

            result = await ManualUploadService().save_upload(
                db, user, [f], {"title": "Integration work", "tags": "fixture"},
            )
            assert result.import_job_id

            await run_import_job(result.import_job_id)

            work = (await db.execute(
                select(Work).where(Work.title == "Integration work")
            )).scalar_one()
            ws = (await db.execute(
                select(WorkSource).where(WorkSource.work_id == work.id)
            )).scalar_one()
            assert ws.source == "manual"
            assert ws.source_work_id == result.work_id

            all_assets = (await db.execute(select(Asset))).scalars().all()
            assert len(all_assets) == 1
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_honors_manual_is_nsfw_flag(tmp_path, monkeypatch):
    """CRITICAL fix: _detect_nsfw() previously had no branch for source ==
    "manual" and always fell through to `return False`, silently dropping the
    uploader-supplied is_nsfw flag. Confirm both True and False propagate
    through the REAL import runner onto Work.is_nsfw."""
    from app.config import settings
    from app.database import async_session, engine
    from app.jobs.import_runner import run_import_job
    from app.models import Work
    from app.services.manual_upload import ManualUploadService

    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr(settings, "library_root", str(library_root))
    monkeypatch.setattr(
        "app.jobs.import_runner.sleep_for_profile_slice_cooldown",
        _no_import_cooldown,
    )

    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, f"{PREFIX}heidi", quota=100 * 1024 * 1024)

            incoming = tmp_path / "incoming"
            incoming.mkdir()

            f_true = _make_upload_file(incoming, "nsfw_true.png", _png_bytes())
            result_true = await ManualUploadService().save_upload(
                db, user, [f_true], {"title": "NSFW true work", "is_nsfw": True},
            )
            await run_import_job(result_true.import_job_id)

            f_false = _make_upload_file(incoming, "nsfw_false.png", _png_bytes())
            result_false = await ManualUploadService().save_upload(
                db, user, [f_false], {"title": "NSFW false work", "is_nsfw": False},
            )
            await run_import_job(result_false.import_job_id)

            work_true = (await db.execute(
                select(Work).where(Work.title == "NSFW true work")
            )).scalar_one()
            work_false = (await db.execute(
                select(Work).where(Work.title == "NSFW false work")
            )).scalar_one()

            assert work_true.is_nsfw is True
            assert work_false.is_nsfw is False
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_video_asset_imports_through_real_import_runner(tmp_path, monkeypatch):
    """IMPORTANT fix: video uploads (.mp4/.webm) previously validated and
    charged quota but ASSET_EXTENSIONS in import_runner.py excluded video
    extensions entirely, so the import ran with "no media assets found" and
    never created a Work. Confirm a webm upload produces a real Work + Asset
    row through the REAL import runner. This deliberately corrupt WebM also
    verifies that ffprobe/poster diagnostics do not fail the import."""
    from app.config import settings
    from app.database import async_session, engine
    from app.jobs.import_runner import run_import_job
    from app.models import Asset, Work, WorkSource
    from app.services.manual_upload import ManualUploadService

    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr(settings, "library_root", str(library_root))
    monkeypatch.setattr(
        "app.jobs.import_runner.sleep_for_profile_slice_cooldown",
        _no_import_cooldown,
    )

    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, f"{PREFIX}ivan", quota=100 * 1024 * 1024)

            incoming = tmp_path / "incoming"
            incoming.mkdir()
            # Tiny valid webm magic-bytes header + padding -- not a real
            # decodable video, but enough to pass _sniff_magic() and exercise
            # the full storage/import pipeline.
            webm_bytes = b"\x1a\x45\xdf\xa3" + b"\x00" * 32
            f = _make_upload_file(incoming, "clip.webm", webm_bytes)

            result = await ManualUploadService().save_upload(
                db, user, [f], {"title": "Video work", "tags": "fixture"},
            )
            assert result.import_job_id

            await run_import_job(result.import_job_id)

            work = (await db.execute(
                select(Work).where(Work.title == "Video work")
            )).scalar_one()
            ws = (await db.execute(
                select(WorkSource).where(WorkSource.work_id == work.id)
            )).scalar_one()
            assert ws.source == "manual"

            assets = (await db.execute(select(Asset))).scalars().all()
            assert len(assets) == 1
            assert assets[0].file_name.endswith(".webm")
            assert assets[0].thumb_sm_path is None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
