"""Backup and restore."""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

from app.auth import RequirePermission
from app.database import async_session, get_db
from app.services.operations import get_operation_status, set_operation_status

from ._routers import router

BACKUP_DIR = Path(settings.download_root) / ".backups"
ALL_BACKUP_CONTENTS = [
    "database",
    "gallerydl-config",
    "app-config",
    "download-archives",
    "library-metadata",
]

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
        sizes["gallerydl-config"] = sum(f.stat().st_size for f in config_src.rglob("*") if f.is_file())
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


@router.get("/backup/estimate")
async def estimate_backup_sizes():
    """Return estimated sizes for each backup component.

    Offloaded to a thread — _estimate_component_sizes walks lib_root for
    metadata.json files, a full-tree traversal that must not block the loop.
    """
    sizes = await asyncio.to_thread(_estimate_component_sizes)
    return {"components": {k: round(v / 1024, 1) for k, v in sizes.items()}}


@router.post("/backup")
async def create_backup(data: dict | None = None):
    """Create a system backup with optional content selection.

    Body (optional): {contents: ["database", "gallerydl-config", ...]}
    Defaults to all components if not specified.

    The whole body is blocking (pg_dump, copytree, rglob, tar.gz) and is run
    off the event loop so a backup doesn't freeze the gallery for everyone.
    """
    return await asyncio.to_thread(_create_backup_sync, data)


