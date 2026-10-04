"""Manual upload API — thin route: parse multipart, stream to temp files,
hand off to ManualUploadService. Transport budgets and temporary files belong
to this route; persistent validation and accounting belong to the service."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DBSession
from app.auth import RequirePermission
from app.config import settings
from app.models.user import User
from app.services.heavy_io import HeavyIOUnavailable, storage_write_barrier
from app.services.manual_upload import (
    ManualUploadService, QuotaExceededError, UploadFileInput,
    MAX_UPLOAD_FILES, MAX_FILE_SIZE_BYTES, MAX_BATCH_SIZE_BYTES, MAX_REQUEST_SIZE_BYTES,
)

_require_upload = RequirePermission("upload")

class UploadRequest(Request):
    def form(self, **kwargs):
        return super().form(**{**kwargs, "max_files": MAX_UPLOAD_FILES, "max_fields": 10, "max_part_size": 64 * 1024})


class UploadRoute(APIRoute):
    """Count received bytes before FastAPI's multipart parser runs."""
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            length = request.headers.get("content-length")
            if length and length.isdecimal() and int(length) > MAX_REQUEST_SIZE_BYTES:
                raise HTTPException(status_code=413, detail="Upload request exceeds the byte limit")
            received = 0

            async def receive():
                nonlocal received
                message = await request.receive()
                received += len(message.get("body", b""))
                if received > MAX_REQUEST_SIZE_BYTES:
                    raise HTTPException(status_code=413, detail="Upload request exceeds the byte limit")
                return message

            return await handler(UploadRequest(request.scope, receive))
        return bounded


router = APIRouter(dependencies=[_require_upload], route_class=UploadRoute)


class UploadResponse(BaseModel):
    work_id: str
    download_job_id: str
    import_job_id: str | None
    used_bytes: int
    quota_bytes: int | None


async def _stream_to_temp(upload: UploadFile, tmp_dir: Path, *, byte_limit: int = MAX_FILE_SIZE_BYTES) -> Path:
    """Stream an UploadFile to a temp file under DOWNLOAD_ROOT.

    Placing the temp file on the same filesystem as DOWNLOAD_ROOT lets the
    service move it into final storage with an atomic os.replace() instead
    of a cross-device copy.
    """
    suffix = Path(upload.filename or "").suffix
    fd, tmp_name = tempfile.mkstemp(suffix=suffix, dir=str(tmp_dir))
    tmp_path = Path(tmp_name)
    delivered = False
    try:
        with open(fd, "wb") as out:
            if upload.size is not None and upload.size > byte_limit:
                raise QuotaExceededError("Upload exceeds the file or remaining batch budget")
            copied = 0
            while True:
                chunk = await upload.read(min(1024 * 1024, byte_limit - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > byte_limit:
                    raise QuotaExceededError("Upload exceeds the file or remaining batch budget")
                out.write(chunk)
        await upload.close()
        delivered = True
    finally:
        if not delivered:
            tmp_path.unlink(missing_ok=True)
        await upload.close()
    return tmp_path


@router.post("", response_model=UploadResponse)
async def upload_files(
    files: list[UploadFile] = File(...),
    title: str | None = Form(None),
    tags: str | None = Form(None),
    is_nsfw: bool = Form(False),
    creator_id: str | None = Form(None),
    db: AsyncSession = DBSession,
    user: User = _require_upload,
):
    try:
        async with storage_write_barrier():
            return await _receive_upload_files(files, title, tags, is_nsfw, creator_id, db, user)
    except HeavyIOUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "maintenance_in_progress",
                "message": "Storage maintenance is in progress; retry the upload later",
            },
            headers={"Retry-After": "10"},
        ) from exc


async def _receive_upload_files(
    files: list[UploadFile], title: str | None, tags: str | None,
    is_nsfw: bool, creator_id: str | None, db: AsyncSession, user: User,
) -> UploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")

    tmp_dir = Path(settings.download_root) / ".upload-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    temp_inputs: list[UploadFileInput] = []
    try:
        remaining = MAX_BATCH_SIZE_BYTES
        if user.upload_quota_bytes is not None:
            remaining = min(remaining, max(0, user.upload_quota_bytes - user.upload_used_bytes))
        for f in files:
            tmp_path = await _stream_to_temp(f, tmp_dir, byte_limit=min(MAX_FILE_SIZE_BYTES, remaining))
            temp_inputs.append(UploadFileInput(temp_path=tmp_path, original_name=f.filename or "upload"))
            remaining -= tmp_path.stat().st_size

        try:
            result = await ManualUploadService().save_upload(
                db,
                user,
                temp_inputs,
                {
                    "title": title,
                    "tags": tags,
                    "is_nsfw": is_nsfw,
                    "creator_id": creator_id,
                },
            )
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        return UploadResponse(
            work_id=result.work_id,
            download_job_id=result.download_job_id,
            import_job_id=result.import_job_id,
            used_bytes=result.used_bytes,
            quota_bytes=result.quota_bytes,
        )
    except QuotaExceededError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    finally:
        # Files the service successfully moved into DOWNLOAD_ROOT no longer
        # exist at temp_path (os.replace already relocated them); this only
        # cleans up leftovers from a failure that happened before the move.
        for ti in temp_inputs:
            try:
                if ti.temp_path.exists():
                    ti.temp_path.unlink()
            except OSError:
                pass
