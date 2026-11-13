"""Backup creation and non-destructive offline-restore staging."""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import backup as backup_store
from app.services.backup import (
    _gallerydl_backup_ignore,
    _gallerydl_backup_files,
    _parse_db_url,
    _escape_pgpass_field,
    _pg_env_with_passfile,
    _estimate_component_sizes,
    _backup_directory_flags,
    _open_backup_root_fd,
    _open_pending_backup_fd,
    _clear_pending_backup_candidates,
    _create_backup_sync,
    publish_backup_candidate,
    prune_published_backup,
    _list_backup_files,
    _prune_backup_files,
    BACKUP_DIR,
    ALL_BACKUP_CONTENTS,
    _BACKUP_CANDIDATE_TOKEN_RE,
    BACKUP_NAME_PATTERN,
)
from app.schemas.admin_operations import (
    AdminOperationAccepted,
    AdminOperationSnapshotResponse,
)

logger = logging.getLogger(__name__)

from app.database import get_db

from ._routers import router







class RestoreUploadCreateRequest(BaseModel):
    filename: str
    size_bytes: int
    sha256: str
    chunk_size: int
    total_chunks: int


class RestoreUploadSessionResponse(BaseModel):
    upload_id: str
    filename: str
    size_bytes: int
    sha256: str
    chunk_size: int
    total_chunks: int
    received_chunks: int
    received_bytes: int
    next_chunk: int
    state: str
    validation_task_id: str | None = None
    request_id: str | None = None
    created_at: str
    updated_at: str


class RestoreUploadCreatedResponse(RestoreUploadSessionResponse):
    upload_token: str


class RestoreChunkResponse(RestoreUploadSessionResponse):
    idempotent: bool


class RestoreReceiptResponse(BaseModel):
    request_id: str
    status: str
    phase: str
    started_at: str | None = None
    completed_at: str | None = None
    rollback_performed: bool | None = None
    rollback_status: str | None = None
    rollback_components: dict[str, dict[str, str]] | None = None
    diagnostic: str | None = None
    error: str | None = None
    rollback_command: str | None = None










@router.post(
    "/backup/estimate",
    status_code=202,
    response_model=AdminOperationAccepted,
)
async def estimate_backup_sizes():
    """Start a backup estimate without traversing storage in this request."""
    from app.services.operations import start_admin_operation

    return await start_admin_operation(
        operation_type="admin-backup-estimate",
        scope_key="backup:estimate:active",
        title="Backup estimate",
        entity="backup-estimate",
        options={},
        queue_name="maintenance",
    )


@router.get(
    "/backup/estimate/latest",
    response_model=AdminOperationSnapshotResponse,
)
async def latest_backup_estimate(db: AsyncSession = Depends(get_db)):
    """Read the latest successful backup estimate from PostgreSQL."""
    from app.services.operations import latest_successful_admin_operation

    return await latest_successful_admin_operation(
        db,
        operation_type="admin-backup-estimate",
        scope_key="backup:estimate:active",
    )


@router.post(
    "/backup",
    status_code=202,
    response_model=AdminOperationAccepted,
)
async def create_backup(data: dict | None = None):
    """Start backup creation and return its durable TaskRun immediately."""
    from app.services.operations import start_admin_operation

    selected = (data or {}).get("contents", list(ALL_BACKUP_CONTENTS))
    return await start_admin_operation(
        operation_type="admin-backup-create",
        scope_key="backup:create:active",
        title="Create backup",
        entity="backup",
        options={"contents": selected},
        queue_name="maintenance",
        job_timeout=3600,
    )


@router.get("/backup/latest", response_model=AdminOperationSnapshotResponse)
async def latest_backup(db: AsyncSession = Depends(get_db)):
    """Read the latest successful backup-creation result."""
    from app.services.operations import latest_successful_admin_operation

    return await latest_successful_admin_operation(
        db,
        operation_type="admin-backup-create",
        scope_key="backup:create:active",
        include_retryable=True,
    )
























def _validate_backup_filename(filename: str, *, backup_root: Path | None = None) -> Path:
    """Select an existing server-discovered backup by its strict basename."""
    if not BACKUP_NAME_PATTERN.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    for target in _list_backup_files(backup_root):
        if target.name == filename:
            return target
    raise HTTPException(status_code=404, detail="Backup not found")


