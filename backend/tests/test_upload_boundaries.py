"""Upload resource limits, temporary ownership and database accounting."""
import asyncio
from io import BytesIO

import httpx
import pytest
from fastapi import FastAPI, UploadFile

from tests.test_manual_upload import PREFIX, _clear, _make_user, _make_upload_file, _png_bytes, _fake_enqueue


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [None, "1", "99999"])
async def test_request_limit_counts_stream_before_route_copy(tmp_path, monkeypatch, declared):
    from app.api import upload
    from app.config import settings
    from app.database import get_db
    from types import SimpleNamespace
    monkeypatch.setattr(settings, "download_root", str(tmp_path))
    monkeypatch.setattr(upload, "MAX_REQUEST_SIZE_BYTES", 600)
    app = FastAPI()
    app.include_router(upload.router, prefix="/api/v1/upload")
    user = SimpleNamespace(upload_quota_bytes=None, upload_used_bytes=0)
    for dependency in upload._require_upload.dependency, get_db:
        app.dependency_overrides[dependency] = lambda: user
    copied = []
    async def copy(*args, **kwargs):
        copied.append(True)
        raise AssertionError("Oversized request must not reach copy")
    monkeypatch.setattr(upload, "_stream_to_temp", copy)
    body = b'--fixture\r\nContent-Disposition: form-data; name="files"; filename="p.png"\r\n\r\n' + b"x" * 2000 + b'\r\n--fixture--\r\n'
    read = 0
    async def chunks():
        nonlocal read
        for start in range(0, len(body), 200):
            read += 200
            yield body[start:start + 200]
    headers = {"Content-Type": "multipart/form-data; boundary=fixture"}
    if declared:
        headers["Content-Length"] = declared
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        result = await client.post("/api/v1/upload", content=chunks(), headers=headers)
    assert result.status_code == 413
    assert result.json()["detail"]["code"] == "upload_request_too_large"
    assert not copied
    assert read < len(body)
    assert not list(tmp_path.rglob("*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("known_size", [True, False])
async def test_file_copy_stops_at_budget_and_cleans_temp(tmp_path, known_size):
    from app.api.upload import _stream_to_temp
    from app.services.manual_upload import QuotaExceededError
    stream = BytesIO(b"x" * 100)
    upload = UploadFile(filename="photo.png", file=stream, size=100 if known_size else None)
    with pytest.raises(QuotaExceededError):
        await _stream_to_temp(upload, tmp_path, byte_limit=20)
    assert not list(tmp_path.iterdir())
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["batch", "quota", "file", "count"])
async def test_upload_api_rejects_budget_before_saving_and_cleans_batch(tmp_path, monkeypatch, limit):
    from app.api import upload
    from app.config import settings
    from app.database import get_db
    from types import SimpleNamespace
    from starlette.formparsers import MultiPartParser
    monkeypatch.setattr(settings, "download_root", str(tmp_path))
    monkeypatch.setattr(upload, "MAX_FILE_SIZE_BYTES", 32)
    monkeypatch.setattr(upload, "MAX_BATCH_SIZE_BYTES", 40)
    monkeypatch.setattr(upload, "MAX_UPLOAD_FILES", 2)
    monkeypatch.setattr(MultiPartParser, "spool_max_size", 1)
    user = SimpleNamespace(upload_quota_bytes=20 if limit == "quota" else None, upload_used_bytes=0)
    app = FastAPI()
    app.include_router(upload.router, prefix="/api/v1/upload")
    for dependency in upload._require_upload.dependency, get_db:
        app.dependency_overrides[dependency] = lambda: user
    saved = []
    async def save(*args):
        saved.append(True)
        raise AssertionError("Rejected budget must not reach save")
    monkeypatch.setattr(upload.ManualUploadService, "save_upload", save)
    content = b"x" * (33 if limit == "file" else 24)
    files = [("files", (f"{i}.png", content, "image/png")) for i in range(3 if limit == "count" else 2)]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        result = await client.post("/api/v1/upload", files=files)
    assert result.status_code == (400 if limit == "count" else 413)
    if limit != "count":
        assert result.json()["detail"]["code"] == {
            "file": "upload_file_too_large", "batch": "upload_batch_too_large",
            "quota": "upload_quota_exceeded",
        }[limit]
    assert not saved
    assert not list(tmp_path.rglob("*.png"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "cancel"])
async def test_multipart_failure_closes_parser_spools(tmp_path, monkeypatch, failure):
    from app.api import upload
    from app.config import settings
    import starlette.formparsers as parsers
    monkeypatch.setattr(settings, "download_root", str(tmp_path))
    monkeypatch.setattr(parsers.MultiPartParser, "spool_max_size", 1)
    factory = parsers.SpooledTemporaryFile
    files = []
    def spool(*args, **kwargs):
        file = factory(*args, **kwargs)
        files.append(file)
        return file
    monkeypatch.setattr(parsers, "SpooledTemporaryFile", spool)
    app = FastAPI()
    app.include_router(upload.router, prefix="/api/v1/upload")
    async def chunks():
        yield b'--fixture\r\nContent-Disposition: form-data; name="files"; filename="p.png"\r\n\r\n' + b"x" * 100
        if failure == "cancel":
            raise asyncio.CancelledError
        raise OSError("controlled stream disconnect")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await client.post("/api/v1/upload", content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=fixture"})
        else:
            result = await client.post("/api/v1/upload", content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=fixture"})
            assert result.status_code == 400
    assert files and all(file.closed for file in files)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "cancel"])
async def test_copy_failure_cleans_its_undelivered_temp_file(tmp_path, failure):
    from app.api.upload import _stream_to_temp
    upload = UploadFile(filename="photo.png", file=BytesIO(b"fixture"))
    reads = 0

    async def read(_size):
        nonlocal reads
        reads += 1
        if reads == 1:
            return b"fixture"
        if failure == "cancel":
            raise asyncio.CancelledError
        raise OSError("controlled read failure")

    upload.read = read
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else OSError):
        await _stream_to_temp(upload, tmp_path)
    assert not list(tmp_path.iterdir())
    assert upload.file.closed


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("quota", [True, False])
async def test_concurrent_uploads_atomically_account_accepted_bytes(tmp_path, monkeypatch, quota):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.user import User
    from app.services.manual_upload import ManualUploadService, QuotaExceededError
    monkeypatch.setattr(settings, "download_root", str(tmp_path / "downloads"))
    monkeypatch.setattr("app.services.import_intent.enqueue_import", _fake_enqueue)
    size = len(_png_bytes())
    barrier = asyncio.Barrier(2)
    async with async_session() as db:
        await _clear(db)
        user = await _make_user(db, PREFIX + "concurrent", quota=size if quota else None)
        user_id = user.id

    async def save(index):
        async with async_session() as db:
            user = await db.get(User, user_id)
            file = _make_upload_file(tmp_path, f"{index}.png", _png_bytes())
            await barrier.wait()
            try:
                return await ManualUploadService().save_upload(db, user, [file], {})
            except QuotaExceededError:
                return None

    try:
        results = await asyncio.wait_for(asyncio.gather(save(1), save(2)), timeout=15)
        accepted = 1 if quota else 2
        assert sum(result is not None for result in results) == accepted
        async with async_session() as db:
            assert (await db.get(User, user_id)).upload_used_bytes == accepted * size
        assert len(list((tmp_path / "downloads").rglob("metadata.json"))) == accepted
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("failure", ["commit", "move", "cancel"])
async def test_upload_save_failure_rolls_back_bytes_and_files(tmp_path, monkeypatch, failure):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.user import User
    from app.services.manual_upload import ManualUploadService
    root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(root))
    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, PREFIX + "failure")
            user_id = user.id
            files = [_make_upload_file(tmp_path, f"{index}.png", _png_bytes()) for index in (1, 2)]
            if failure in {"commit", "cancel"}:
                async def fail():
                    if failure == "cancel":
                        raise asyncio.CancelledError
                    raise RuntimeError("controlled commit failure")
                monkeypatch.setattr(db, "commit", fail)
            else:
                import app.services.manual_upload as module
                replace = module.os.replace
                calls = 0
                def fail(source, target):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise OSError("controlled move failure")
                    replace(source, target)
                monkeypatch.setattr(module.os, "replace", fail)
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else (RuntimeError, OSError)):
                await ManualUploadService().save_upload(db, user, files, {})
        async with async_session() as db:
            assert (await db.get(User, user_id)).upload_used_bytes == 0
        assert not list(root.rglob("*.png"))
        assert not list(root.rglob("metadata.json"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("identity_state", ["new", "complete", "missing_alias", "missing_repository"])
@pytest.mark.parametrize("limited", [True, False])
async def test_eight_uploads_preserve_identity_and_quota(tmp_path, monkeypatch, identity_state, limited):
    from sqlalchemy import func, select
    from app.config import settings
    from app.database import async_session, engine
    from app.models import CreatorAlias, DownloadJob, SourceCreator, SubscriptionSource, User
    from app.services.manual_upload import ManualUploadService, QuotaExceededError

    root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(root))
    monkeypatch.setattr("app.services.import_intent.enqueue_import", _fake_enqueue)
    size = len(_png_bytes())
    warmed = identity_state != "new"
    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, PREFIX + "eight", quota=None)
            user_id = user.id
            if warmed:
                await ManualUploadService().save_upload(db, user, [_make_upload_file(tmp_path, "warm.png", _png_bytes())], {})
                if identity_state == "missing_alias":
                    alias = (await db.execute(select(CreatorAlias).limit(1))).scalar_one()
                    await db.delete(alias)
                elif identity_state == "missing_repository":
                    repository = (await db.execute(select(SubscriptionSource).where(SubscriptionSource.source == "manual"))).scalar_one()
                    await db.delete(repository)
            user.upload_quota_bytes = (int(warmed) + 2) * size if limited else None
            await db.commit()
        barrier = asyncio.Barrier(8)
        async def save(index):
            async with async_session() as db:
                user = await db.get(User, user_id)
                file = _make_upload_file(tmp_path, f"eight-{index}.png", _png_bytes())
                await barrier.wait()
                try:
                    return await ManualUploadService().save_upload(db, user, [file], {})
                except QuotaExceededError:
                    return None
        results = await asyncio.wait_for(asyncio.gather(*(save(i) for i in range(8))), timeout=30)
        accepted = 2 if limited else 8
        assert sum(r is not None for r in results) == accepted
        async with async_session() as db:
            assert (await db.get(User, user_id)).upload_used_bytes == (int(warmed) + accepted) * size
            assert (await db.scalar(select(func.count()).select_from(SourceCreator).where(SourceCreator.source == "manual"))) == 1
            assert (await db.scalar(select(func.count()).select_from(SubscriptionSource).where(SubscriptionSource.source == "manual"))) == 1
            assert (await db.scalar(select(func.count()).select_from(DownloadJob))) == int(warmed) + accepted
            aliases = list((await db.execute(select(CreatorAlias))).scalars())
            assert aliases and all(a.is_current for a in aliases)
        assert len(list(root.rglob("metadata.json"))) == int(warmed) + accepted
        assert len(list(root.rglob("*.png"))) == int(warmed) + accepted
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("change", ["rename", "quota"])
async def test_upload_uses_current_account_after_preparation(tmp_path, monkeypatch, change):
    from sqlalchemy import select, update
    from app.config import settings
    from app.database import async_session, engine
    from app.models import SourceCreator, User
    from app.services.manual_upload import ManualUploadService, QuotaExceededError
    root = tmp_path / "downloads"
    monkeypatch.setattr(settings, "download_root", str(root))
    monkeypatch.setattr("app.services.import_intent.enqueue_import", _fake_enqueue)
    try:
        async with async_session() as db:
            await _clear(db)
            user = await _make_user(db, PREFIX + "snapshot", quota=None)
            user_id = user.id
            async with async_session() as other:
                values = {"username": PREFIX + "renamed"} if change == "rename" else {"upload_quota_bytes": 0}
                await other.execute(update(User).where(User.id == user.id).values(**values))
                await other.commit()
            file = _make_upload_file(tmp_path, "snapshot.png", _png_bytes())
            if change == "quota":
                with pytest.raises(QuotaExceededError):
                    await ManualUploadService().save_upload(db, user, [file], {})
                assert not list(root.rglob("*.png"))
                assert not list(root.rglob("metadata.json"))
                assert (await db.get(User, user_id, populate_existing=True)).upload_used_bytes == 0
            else:
                result = await ManualUploadService().save_upload(db, user, [file], {})
                directory = root / "manual" / ("user_" + PREFIX + "renamed") / result.work_id
                assert (directory / "metadata.json").is_file()
                assert len(list(directory.glob("*.png"))) == 1
                source = (await db.execute(select(SourceCreator).where(SourceCreator.source == "manual"))).scalar_one()
                assert source.source_creator_id == "user:" + PREFIX + "renamed"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("known_size", [True, False])
async def test_copy_accepts_exact_file_budget(tmp_path, known_size):
    from app.api.upload import _stream_to_temp
    stream = BytesIO(b"x" * 20)
    upload = UploadFile(filename="photo.png", file=stream, size=20 if known_size else None)
    saved = await _stream_to_temp(upload, tmp_path, byte_limit=20)
    assert saved.read_bytes() == b"x" * 20
    assert stream.closed
