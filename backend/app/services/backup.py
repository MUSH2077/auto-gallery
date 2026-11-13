"""Backup archive construction and publication, independent of HTTP/RQ."""
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
from datetime import datetime, timezone
from pathlib import Path
from app.config import settings

logger = logging.getLogger(__name__)

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


def _list_backup_files(root: Path | None = None) -> list[Path]:
    """Return only regular, non-symlink backup files contained by BACKUP_DIR."""
    root = (root or BACKUP_DIR).resolve()
    files: list[Path] = []
    for candidate in root.glob("auto-gallery-backup_*.tar.gz"):
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