@router.get(
    "/backup/download",
    responses={
        200: {
            "content": {
                "application/gzip": {"schema": {"type": "string", "format": "binary"}},
                "application/json": {"schema": {"type": "object", "additionalProperties": True}},
            },
            "description": "Backup archive bytes, or a JSON diagnostic when no backup exists.",
        }
    },
)
async def download_backup(filename: str | None = None):
    """Download a backup file. If filename not specified, returns the latest."""
    backup_store.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    existing = _list_backup_files()
    if not existing:
        return {"status": "error", "message": "No backups available"}
    target = _validate_backup_filename(filename) if filename else existing[-1]
    return FileResponse(
        str(target), media_type="application/gzip", filename=target.name,
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'})


@router.delete("/backup/{filename}")
async def delete_backup(filename: str):
    """Delete a specific backup file."""
    backup_store.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = _validate_backup_filename(filename)
    target.unlink()
    return {"status": "ok", "message": f"Deleted {filename}"}


@router.get("/backup/list")
async def list_backups():
    """List available backup files."""
    backup_store.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    existing = list(reversed(_list_backup_files()))
    result = []
    for f in existing:
        stat = f.stat()
        restorable = False
        try:
            with tarfile.open(f, "r:gz") as archive:
                member = archive.getmember("manifest.json")
                if member.isreg() and 0 < member.size <= 4 * 1024 * 1024:
                    source = archive.extractfile(member)
                    manifest = json.loads(source.read(member.size + 1)) if source else {}
                    restorable = bool(
                        isinstance(manifest, dict)
                        and "database" in (manifest.get("contents") or [])
                    )
        except (KeyError, OSError, tarfile.TarError, json.JSONDecodeError):
            restorable = False
        result.append({
            "filename": f.name,
            "size_mb": round(stat.st_size / 1024 / 1024, 1),
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "restorable": restorable,
        })
    return {"backups": result}


def _restore_http_error(exc: Exception) -> HTTPException:
    from app.services.offline_restore import (
        RestoreConflict,
        RestoreForbidden,
        RestoreValidationError,
    )

    if isinstance(exc, RestoreForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, RestoreConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RestoreValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("Offline restore staging failed", exc_info=exc)
    return HTTPException(
        status_code=500,
        detail="Restore staging failed. Check the backend logs for details.",
    )


@router.post(
    "/backup/restore/uploads",
    status_code=201,
    response_model=RestoreUploadCreatedResponse,
)
async def create_restore_upload(data: RestoreUploadCreateRequest):
    """Create a resumable upload outside every live configuration directory."""

    from app.services.offline_restore import create_upload_session, staging_root

    try:
        return create_upload_session(root=staging_root(), **data.model_dump())
    except Exception as exc:
        raise _restore_http_error(exc) from exc


@router.get(
    "/backup/restore/uploads/{upload_id}",
    response_model=RestoreUploadSessionResponse,
)
async def get_restore_upload(
    upload_id: str,
    restore_token: str = Header(alias="X-Restore-Token"),
):
    """Resume one capability-isolated upload without enumerating its siblings."""

    from app.services.offline_restore import get_upload_session, staging_root

    try:
        return get_upload_session(
            root=staging_root(), upload_id=upload_id, token=restore_token
        )
    except Exception as exc:
        raise _restore_http_error(exc) from exc


@router.put(
    "/backup/restore/uploads/{upload_id}/chunks/{chunk_index}",
    response_model=RestoreChunkResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def upload_restore_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request,
    restore_token: str = Header(alias="X-Restore-Token"),
    chunk_sha256: str = Header(alias="X-Chunk-SHA256"),
):
    """Persist exactly one ordered chunk; identical retries are idempotent."""

    from app.services.offline_restore import (
        MAX_CHUNK_SIZE,
        put_upload_chunk_file,
        staging_root,
    )

    temporary_path: Path | None = None
    try:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="Restore chunk Content-Length is invalid",
                ) from exc
            if declared_length < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Restore chunk Content-Length is invalid",
                )
            if declared_length > MAX_CHUNK_SIZE:
                raise HTTPException(status_code=413, detail="Restore chunk is too large")

        root = staging_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".restore-chunk-ingress-",
            suffix=".tmp",
            dir=root,
        )
        temporary_path = Path(temporary_name)
        total = 0
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as ingress:
            async for block in request.stream():
                if not block:
                    continue
                total += len(block)
                if total > MAX_CHUNK_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="Restore chunk is too large",
                    )
                digest.update(block)
                ingress.write(block)
            ingress.flush()
            os.fsync(ingress.fileno())

        return await asyncio.to_thread(
            put_upload_chunk_file,
            root=root,
            upload_id=upload_id,
            token=restore_token,
            index=chunk_index,
            source_path=temporary_path,
            size=total,
            actual_sha256=digest.hexdigest(),
            sha256=chunk_sha256,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _restore_http_error(exc) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@router.post(
    "/backup/restore/uploads/{upload_id}/validate",
    status_code=202,
    response_model=AdminOperationAccepted,
)
async def validate_restore_upload(
    upload_id: str,
    restore_token: str = Header(alias="X-Restore-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Start archive validation; this action cannot execute a host restore."""

    from app.services.offline_restore import (
        RestoreConflict,
        get_upload_session,
        seal_upload_for_validation,
        staging_root,
    )
    from app.models import TaskRun
    from app.services.operations import (
        ADMIN_DISPATCH_META_KEY,
        prepare_admin_operation,
        publish_admin_operation,
    )
    from sqlalchemy import select

    scope_key = f"restore:validate:{upload_id}"

    async def active_scope_task():
        scope_text = TaskRun.meta[ADMIN_DISPATCH_META_KEY]["scope_key"].astext
        return (
            await db.execute(
                select(TaskRun)
                .where(
                    TaskRun.kind == "admin",
                    TaskRun.operation_type == "admin-restore-validate",
                    TaskRun.status.in_({"enqueued", "running", "paused", "recovering"}),
                    scope_text == scope_key,
                )
                .order_by(TaskRun.created_at.desc(), TaskRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    def accepted(task: TaskRun) -> dict[str, str]:
        return {
            "task_id": str(task.id),
            "job_id": str(task.rq_job_id),
            "status": "enqueued",
            "operation_type": "admin-restore-validate",
        }

    try:
        session = get_upload_session(
            root=staging_root(), upload_id=upload_id, token=restore_token
        )
        attached_id = session.get("validation_task_id")
        attached = None
        if attached_id:
            try:
                attached = await db.get(TaskRun, UUID(str(attached_id)))
            except ValueError:
                attached = None
        if attached is not None:
            dispatch = (attached.meta or {}).get(ADMIN_DISPATCH_META_KEY, {})
            options = dispatch.get("options", {}) if isinstance(dispatch, dict) else {}
            if (
                attached.operation_type != "admin-restore-validate"
                or not isinstance(options, dict)
                or options.get("upload_id") != upload_id
                or not attached.rq_job_id
            ):
                raise RestoreConflict("Restore validation TaskRun is unavailable")
            response = accepted(attached)
            attempt = attached.attempts
            await db.rollback()
            await publish_admin_operation(response["task_id"], attempt)
            return response

        active = await active_scope_task()
        if active is not None:
            response = accepted(active)
            attempt = active.attempts
            seal_upload_for_validation(
                root=staging_root(),
                upload_id=upload_id,
                token=restore_token,
                task_id=str(active.id),
                replace_task_id=str(attached_id) if attached_id else None,
            )
            await db.rollback()
            await publish_admin_operation(response["task_id"], attempt)
            return response

        recoverable_states = {"uploaded", "validating", "validation_failed"}
        if attached_id and attached is None:
            recoverable_states.add("ready")
        if session["state"] not in recoverable_states:
            raise RestoreConflict("Restore upload is not complete")
        prepared = await prepare_admin_operation(
            db,
            operation_type="admin-restore-validate",
            scope_key=scope_key,
            title="Validate restore upload",
            entity="restore-upload",
            options={"upload_id": upload_id},
            queue_name="maintenance",
            job_timeout=3600,
        )
        seal_upload_for_validation(
            root=staging_root(),
            upload_id=upload_id,
            token=restore_token,
            task_id=str(prepared.task.id),
            replace_task_id=str(attached_id) if attached_id else None,
        )
        await db.commit()
        await publish_admin_operation(
            prepared.task.id,
            prepared.attempt,
        )
        return accepted(prepared.task)
    except Exception as exc:
        await db.rollback()
        if isinstance(exc, HTTPException):
            raise
        raise _restore_http_error(exc) from exc


@router.get(
    "/backup/restore/uploads/{upload_id}/validation/latest",
    response_model=AdminOperationSnapshotResponse,
)
async def latest_restore_validation(
    upload_id: str,
    restore_token: str = Header(alias="X-Restore-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Load the durable validator state after a frontend remount."""

    from app.services.offline_restore import get_upload_session, staging_root
    from app.services.operations import latest_successful_admin_operation

    try:
        get_upload_session(root=staging_root(), upload_id=upload_id, token=restore_token)
        return await latest_successful_admin_operation(
            db,
            operation_type="admin-restore-validate",
            scope_key=f"restore:validate:{upload_id}",
        )
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise _restore_http_error(exc) from exc


@router.get(
    "/backup/restore/receipts/{request_id}",
    response_model=RestoreReceiptResponse,
)
async def get_restore_receipt(
    request_id: str,
    restore_token: str = Header(alias="X-Restore-Token"),
):
    """Read the external immutable host receipt without using TaskRun state."""

    from app.services.offline_restore import (
        read_restore_receipt,
        receipts_root,
        staging_root,
    )

    try:
        return read_restore_receipt(
            staging=staging_root(),
            receipts=receipts_root(),
            request_id=request_id,
            token=restore_token,
        )
    except Exception as exc:
        raise _restore_http_error(exc) from exc