def _create_backup_sync(data: dict | None = None):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"auto-gallery-backup_{ts}.tar.gz"
    filepath = BACKUP_DIR / filename

    selected = (data or {}).get("contents", list(ALL_BACKUP_CONTENTS))
    selected = [c for c in selected if c in ALL_BACKUP_CONTENTS]
    if not selected:
        selected = list(ALL_BACKUP_CONTENTS)

    db_info = _parse_db_url(settings.database_url)
    tmpdir = tempfile.mkdtemp(prefix="ag-backup-")
    sizes: dict[str, int] = {}

    try:
        # 1. PostgreSQL dump
        if "database" in selected:
            dump_path = os.path.join(tmpdir, "database.sql")
            env = _pg_env_with_passfile(tmpdir, db_info)
            result = subprocess.run(
                ["pg_dump", "-h", db_info["host"], "-p", db_info["port"], "-U", db_info["user"],
                 "-d", db_info["dbname"], "--no-owner", "--no-acl", "-f", dump_path],
                capture_output=True, text=True, env=env, timeout=120)
            if result.returncode != 0:
                raise RuntimeError(f"Database dump failed: {result.stderr[:500]}")
            sizes["database"] = os.path.getsize(dump_path)

        # 2. gallery-dl config
        if "gallerydl-config" in selected:
            config_src = Path(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"))
            config_dst = os.path.join(tmpdir, "gallerydl-config")
            if config_src.exists():
                shutil.copytree(str(config_src), config_dst, symlinks=False, ignore_dangling_symlinks=True,
                                ignore=shutil.ignore_patterns("*.pyc", "__pycache__", ".git"))

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

        # Manifest
        manifest = {
            "created_at": ts,
            "version": "0.2.0",
            "contents": selected,
            "component_sizes": {k: v for k, v in sizes.items()},
        }
        with open(os.path.join(tmpdir, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)

        # Create tar.gz
        with tarfile.open(filepath, "w:gz") as tar:
            for item in os.listdir(tmpdir):
                tar.add(os.path.join(tmpdir, item), arcname=item)

        file_size = os.path.getsize(filepath)
        logger.info("Backup created: %s (%.1f MB) contents=%s", filename, file_size / 1024 / 1024, selected)

        # Keep last 10 backups
        existing = _list_backup_files()
        for old in existing[:-10]:
            old.unlink()

        return {
            "status": "ok",
            "filename": filename,
            "size_bytes": file_size,
            "size_mb": round(file_size / 1024 / 1024, 1),
            "contents": selected,
            "component_sizes": {k: round(v / 1024, 1) for k, v in sizes.items()},
        }

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# restore_backup is defined below with _safe_extract_tar, .pgpass handling,
# and confirm=DELETE-EVERYTHING guard.  See the second definition in this file.


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


def _validate_backup_filename(filename: str) -> Path:
    """Select an existing server-discovered backup by its strict basename."""
    if not BACKUP_NAME_PATTERN.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    for target in _list_backup_files():
        if target.name == filename:
            return target
    raise HTTPException(status_code=404, detail="Backup not found")


@router.get("/backup/download")
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
        result.append({
            "filename": f.name,
            "size_mb": round(stat.st_size / 1024 / 1024, 1),
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return {"backups": result}


def _safe_extract_tar(tar_path: str, dest_dir: str) -> None:
    """Extract a tar.gz file, validating all member paths stay within dest_dir."""
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            member_path = os.path.join(dest_dir, member.name)
            resolved = os.path.realpath(member_path)
            resolved_dest = os.path.realpath(dest_dir)
            if os.path.commonpath([resolved, resolved_dest]) != resolved_dest:
                logger.warning("Rejected tar member outside dest: %s -> %s", member.name, resolved)
                continue
            if member.isdir():
                os.makedirs(resolved, exist_ok=True)
            elif member.isfile():
                os.makedirs(os.path.dirname(resolved), exist_ok=True)
                src = tar.extractfile(member)
                if src is None:
                    continue
                with src, open(resolved, "wb") as dst:
                    dst.write(src.read())


def _do_restore(upload_path: str, tmpdir: str) -> dict:
    """Blocking restore work (tar extract, psql subprocesses, config copy).
    Runs in a thread so a restore doesn't freeze the event loop."""
    extract_dir = os.path.join(tmpdir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)
    _safe_extract_tar(upload_path, extract_dir)

    manifest_path = os.path.join(extract_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return {"status": "error", "message": "Invalid backup: no manifest.json found"}

    results = []

    # 1. Restore database
    dump_path = os.path.join(extract_dir, "database.sql")
    if os.path.exists(dump_path):
        db = _parse_db_url(settings.database_url)
        env = _pg_env_with_passfile(tmpdir, db)
        result = subprocess.run(
            ["psql", "-h", db["host"], "-p", db["port"], "-U", db["user"], "-d", db["dbname"],
             "-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        if result.returncode != 0:
            logger.error("Schema reset failed: %s", result.stderr)
            results.append({"item": "database", "status": "error", "error": result.stderr[:200]})
        else:
            result = subprocess.run(
                ["psql", "-h", db["host"], "-p", db["port"], "-U", db["user"], "-d", db["dbname"],
                 "-f", dump_path],
                capture_output=True, text=True, env=env, timeout=120,
            )
            if result.returncode == 0:
                results.append({"item": "database", "status": "restored"})
                logger.info("Database restored from backup")
            else:
                logger.error("DB restore failed: %s", result.stderr)
                results.append({"item": "database", "status": "error", "error": result.stderr[:200]})

    # 2. Restore config files
    for src_name, dst_env in [("gallerydl-config", "GALLERYDL_CONFIG_ROOT"),
                               ("app-config", "APP_CONFIG_ROOT")]:
        src = os.path.join(extract_dir, src_name)
        if os.path.exists(src):
            dst = os.environ.get(dst_env, f"/{src_name}")
            if os.path.exists(dst):
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, symlinks=False, ignore_dangling_symlinks=True)
            results.append({"item": src_name, "status": "restored"})

    # 3. Restore download archives
    archives_src = os.path.join(extract_dir, "download-archives")
    if os.path.exists(archives_src):
        dl_root = str(settings.download_root)
        for af in os.listdir(archives_src):
            shutil.copy2(os.path.join(archives_src, af), os.path.join(dl_root, af))
        results.append({"item": "download-archives", "status": "restored"})

    return {"status": "ok", "results": results}


@router.post("/backup/restore")
async def restore_backup(file: UploadFile = File(...), confirm: str = ""):
    """Restore system from a backup file. THIS IS DESTRUCTIVE — replaces current data.

    Requires ``?confirm=DELETE-EVERYTHING`` to prevent accidental invocation.
    """
    if confirm != "DELETE-EVERYTHING":
        return {"status": "error", "message": "Add ?confirm=DELETE-EVERYTHING to proceed with restore"}
    if not file.filename or not file.filename.endswith(".tar.gz"):
        return {"status": "error", "message": "Invalid file: must be a .tar.gz backup"}

    logger.warning("Backup restore initiated (confirm=%s, file=%s)", confirm, file.filename)

    tmpdir = tempfile.mkdtemp(prefix="ag-restore-")
    try:
        # Save uploaded file with a fixed name (ignore user-supplied filename for safety)
        upload_path = os.path.join(tmpdir, "upload.tar.gz")
        with open(upload_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # Extract + DB restore + config copy are all blocking (tar, psql
        # subprocesses, copytree) — run off the event loop.
        return await asyncio.to_thread(_do_restore, upload_path, tmpdir)

    except Exception:
        logger.exception("Restore failed")
        return {
            "status": "error",
            "message": "Restore failed. Check the backend logs for the request details.",
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
