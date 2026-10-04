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
