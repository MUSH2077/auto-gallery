"""Manual upload API — thin route: parse multipart, stream to temp files,
hand off to ManualUploadService. All validation/business logic lives in the
service (app/services/manual_upload.py)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DBSession
from app.auth import RequirePermission
from app.config import settings
from app.models.user import User
from app.services.manual_upload import ManualUploadService, QuotaExceededError, UploadFileInput

_require_upload = RequirePermission("upload")

router = APIRouter(dependencies=[_require_upload])


class UploadResponse(BaseModel):
    work_id: str
    download_job_id: str
    import_job_id: str | None
    used_bytes: int
    quota_bytes: int | None


async def _stream_to_temp(upload: UploadFile, tmp_dir: Path) -> Path:
    """Stream an UploadFile to a temp file under DOWNLOAD_ROOT.

    Placing the temp file on the same filesystem as DOWNLOAD_ROOT lets the
    service move it into final storage with an atomic os.replace() instead
    of a cross-device copy.
    """
    suffix = Path(upload.filename or "").suffix
    fd, tmp_name = tempfile.mkstemp(suffix=suffix, dir=str(tmp_dir))
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    finally:
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
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")

    tmp_dir = Path(settings.download_root) / ".upload-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    temp_inputs: list[UploadFileInput] = []
    try:
        for f in files:
            tmp_path = await _stream_to_temp(f, tmp_dir)
            temp_inputs.append(UploadFileInput(temp_path=tmp_path, original_name=f.filename or "upload"))

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
        except QuotaExceededError as e:
            raise HTTPException(status_code=413, detail=str(e))
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
