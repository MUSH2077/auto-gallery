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
from app.schemas.admin_operations import (
    AdminOperationAccepted,
    AdminOperationSnapshotResponse,
)

logger = logging.getLogger(__name__)

from app.database import get_db

from ._routers import router

BACKUP_DIR = Path(settings.download_root) / ".backups"
ALL_BACKUP_CONTENTS = [
    "database",
    "gallerydl-config",
    "app-config",
    "download-archives",
    "library-metadata",
]


def _gallerydl_backup_ignore(_directory: str, names: list[str]) -> set[str]:
    """Exclude every runtime job overlay from durable backup traversal."""

    return {
        name
        for name in names
        if name in {"jobs", "auth-temp", "auto-gallery-secrets"}
        or name.startswith("auth-")
    }


def _gallerydl_backup_files(config_root: Path):
    for candidate in config_root.rglob("*"):
        try:
            relative = candidate.relative_to(config_root)
        except ValueError:  # pragma: no cover - rglob containment invariant
            continue
        if not relative.parts or relative.parts[0] in {
            "jobs",
            "auth-temp",
            "auto-gallery-secrets",
        }:
            continue
        if candidate.is_file() and not candidate.is_symlink():
            yield candidate


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


def _parse_db_url(url: str) -> dict:
    """Parse DATABASE_URL into pg_dump-compatible components.

    NOTE: The returned dict includes ``password`` for temporary .pgpass
    files in subprocess calls. Callers MUST NOT log or serialize this dict.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "postgres",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "autogallery",
        "password": parsed.password or "",
        "dbname": parsed.path.lstrip("/") or "autogallery",
    }


def _escape_pgpass_field(value: str) -> str:
    """Escape a value for PostgreSQL .pgpass format."""
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _pg_env_with_passfile(tmpdir: str, db_info: dict) -> dict:
    """Return subprocess env using a private, short-lived PostgreSQL passfile."""
    pgpass_path = os.path.join(tmpdir, ".pgpass")
    fields = [
        db_info["host"],
        db_info["port"],
        db_info["dbname"],
        db_info["user"],
        db_info["password"],
    ]
    # PostgreSQL requires the credential in clear text. Create the temporary
    # file with its final permissions atomically so there is never a window
    # where another local user can read it. The owning tmpdir is removed by
    # the caller immediately after pg_dump/psql completes.
    fd = os.open(pgpass_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as passfile:
        passfile.write(  # codeql[py/clear-text-storage-sensitive-data]
            ":".join(_escape_pgpass_field(str(field)) for field in fields) + "\n"
        )
    env = os.environ.copy()
    env.pop("PGPASSWORD", None)
    env["PGPASSFILE"] = pgpass_path
    return env


def _estimate_component_sizes() -> dict[str, int]:
    """Estimate the size of each backup component (bytes)."""
    sizes: dict[str, int] = {}
    # Database: rough estimate from pg_dump (we can't know exactly without running it)
    sizes["database"] = 0  # Will be measured during actual backup

    # gallery-dl config
    config_src = Path(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"))
    if config_src.exists():
        sizes["gallerydl-config"] = sum(
            f.stat().st_size for f in _gallerydl_backup_files(config_src)
        )
    else:
        sizes["gallerydl-config"] = 0

    # App config
    app_src = Path(os.environ.get("APP_CONFIG_ROOT", "/app-config"))
    if app_src.exists():
        sizes["app-config"] = sum(f.stat().st_size for f in app_src.rglob("*") if f.is_file())
    else:
        sizes["app-config"] = 0

    # Download archives
    dl_root = Path(settings.download_root)
    sizes["download-archives"] = sum(
        af.stat().st_size for af in dl_root.glob("archive-*.sqlite3") if af.is_file())

    # Library metadata
    lib_root = Path(settings.library_root)
    if lib_root.exists():
        sizes["library-metadata"] = sum(
            f.stat().st_size for f in lib_root.rglob("metadata.json") if f.is_file())
    else:
        sizes["library-metadata"] = 0

    return sizes


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
    )


_BACKUP_CANDIDATE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")


def _backup_directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_backup_root_fd() -> int:
    return os.open(BACKUP_DIR, _backup_directory_flags())


def _open_pending_backup_fd(root_fd: int, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(".pending", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
    return os.open(".pending", _backup_directory_flags(), dir_fd=root_fd)


def _clear_pending_backup_candidates(pending_fd: int) -> None:
    """Unlink prior attempt files without following unexpected entries."""

    with os.scandir(pending_fd) as entries:
        for entry in entries:
            if not entry.name.endswith(".pending"):
                continue
            if entry.is_symlink() or entry.is_file(follow_symlinks=False):
                try:
                    os.unlink(entry.name, dir_fd=pending_fd)
                except FileNotFoundError:
                    pass
    os.fsync(pending_fd)


def _create_backup_sync(
    data: dict | None = None,
    *,
    publish: bool = True,
    candidate_token: str | None = None,
):
    """Build a backup archive, optionally leaving it invisible for fencing.

    Registered TaskRun workers always build an attempt-specific candidate.
    Only the async caller may publish that candidate after its PostgreSQL
    attempt fence succeeds.  The default preserves the local maintenance/test
    helper's historical synchronous behavior.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"auto-gallery-backup_{ts}.tar.gz"
    candidate_handle = None
    candidate_pending_fd = None
    candidate_root_fd = None
    candidate_name = None
    if publish:
        filepath = BACKUP_DIR / filename
    else:
        if not candidate_token or not _BACKUP_CANDIDATE_TOKEN_RE.fullmatch(
            candidate_token
        ):
            raise ValueError("Backup candidate token is invalid")
        candidate_name = f"{filename}.{candidate_token}.pending"
        filepath = BACKUP_DIR / ".pending" / candidate_name
        candidate_root_fd = _open_backup_root_fd()
        try:
            candidate_pending_fd = _open_pending_backup_fd(
                candidate_root_fd,
                create=True,
            )
            _clear_pending_backup_candidates(candidate_pending_fd)
            candidate_fd = os.open(
                candidate_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=candidate_pending_fd,
            )
            os.fsync(candidate_pending_fd)
        except Exception:
            if candidate_pending_fd is not None:
                os.close(candidate_pending_fd)
            os.close(candidate_root_fd)
            raise
        candidate_handle = os.fdopen(candidate_fd, "w+b")

    tmpdir = None
    sizes: dict[str, int] = {}
    candidate_returned = False

    try:
        selected = (data or {}).get("contents", list(ALL_BACKUP_CONTENTS))
        selected = [c for c in selected if c in ALL_BACKUP_CONTENTS]
        if not selected:
            selected = list(ALL_BACKUP_CONTENTS)

        db_info = _parse_db_url(settings.database_url)
        tmpdir = tempfile.mkdtemp(prefix="ag-backup-")

        # 1. PostgreSQL dump
        if "database" in selected:
            dump_path = os.path.join(tmpdir, "database.dump")
            with tempfile.TemporaryDirectory(
                prefix="ag-backup-credentials-"
            ) as credential_tmpdir:
                env = _pg_env_with_passfile(credential_tmpdir, db_info)
                result = subprocess.run(
                    ["pg_dump", "-h", db_info["host"], "-p", db_info["port"], "-U", db_info["user"],
                     "-d", db_info["dbname"], "--format=custom", "--compress=3",
                     "--no-owner", "--no-acl", "-f", dump_path],
                    capture_output=True, text=True, env=env, timeout=120)
            if result.returncode != 0:
                raise RuntimeError(f"Database dump failed: {result.stderr[:500]}")
            sizes["database"] = os.path.getsize(dump_path)

        # 2. gallery-dl config
        if "gallerydl-config" in selected:
            config_src = Path(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"))
            config_dst = os.path.join(tmpdir, "gallerydl-config")
            if config_src.exists():
                shutil.copytree(
                    str(config_src),
                    config_dst,
                    symlinks=False,
                    ignore_dangling_symlinks=True,
                    ignore=lambda directory, names: (
                        shutil.ignore_patterns("*.pyc", "__pycache__", ".git")(
                            directory,
                            names,
                        )
                        | _gallerydl_backup_ignore(directory, names)
                    ),
                )

        # 3. App config
        if "app-config" in selected:
            app_config_src = Path(os.environ.get("APP_CONFIG_ROOT", "/app-config"))
            app_config_dst = os.path.join(tmpdir, "app-config")
            if app_config_src.exists():
                shutil.copytree(str(app_config_src), app_config_dst, symlinks=False, ignore_dangling_symlinks=True,
                                ignore=shutil.ignore_patterns("*.pyc", "__pycache__", ".git"))

        # 4. Download archives
        if "download-archives" in selected:
            dl_root = Path(settings.download_root)
            archives_dst = os.path.join(tmpdir, "download-archives")
            os.makedirs(archives_dst, exist_ok=True)
            for af in dl_root.glob("archive-*.sqlite3"):
                shutil.copy2(str(af), os.path.join(archives_dst, af.name))
                sizes[f"archive:{af.stem}"] = af.stat().st_size

        # 5. Library metadata
        if "library-metadata" in selected:
            lib_root = Path(settings.library_root)
            lib_dst = os.path.join(tmpdir, "library-metadata")
            if lib_root.exists():
                for mf in lib_root.rglob("metadata.json"):
                    rel = mf.relative_to(lib_root)
                    dest = Path(lib_dst) / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(mf), str(dest))

        # Manifest: restore validation treats this as the portable trust
        # boundary. Every regular payload file is independently sized and
        # hashed; manifest.json itself is intentionally excluded to avoid a
        # circular digest.
        entries: dict[str, dict[str, int | str]] = {}
        for payload_path in sorted(Path(tmpdir).rglob("*")):
            if payload_path.is_symlink() or not payload_path.is_file():
                continue
            digest = hashlib.sha256()
            with payload_path.open("rb") as payload_file:
                while block := payload_file.read(1024 * 1024):
                    digest.update(block)
            relative = payload_path.relative_to(tmpdir).as_posix()
            entries[relative] = {
                "size": payload_path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        manifest = {
            "created_at": ts,
            "version": "0.3.0",
            "contents": selected,
            "restorable": "database" in selected,
            "component_sizes": {k: v for k, v in sizes.items()},
            "entries": entries,
            "total_uncompressed_bytes": sum(
                int(entry["size"]) for entry in entries.values()
            ),
        }
        with open(os.path.join(tmpdir, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)

        # Create tar.gz
        if candidate_handle is None:
            with tarfile.open(filepath, "w:gz") as tar:
                for item in os.listdir(tmpdir):
                    tar.add(os.path.join(tmpdir, item), arcname=item)
            file_size = os.path.getsize(filepath)
        else:
            with tarfile.open(fileobj=candidate_handle, mode="w:gz") as tar:
                for item in os.listdir(tmpdir):
                    tar.add(os.path.join(tmpdir, item), arcname=item)
            candidate_handle.flush()
            os.fsync(candidate_handle.fileno())
            file_size = os.fstat(candidate_handle.fileno()).st_size
        logger.info("Backup created: %s (%.1f MB) contents=%s", filename, file_size / 1024 / 1024, selected)

        result = {
            "status": "ok",
            "filename": filename,
            "size_bytes": file_size,
            "size_mb": round(file_size / 1024 / 1024, 1),
            "contents": selected,
            "restorable": "database" in selected,
            "component_sizes": {k: round(v / 1024, 1) for k, v in sizes.items()},
        }
        if publish:
            # Keep last 10 visible backups. Candidate archives never prune.
            _prune_backup_files(preserve=filepath)
            return result
        candidate_returned = True
        return {**result, "_candidate_path": str(filepath)}

    finally:
        if candidate_handle is not None:
            candidate_handle.close()
        if (
            not candidate_returned
            and candidate_pending_fd is not None
            and candidate_name is not None
        ):
            try:
                os.unlink(candidate_name, dir_fd=candidate_pending_fd)
            except FileNotFoundError:
                pass
            os.fsync(candidate_pending_fd)
        if candidate_pending_fd is not None:
            os.close(candidate_pending_fd)
        if candidate_root_fd is not None:
            os.close(candidate_root_fd)
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def publish_backup_candidate(staged: dict, *, prune: bool = True) -> dict:
    """Atomically expose one already-built candidate and then prune history."""

    candidate_value = staged.get("_candidate_path")
    filename = str(staged.get("filename") or "")
    if not isinstance(candidate_value, str) or not BACKUP_NAME_PATTERN.fullmatch(
        filename
    ):
        raise ValueError("Backup candidate metadata is invalid")
    candidate = Path(candidate_value)
    candidate_name = candidate.name
    expected_prefix = f"{filename}."
    if (
        candidate != BACKUP_DIR / ".pending" / candidate_name
        or not candidate_name.startswith(expected_prefix)
        or not candidate_name.endswith(".pending")
        or not _BACKUP_CANDIDATE_TOKEN_RE.fullmatch(
            candidate_name[len(expected_prefix) : -len(".pending")]
        )
    ):
        raise ValueError("Backup candidate escaped its pending directory")
    root_fd = _open_backup_root_fd()
    try:
        pending_fd = _open_pending_backup_fd(root_fd, create=False)
        try:
            candidate_stat = os.stat(
                candidate_name,
                dir_fd=pending_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(candidate_stat.st_mode):
                raise RuntimeError("Backup candidate is missing")
            os.replace(
                candidate_name,
                filename,
                src_dir_fd=pending_fd,
                dst_dir_fd=root_fd,
            )
            os.fsync(pending_fd)
            os.fsync(root_fd)
        finally:
            os.close(pending_fd)
    finally:
        os.close(root_fd)
    target = BACKUP_DIR / filename

    if prune:
        _prune_backup_files(preserve=target)
    return {key: value for key, value in staged.items() if not key.startswith("_")}


def prune_published_backup(filename: str) -> None:
    """Prune only after the publishing TaskRun is durably complete."""

    if not BACKUP_NAME_PATTERN.fullmatch(filename):
        raise ValueError("Published backup filename is invalid")
    _prune_backup_files(preserve=BACKUP_DIR.resolve() / filename)


BACKUP_NAME_PATTERN = re.compile(r"auto-gallery-backup_[0-9]{8}_[0-9]{6}\.tar\.gz")


def _list_backup_files() -> list[Path]:
    """Return only regular, non-symlink backup files contained by BACKUP_DIR."""
    root = BACKUP_DIR.resolve()
    files: list[Path] = []
    for candidate in BACKUP_DIR.glob("auto-gallery-backup_*.tar.gz"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if BACKUP_NAME_PATTERN.fullmatch(resolved.name):
            files.append(resolved)
    return sorted(files)


def _prune_backup_files(*, preserve: Path, keep: int = 10) -> None:
    """Prune history without ever deleting the archive just published."""

    existing = _list_backup_files()
    excess = max(0, len(existing) - keep)
    preserved = preserve.resolve(strict=False)
    for old in [path for path in existing if path != preserved][:excess]:
        old.unlink()


def _validate_backup_filename(filename: str) -> Path:
    """Select an existing server-discovered backup by its strict basename."""
    if not BACKUP_NAME_PATTERN.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    for target in _list_backup_files():
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
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
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
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = _validate_backup_filename(filename)
    target.unlink()
    return {"status": "ok", "message": f"Deleted {filename}"}


@router.get("/backup/list")
async def list_backups():
    """List available backup files."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
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
