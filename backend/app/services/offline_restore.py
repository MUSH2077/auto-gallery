"""Filesystem-only staging and validation for an offline destructive restore.

This module deliberately has no database, Redis, or Docker dependencies.  The
application may validate and publish a host handoff, but it can never replace
the live services it is currently using.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import tarfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from uuid import UUID, uuid4


DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
MIN_CHUNK_SIZE = 16 * 1024
MAX_CHUNK_SIZE = 16 * 1024 * 1024
MAX_ARCHIVE_SIZE = 64 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 250_000
MAX_MANIFEST_SIZE = 4 * 1024 * 1024
MANIFEST_VERSION = "0.3.0"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ARCHIVE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.tar\.gz")
_CHUNK_FILE_RE = re.compile(r"(?P<index>[0-9]{8})\.part(?P<temporary>\.tmp)?")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_COMPONENTS = frozenset(
    {
        "database",
        "gallerydl-config",
        "app-config",
        "download-archives",
        "library-metadata",
    }
)
_DATABASE_PAYLOADS = frozenset({"database.dump", "database.sql"})


class RestoreError(RuntimeError):
    """Base public staged-restore error."""


class RestoreValidationError(RestoreError):
    """The upload or archive is malformed or unsafe."""


class RestoreConflict(RestoreError):
    """The requested write conflicts with durable staging state."""


class RestoreForbidden(RestoreError):
    """The caller does not possess the per-upload capability."""


def staging_root() -> Path:
    return Path(os.environ.get("RESTORE_STAGING_ROOT", "/restore-staging"))


def receipts_root() -> Path:
    return Path(os.environ.get("RESTORE_RECEIPTS_ROOT", "/restore-receipts"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> None:
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
    fd = os.open(temp, flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, mode)
        directory_fd = os.open(path.parent, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.lexists(temp):
            temp.unlink()


def _session_dir(root: Path, upload_id: str) -> Path:
    try:
        normalized = str(UUID(str(upload_id)))
    except (TypeError, ValueError) as exc:
        raise RestoreValidationError("Invalid restore upload id") from exc
    if normalized != str(upload_id):
        raise RestoreValidationError("Invalid restore upload id")
    resolved_root = root.resolve()
    candidate = (resolved_root / normalized).resolve(strict=False)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise RestoreValidationError("Restore staging path escaped its root") from exc
    return candidate


@contextmanager
def _session_lock(session: Path) -> Iterator[None]:
    lock_path = session / ".lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_metadata(session: Path) -> dict[str, Any]:
    path = session / "metadata.json"
    try:
        if path.is_symlink() or not path.is_file():
            raise RestoreValidationError("Restore upload metadata is missing")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RestoreValidationError("Restore upload metadata is invalid") from exc
    if not isinstance(value, dict):
        raise RestoreValidationError("Restore upload metadata is invalid")
    return value


def _authorize(metadata: dict[str, Any], token: str) -> None:
    supplied = _token_hash(str(token or ""))
    expected = str(metadata.get("token_hash") or "")
    if not expected or not hmac.compare_digest(supplied, expected):
        raise RestoreForbidden("Restore upload capability is invalid")


def _public_session(metadata: dict[str, Any]) -> dict[str, Any]:
    chunks = list(metadata.get("chunks") or [])
    return {
        "upload_id": metadata["upload_id"],
        "filename": metadata["filename"],
        "size_bytes": metadata["size_bytes"],
        "sha256": metadata["sha256"],
        "chunk_size": metadata["chunk_size"],
        "total_chunks": metadata["total_chunks"],
        "received_chunks": len(chunks),
        "received_bytes": sum(int(chunk["size"]) for chunk in chunks),
        "next_chunk": len(chunks),
        "state": metadata["state"],
        "validation_task_id": metadata.get("validation_task_id"),
        "request_id": metadata.get("request_id"),
        "created_at": metadata["created_at"],
        "updated_at": metadata["updated_at"],
    }


def _reconcile_durable_chunks(session: Path, metadata: dict[str, Any]) -> None:
    """Verify metadata-backed chunks and remove crash-orphan chunk files."""

    chunks_dir = session / "chunks"
    try:
        chunks_stat = chunks_dir.lstat()
    except FileNotFoundError as exc:
        raise RestoreConflict("Restore chunks directory is missing") from exc
    if stat.S_ISLNK(chunks_stat.st_mode) or not stat.S_ISDIR(chunks_stat.st_mode):
        raise RestoreConflict("Restore chunks directory is unsafe")
    chunks_fd = os.open(chunks_dir, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    changed = False
    try:
        declared = list(metadata.get("chunks") or [])
        tracked: dict[str, dict[str, Any]] = {}
        for expected_index, chunk in enumerate(declared):
            if int(chunk.get("index", -1)) != expected_index:
                raise RestoreConflict("Restore chunk metadata order is invalid")
            tracked[f"{expected_index:08d}.part"] = chunk

        for name in os.listdir(chunks_fd):
            match = _CHUNK_FILE_RE.fullmatch(name)
            if match is None:
                raise RestoreConflict("Unexpected restore chunk entry exists")
            entry_stat = os.stat(name, dir_fd=chunks_fd, follow_symlinks=False)
            if match.group("temporary") or name not in tracked:
                if stat.S_ISDIR(entry_stat.st_mode):
                    raise RestoreConflict("Unexpected restore chunk directory exists")
                os.unlink(name, dir_fd=chunks_fd)
                changed = True

        if changed:
            os.fsync(chunks_fd)

        for name, expected in tracked.items():
            try:
                fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=chunks_fd)
            except OSError as exc:
                raise RestoreConflict("A durable chunk is missing or unsafe") from exc
            digest = hashlib.sha256()
            size = 0
            try:
                file_stat = os.fstat(fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise RestoreConflict("A durable chunk is not a regular file")
                with os.fdopen(fd, "rb") as handle:
                    fd = -1
                    while block := handle.read(1024 * 1024):
                        digest.update(block)
                        size += len(block)
            finally:
                if fd >= 0:
                    os.close(fd)
            if size != int(expected.get("size", -1)) or not hmac.compare_digest(digest.hexdigest(), str(expected.get("sha256") or "")):
                raise RestoreConflict("A durable chunk differs from its metadata")
    finally:
        os.close(chunks_fd)


def _free_bytes(path: Path) -> int:
    usage = shutil.disk_usage(path)
    free = getattr(usage, "free", None)
    if free is not None:
        return int(free)
    return int(usage.f_bavail) * int(usage.f_frsize)


def create_upload_session(
    *,
    root: Path,
    filename: str,
    size_bytes: int,
    sha256: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    total_chunks: int,
) -> dict[str, Any]:
    """Create one capability-isolated upload directory."""

    root = Path(root)
    if Path(filename).name != filename or not _ARCHIVE_RE.fullmatch(filename):
        raise RestoreValidationError("Invalid restore filename")
    if not 0 < int(size_bytes) <= MAX_ARCHIVE_SIZE:
        raise RestoreValidationError("Restore archive size is outside the allowed range")
    if not _SHA256_RE.fullmatch(str(sha256)):
        raise RestoreValidationError("Restore archive sha256 is invalid")
    if not MIN_CHUNK_SIZE <= int(chunk_size) <= MAX_CHUNK_SIZE:
        # Tests and small internal clients may deliberately use tiny chunks;
        # retain the upper safety bound while allowing a positive test-sized
        # declaration. Production API policy supplies DEFAULT_CHUNK_SIZE.
        if int(chunk_size) <= 0 or int(chunk_size) > MAX_CHUNK_SIZE:
            raise RestoreValidationError("Restore chunk size is outside the allowed range")
    expected_chunks = (int(size_bytes) + int(chunk_size) - 1) // int(chunk_size)
    if int(total_chunks) != expected_chunks or not 0 < expected_chunks <= 1_000_000:
        raise RestoreValidationError("Restore chunk count does not match archive size")

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise RestoreValidationError("Restore staging root is invalid")
    # Assembly plus verified extraction must coexist until the immutable ready
    # handoff is published. The fixed margin covers metadata and tar headers.
    if _free_bytes(root) < int(size_bytes) * 2 + 1024 * 1024:
        raise RestoreValidationError("Insufficient free space for restore staging")

    upload_id = str(uuid4())
    token = secrets.token_urlsafe(32)
    session = _session_dir(root, upload_id)
    os.mkdir(session, 0o700)
    os.mkdir(session / "chunks", 0o700)
    timestamp = _now()
    metadata = {
        "version": 1,
        "upload_id": upload_id,
        "token_hash": _token_hash(token),
        "filename": filename,
        "size_bytes": int(size_bytes),
        "sha256": sha256,
        "chunk_size": int(chunk_size),
        "total_chunks": int(total_chunks),
        "chunks": [],
        "state": "uploading",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _atomic_json(session / "metadata.json", metadata)
    return {**_public_session(metadata), "upload_token": token}


def get_upload_session(*, root: Path, upload_id: str, token: str) -> dict[str, Any]:
    session = _session_dir(Path(root), upload_id)
    if not session.is_dir() or session.is_symlink():
        raise RestoreValidationError("Restore upload not found")
    with _session_lock(session):
        metadata = _read_metadata(session)
        _authorize(metadata, token)
        _reconcile_durable_chunks(session, metadata)
        return _public_session(metadata)


def _put_upload_chunk_source(
    *,
    root: Path,
    upload_id: str,
    token: str,
    index: int,
    size: int,
    actual_hash: str,
    sha256: str,
    data: bytes | None = None,
    source_path: Path | None = None,
) -> dict[str, Any]:
    session = _session_dir(Path(root), upload_id)
    if not session.is_dir() or session.is_symlink():
        raise RestoreValidationError("Restore upload not found")
    if not _SHA256_RE.fullmatch(str(sha256)) or not hmac.compare_digest(actual_hash, str(sha256)):
        raise RestoreValidationError("Restore chunk hash does not match its bytes")

    with _session_lock(session):
        metadata = _read_metadata(session)
        _authorize(metadata, token)
        _reconcile_durable_chunks(session, metadata)
        if metadata["state"] not in {"uploading", "uploaded"}:
            raise RestoreConflict("Restore upload is sealed for validation")
        chunks = list(metadata.get("chunks") or [])
        index = int(index)
        if index < len(chunks):
            existing = chunks[index]
            if int(existing["size"]) != size or not hmac.compare_digest(str(existing["sha256"]), actual_hash):
                raise RestoreConflict("Chunk was already uploaded with different content")
            return {**_public_session(metadata), "idempotent": True}
        if index != len(chunks):
            raise RestoreConflict(f"Restore expected chunk {len(chunks)}")
        if index >= int(metadata["total_chunks"]):
            raise RestoreConflict("Restore chunk index exceeds declared total")
        expected_size = int(metadata["chunk_size"])
        if index == int(metadata["total_chunks"]) - 1:
            expected_size = int(metadata["size_bytes"]) - (int(metadata["chunk_size"]) * index)
        if size != expected_size:
            raise RestoreValidationError(f"Restore chunk size is {size}; expected {expected_size}")
        chunks_dir = session / "chunks"
        chunk_name = f"{index:08d}.part"
        temporary_name = f"{chunk_name}.tmp"
        chunks_fd = os.open(chunks_dir, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
        try:
            try:
                os.stat(chunk_name, dir_fd=chunks_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RestoreConflict("Unexpected restore chunk file already exists")
            fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
                dir_fd=chunks_fd,
            )
            with os.fdopen(fd, "wb") as handle:
                written = 0
                digest = hashlib.sha256()
                if source_path is not None:
                    source_fd = os.open(source_path, os.O_RDONLY | _NOFOLLOW)
                    try:
                        source_stat = os.fstat(source_fd)
                        if not stat.S_ISREG(source_stat.st_mode):
                            raise RestoreValidationError("Restore chunk source is invalid")
                        with os.fdopen(source_fd, "rb") as source:
                            source_fd = -1
                            while block := source.read(1024 * 1024):
                                written += len(block)
                                digest.update(block)
                                handle.write(block)
                    finally:
                        if source_fd >= 0:
                            os.close(source_fd)
                else:
                    assert data is not None
                    for offset in range(0, len(data), 1024 * 1024):
                        block = data[offset:offset + 1024 * 1024]
                        written += len(block)
                        digest.update(block)
                        handle.write(block)
                if written != size or not hmac.compare_digest(
                    digest.hexdigest(), actual_hash
                ):
                    raise RestoreValidationError(
                        "Restore chunk source changed while it was persisted"
                    )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary_name,
                chunk_name,
                src_dir_fd=chunks_fd,
                dst_dir_fd=chunks_fd,
            )
            os.fsync(chunks_fd)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=chunks_fd)
                os.fsync(chunks_fd)
            except FileNotFoundError:
                pass
            os.close(chunks_fd)
        chunks.append({"index": index, "size": size, "sha256": actual_hash})
        metadata["chunks"] = chunks
        metadata["state"] = "uploaded" if len(chunks) == int(metadata["total_chunks"]) else "uploading"
        metadata["updated_at"] = _now()
        _atomic_json(session / "metadata.json", metadata)
        return {**_public_session(metadata), "idempotent": False}


def put_upload_chunk(
    *,
    root: Path,
    upload_id: str,
    token: str,
    index: int,
    data: bytes,
    sha256: str,
) -> dict[str, Any]:
    """Persist an in-memory chunk for non-HTTP/internal callers."""

    return _put_upload_chunk_source(
        root=root,
        upload_id=upload_id,
        token=token,
        index=index,
        size=len(data),
        actual_hash=hashlib.sha256(data).hexdigest(),
        sha256=sha256,
        data=data,
    )


def put_upload_chunk_file(
    *,
    root: Path,
    upload_id: str,
    token: str,
    index: int,
    source_path: Path,
    size: int,
    actual_sha256: str,
    sha256: str,
) -> dict[str, Any]:
    """Persist a bounded, no-follow HTTP ingress file without buffering it."""

    return _put_upload_chunk_source(
        root=root,
        upload_id=upload_id,
        token=token,
        index=index,
        size=int(size),
        actual_hash=actual_sha256,
        sha256=sha256,
        source_path=Path(source_path),
    )


def seal_upload_for_validation(
    *,
    root: Path,
    upload_id: str,
    token: str,
    task_id: str,
    replace_task_id: str | None = None,
) -> dict[str, Any]:
    """Seal all chunks and attach or exactly replace a validation TaskRun."""

    session = _session_dir(Path(root), upload_id)
    with _session_lock(session):
        metadata = _read_metadata(session)
        _authorize(metadata, token)
        _reconcile_durable_chunks(session, metadata)
        current_task = metadata.get("validation_task_id")
        if (
            current_task
            and current_task != task_id
            and current_task != replace_task_id
        ):
            raise RestoreConflict("Restore validation has already started")
        if metadata["state"] == "ready":
            if current_task == str(task_id) and (session / "ready-request.json").is_file():
                return _public_session(metadata)
            if current_task != replace_task_id or current_task == str(task_id):
                raise RestoreConflict("Restore ready handoff is inconsistent")
            # The attached durable TaskRun was lost. Revalidation owns the
            # existing chunks and invalidates the obsolete host command.
            (session / "ready-request.json").unlink(missing_ok=True)
        if metadata["state"] not in {
            "uploaded",
            "validating",
            "validation_failed",
            "ready",
        }:
            raise RestoreConflict("Restore upload is not complete")
        metadata["validation_task_id"] = str(task_id)
        metadata["state"] = "validating"
        metadata["updated_at"] = _now()
        _atomic_json(session / "metadata.json", metadata)
        return _public_session(metadata)


def _safe_member_path(name: str) -> PurePosixPath:
    if not name or "\\" in name:
        raise RestoreValidationError("Archive entry path is invalid")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RestoreValidationError(f"Archive entry path is unsafe: {name}")
    return path


def _allowed_file(path: PurePosixPath, contents: set[str]) -> bool:
    text = path.as_posix()
    if text in {"database.sql", "database.dump"}:
        return "database" in contents
    if len(path.parts) >= 2 and path.parts[0] in {
        "gallerydl-config",
        "app-config",
    }:
        return path.parts[0] in contents
    if len(path.parts) == 2 and path.parts[0] == "download-archives" and path.name.startswith("archive-") and path.name.endswith(".sqlite3"):
        return "download-archives" in contents
    if len(path.parts) >= 2 and path.parts[0] == "library-metadata" and path.name == "metadata.json":
        return "library-metadata" in contents
    return False


def _manifest_size(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_ARCHIVE_SIZE:
        raise RestoreValidationError(f"Archive manifest {label} size is invalid")
    return value


def _validated_manifest(archive: tarfile.TarFile, members: list[tarfile.TarInfo]):
    manifest_members = [member for member in members if member.name == "manifest.json"]
    if len(manifest_members) != 1 or not manifest_members[0].isreg():
        raise RestoreValidationError("Archive manifest.json is missing or invalid")
    manifest_member = manifest_members[0]
    if manifest_member.size <= 0 or manifest_member.size > MAX_MANIFEST_SIZE:
        raise RestoreValidationError("Archive manifest size is invalid")
    source = archive.extractfile(manifest_member)
    if source is None:
        raise RestoreValidationError("Archive manifest cannot be read")
    try:
        manifest = json.loads(source.read(MAX_MANIFEST_SIZE + 1))
    except json.JSONDecodeError as exc:
        raise RestoreValidationError("Archive manifest JSON is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise RestoreValidationError("Archive manifest version is unsupported")
    contents = manifest.get("contents")
    if not isinstance(contents, list) or not contents or len(contents) != len(set(contents)) or not set(contents).issubset(_COMPONENTS):
        raise RestoreValidationError("Archive manifest contents are invalid")
    if "database" not in contents:
        raise RestoreValidationError(
            "Archive is downloadable but not restorable because it has no database component"
        )
    entries = manifest.get("entries")
    if not isinstance(entries, dict) or not entries:
        raise RestoreValidationError("Archive manifest entries are missing")
    _manifest_size(manifest.get("total_uncompressed_bytes"), "total")
    return manifest, set(contents), entries


def _hash_stream(source) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        block = source.read(1024 * 1024)
        if not block:
            break
        size += len(block)
        digest.update(block)
    return size, digest.hexdigest()


def _validation_attempt_token(task_id: str, attempt: int) -> str:
    try:
        normalized_task = str(UUID(str(task_id)))
        normalized_attempt = int(attempt)
    except (TypeError, ValueError) as exc:
        raise RestoreValidationError("Restore validation attempt is invalid") from exc
    if normalized_task != str(task_id) or normalized_attempt < 1:
        raise RestoreValidationError("Restore validation attempt is invalid")
    return f"{normalized_task}.attempt-{normalized_attempt}"


_VALIDATION_ATTEMPT_TOKEN_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"\.attempt-[1-9][0-9]*"
)


def _validation_candidate_token(name: str) -> str | None:
    core = name
    if core.startswith(".") and core.endswith(".tmp"):
        core = core[1:-4]
    if core.startswith("archive.") and core.endswith(".tar.gz"):
        token = core[len("archive.") : -len(".tar.gz")]
    elif core.startswith("payload."):
        token = core[len("payload.") :]
    else:
        return None
    return token if _VALIDATION_ATTEMPT_TOKEN_RE.fullmatch(token) else None


def _remove_validation_candidate(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _clear_validation_attempt_candidates(
    session: Path,
    *,
    keep_token: str | None = None,
) -> None:
    """Bound retry staging to the current attempt while holding session lock."""

    for candidate in session.iterdir():
        token = _validation_candidate_token(candidate.name)
        if token is not None and token != keep_token:
            _remove_validation_candidate(candidate)


def validate_upload(
    *,
    root: Path,
    upload_id: str,
    task_id: str,
    attempt: int | None = None,
    publish_ready: bool = True,
) -> dict[str, Any]:
    """Assemble and validate an upload, optionally staging an attempt only.

    A registered worker uses ``publish_ready=False`` so cancellation of its
    ``to_thread`` await cannot let the still-running thread authorize a host
    restore.  Its caller publishes readiness only after an exact TaskRun fence.
    """

    if not publish_ready and attempt is None:
        raise RestoreValidationError("Registered restore validation needs an attempt")
    attempt_token = (
        _validation_attempt_token(task_id, int(attempt))
        if attempt is not None
        else None
    )

    root = Path(root)
    session = _session_dir(root, upload_id)
    with _session_lock(session):
        metadata = _read_metadata(session)
        _reconcile_durable_chunks(session, metadata)
        if metadata["state"] == "ready" and (session / "ready-request.json").is_file():
            ready = json.loads((session / "ready-request.json").read_text())
            if publish_ready or (
                ready.get("task_id") == str(task_id)
                and ready.get("attempt") == attempt
            ):
                return {
                    "state": "ready",
                    "request_id": ready["request_id"],
                    "manifest": ready["manifest"],
                    "host_command": ready["host_command"],
                    "message": "Restore request is ready for offline host execution",
                    "_already_published": True,
                }
        allowed_states = {"uploaded", "validating", "validation_failed"}
        if not publish_ready:
            allowed_states.add("ready")
        if metadata["state"] not in allowed_states:
            raise RestoreConflict("Restore upload is not complete")
        attached_task = metadata.get("validation_task_id")
        if attached_task is not None and attached_task != str(task_id):
            raise RestoreForbidden("Restore validation TaskRun does not own this upload")
        if not publish_ready:
            # A new attempt invalidates the prior host handoff in the staging
            # directory. The host independently rechecks this attempt against
            # the durable TaskRun before any destructive work.
            (session / "ready-request.json").unlink(missing_ok=True)
            _clear_validation_attempt_candidates(session)
        chunks = list(metadata.get("chunks") or [])
        if len(chunks) != int(metadata["total_chunks"]):
            raise RestoreConflict("Restore upload is incomplete")
        metadata["state"] = "validating"
        metadata["validation_task_id"] = str(task_id)
        metadata["updated_at"] = _now()
        _atomic_json(session / "metadata.json", metadata)

        archive_name = (
            f"archive.{attempt_token}.tar.gz"
            if attempt_token is not None
            else "archive.tar.gz"
        )
        payload_name = (
            f"payload.{attempt_token}" if attempt_token is not None else "payload"
        )
        archive_path = session / archive_name
        archive_temp = session / f".{archive_name}.tmp"
        payload = session / payload_name
        payload_temp = session / f".{payload_name}.tmp"
        try:
            archive_temp.unlink(missing_ok=True)
            if not publish_ready:
                archive_path.unlink(missing_ok=True)
                if payload.exists():
                    shutil.rmtree(payload)
            digest = hashlib.sha256()
            assembled_size = 0
            with open(archive_temp, "xb") as output:
                for expected_index, chunk in enumerate(chunks):
                    if int(chunk.get("index", -1)) != expected_index:
                        raise RestoreValidationError("Restore chunk order metadata is invalid")
                    chunk_path = session / "chunks" / f"{expected_index:08d}.part"
                    if chunk_path.is_symlink() or not chunk_path.is_file():
                        raise RestoreValidationError("Restore chunk file is missing")
                    data = chunk_path.read_bytes()
                    actual = hashlib.sha256(data).hexdigest()
                    if len(data) != int(chunk["size"]) or not hmac.compare_digest(actual, str(chunk["sha256"])):
                        raise RestoreValidationError("Restore chunk hash or size changed")
                    output.write(data)
                    digest.update(data)
                    assembled_size += len(data)
                output.flush()
                os.fsync(output.fileno())
            if assembled_size != int(metadata["size_bytes"]):
                raise RestoreValidationError("Restore archive size does not match upload")
            archive_hash = digest.hexdigest()
            if not hmac.compare_digest(archive_hash, str(metadata["sha256"])):
                raise RestoreValidationError("Restore archive hash does not match upload")
            os.replace(archive_temp, archive_path)

            try:
                archive = tarfile.open(archive_path, "r:gz")
            except (tarfile.TarError, OSError) as exc:
                raise RestoreValidationError("Restore archive is not a valid tar.gz") from exc
            with archive:
                members = archive.getmembers()
                if not members or len(members) > MAX_ARCHIVE_ENTRIES:
                    raise RestoreValidationError("Restore archive entry count is invalid")
                seen: set[str] = set()
                for member in members:
                    safe_path = _safe_member_path(member.name)
                    normalized = safe_path.as_posix()
                    if normalized in seen:
                        raise RestoreValidationError("Restore archive contains duplicate paths")
                    seen.add(normalized)
                    if not (member.isdir() or member.isreg()):
                        raise RestoreValidationError(f"Restore archive entry type is unsafe: {member.name}")
                manifest, contents, entries = _validated_manifest(archive, members)
                actual_files = {member.name: member for member in members if member.isreg() and member.name != "manifest.json"}
                if set(entries) != set(actual_files):
                    raise RestoreValidationError("Archive manifest entries do not match archive files")
                verified_total = 0
                validated_database_payloads: list[str] = []
                for name, member in actual_files.items():
                    safe_path = _safe_member_path(name)
                    if not _allowed_file(safe_path, contents):
                        raise RestoreValidationError(f"Archive file is not an allowed restore path: {name}")
                    declared = entries.get(name)
                    if not isinstance(declared, dict):
                        raise RestoreValidationError("Archive manifest entry is invalid")
                    declared_size = _manifest_size(
                        declared.get("size"),
                        f"entry {name}",
                    )
                    source = archive.extractfile(member)
                    if source is None:
                        raise RestoreValidationError("Archive file cannot be read")
                    actual_size, actual_hash = _hash_stream(source)
                    if declared_size != member.size or actual_size != member.size:
                        raise RestoreValidationError(f"Archive file size does not match manifest: {name}")
                    declared_hash = str(declared.get("sha256") or "")
                    if not _SHA256_RE.fullmatch(declared_hash) or not hmac.compare_digest(declared_hash, actual_hash):
                        raise RestoreValidationError(f"Archive file hash does not match manifest: {name}")
                    if name in _DATABASE_PAYLOADS:
                        validated_database_payloads.append(name)
                    verified_total += actual_size
                if len(validated_database_payloads) != 1:
                    raise RestoreValidationError(
                        "Archive must contain exactly one recognized database payload"
                    )
                if manifest["total_uncompressed_bytes"] != verified_total:
                    raise RestoreValidationError("Archive manifest total uncompressed size does not match files")
                if _free_bytes(root) < assembled_size + verified_total + 1024 * 1024:
                    raise RestoreValidationError("Insufficient free space for verified restore extraction")
                if payload_temp.exists():
                    shutil.rmtree(payload_temp)
                payload_temp.mkdir(mode=0o700)
                for name, member in actual_files.items():
                    target = payload_temp.joinpath(*PurePosixPath(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    source = archive.extractfile(member)
                    if source is None:
                        raise RestoreValidationError("Archive file cannot be extracted")
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    fd = os.open(target, flags, stat.S_IMODE(member.mode) & 0o700 or 0o600)
                    with os.fdopen(fd, "wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())

            if payload.exists():
                raise RestoreConflict("Verified restore payload already exists unexpectedly")
            os.replace(payload_temp, payload)
            host_command = f'./scripts/offline-restore.py --request "$HOST_RESTORE_STAGING/{upload_id}/ready-request.json"'
            ready = {
                "version": 1,
                "request_id": upload_id,
                "upload_id": upload_id,
                "task_id": str(task_id),
                "attempt": attempt,
                "archive": archive_name,
                "payload": payload_name,
                "archive_size": assembled_size,
                "archive_sha256": archive_hash,
                "manifest": manifest,
                "token_hash": metadata["token_hash"],
                "validated_at": _now(),
                "host_command": host_command,
            }
            if not publish_ready:
                return {
                    "state": "validated",
                    "request_id": upload_id,
                    "manifest": manifest,
                    "host_command": host_command,
                    "message": "Restore archive validated; awaiting attempt fence",
                    "_ready": ready,
                }
            ready_path = session / "ready-request.json"
            _atomic_json(ready_path, ready, mode=0o444)
            metadata["state"] = "ready"
            metadata["request_id"] = upload_id
            metadata["updated_at"] = _now()
            _atomic_json(session / "metadata.json", metadata)
            return {
                "state": "ready",
                "request_id": upload_id,
                "manifest": manifest,
                "host_command": host_command,
                "message": "Restore request is ready for offline host execution",
            }
        except Exception:
            for temporary in (archive_temp,):
                if temporary.exists():
                    temporary.unlink()
            if payload_temp.exists():
                shutil.rmtree(payload_temp)
            if not publish_ready:
                _remove_validation_candidate(archive_path)
                _remove_validation_candidate(payload)
            failed = _read_metadata(session)
            failed["state"] = "validation_failed"
            failed["updated_at"] = _now()
            _atomic_json(session / "metadata.json", failed)
            raise


def publish_validated_upload(
    *,
    root: Path,
    upload_id: str,
    task_id: str,
    attempt: int,
    staged: dict[str, Any],
) -> dict[str, Any]:
    """Publish a staged host handoff for exactly the supplied attempt.

    The registered async handler must call this synchronously, without an await,
    immediately after locking and fencing its TaskRun row.
    """

    token = _validation_attempt_token(task_id, attempt)
    ready = staged.get("_ready")
    if staged.get("_already_published"):
        return {
            key: value for key, value in staged.items() if not key.startswith("_")
        }
    if not isinstance(ready, dict):
        raise RestoreValidationError("Restore validation candidate is missing")
    archive_name = f"archive.{token}.tar.gz"
    payload_name = f"payload.{token}"
    if (
        ready.get("task_id") != str(task_id)
        or ready.get("attempt") != int(attempt)
        or ready.get("archive") != archive_name
        or ready.get("payload") != payload_name
    ):
        raise RestoreForbidden("Restore validation candidate attempt is stale")

    session = _session_dir(Path(root), upload_id)
    with _session_lock(session):
        metadata = _read_metadata(session)
        if metadata.get("validation_task_id") != str(task_id):
            raise RestoreForbidden("Restore validation TaskRun does not own this upload")
        archive = session / archive_name
        payload = session / payload_name
        if archive.is_symlink() or not archive.is_file():
            raise RestoreValidationError("Validated restore archive is missing")
        if payload.is_symlink() or not payload.is_dir():
            raise RestoreValidationError("Validated restore payload is missing")
        _atomic_json(session / "ready-request.json", ready, mode=0o444)
        metadata["state"] = "ready"
        metadata["request_id"] = upload_id
        metadata["validation_attempt"] = int(attempt)
        metadata["updated_at"] = _now()
        _atomic_json(session / "metadata.json", metadata)
        _clear_validation_attempt_candidates(session, keep_token=token)
    return {
        "state": "ready",
        "request_id": upload_id,
        "manifest": ready["manifest"],
        "host_command": ready["host_command"],
        "message": "Restore request is ready for offline host execution",
    }


def read_restore_receipt(*, staging: Path, receipts: Path, request_id: str, token: str) -> dict[str, Any]:
    """Return only the external host receipt authorized by the upload token."""

    session = _session_dir(Path(staging), request_id)
    with _session_lock(session):
        metadata = _read_metadata(session)
        _authorize(metadata, token)
        if metadata.get("state") != "ready":
            raise RestoreConflict("Restore request is not ready")
    receipt_root = Path(receipts).resolve()
    receipt_path = (receipt_root / f"{request_id}.json").resolve(strict=False)
    try:
        receipt_path.relative_to(receipt_root)
    except ValueError as exc:
        raise RestoreValidationError("Restore receipt path escaped its root") from exc
    if not receipt_path.exists():
        return {"request_id": request_id, "status": "pending", "phase": "handoff"}
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise RestoreValidationError("Restore receipt is invalid")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RestoreValidationError("Restore receipt is invalid") from exc
    if not isinstance(receipt, dict) or receipt.get("request_id") != request_id:
        raise RestoreValidationError("Restore receipt identity is invalid")
    allowed = {
        "request_id",
        "status",
        "phase",
        "started_at",
        "completed_at",
        "rollback_performed",
        "rollback_status",
        "rollback_components",
        "diagnostic",
        "error",
        "rollback_command",
    }
    return {key: value for key, value in receipt.items() if key in allowed}
