"""Staged offline restore trust-boundary regressions."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_DEFAULT_MANIFEST_TOTAL = object()


def _archive_bytes(
    files: dict[str, bytes],
    *,
    manifest_entries: dict[str, dict[str, object]] | None = None,
    manifest_total: object = _DEFAULT_MANIFEST_TOTAL,
    contents: list[str] | None = None,
    extra_members: list[tarfile.TarInfo] | None = None,
) -> bytes:
    entries = manifest_entries or {
        name: {"size": len(data), "sha256": _sha256(data)}
        for name, data in files.items()
    }
    total = (
        sum(entry["size"] for entry in entries.values())
        if manifest_total is _DEFAULT_MANIFEST_TOTAL
        else manifest_total
    )
    manifest = json.dumps(
        {
            "version": "0.3.0",
            "created_at": "20260824_120000",
            "contents": contents or ["database"],
            "component_sizes": {"database": len(files.get("database.sql", b""))},
            "entries": entries,
            "total_uncompressed_bytes": total,
        },
        separators=(",", ":"),
    ).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in {"manifest.json": manifest, **files}.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(data))
        for member in extra_members or []:
            archive.addfile(member)
    return output.getvalue()


def _new_session(root: Path, archive: bytes, *, chunk_size: int = 32):
    from app.services.offline_restore import create_upload_session

    return create_upload_session(
        root=root,
        filename="auto-gallery-backup_20260824_120000.tar.gz",
        size_bytes=len(archive),
        sha256=_sha256(archive),
        chunk_size=chunk_size,
        total_chunks=(len(archive) + chunk_size - 1) // chunk_size,
    )


def _link_member() -> tarfile.TarInfo:
    member = tarfile.TarInfo("gallerydl-config/link")
    member.type = tarfile.SYMTYPE
    member.linkname = "/etc/passwd"
    return member


def test_chunks_are_ordered_idempotent_and_resumable(tmp_path):
    """A missing order check, duplicate rewrite, or memory-only cursor breaks this."""
    from app.services.offline_restore import (
        RestoreConflict,
        get_upload_session,
        put_upload_chunk,
    )

    archive = _archive_bytes({"database.sql": b"select 1;"})
    created = _new_session(tmp_path, archive, chunk_size=31)
    upload_id = created["upload_id"]
    token = created["upload_token"]
    chunks = [archive[index : index + 31] for index in range(0, len(archive), 31)]

    with pytest.raises(RestoreConflict, match="expected chunk 0"):
        put_upload_chunk(
            root=tmp_path,
            upload_id=upload_id,
            token=token,
            index=1,
            data=chunks[1],
            sha256=_sha256(chunks[1]),
        )

    first = put_upload_chunk(
        root=tmp_path,
        upload_id=upload_id,
        token=token,
        index=0,
        data=chunks[0],
        sha256=_sha256(chunks[0]),
    )
    duplicate = put_upload_chunk(
        root=tmp_path,
        upload_id=upload_id,
        token=token,
        index=0,
        data=chunks[0],
        sha256=_sha256(chunks[0]),
    )
    assert first["next_chunk"] == 1
    assert duplicate["idempotent"] is True

    with pytest.raises(RestoreConflict, match="different content"):
        put_upload_chunk(
            root=tmp_path,
            upload_id=upload_id,
            token=token,
            index=0,
            data=b"different",
            sha256=_sha256(b"different"),
        )

    resumed = get_upload_session(root=tmp_path, upload_id=upload_id, token=token)
    assert resumed["next_chunk"] == 1
    assert resumed["received_bytes"] == len(chunks[0])

    for index, chunk in enumerate(chunks[1:], start=1):
        put_upload_chunk(
            root=tmp_path,
            upload_id=upload_id,
            token=token,
            index=index,
            data=chunk,
            sha256=_sha256(chunk),
        )
    assert get_upload_session(root=tmp_path, upload_id=upload_id, token=token)["state"] == "uploaded"


def test_chunk_resume_removes_a_crash_orphan_without_following_its_symlink(tmp_path):
    """A deterministic temp left by a crash must be safely reconciled on retry."""
    from app.services.offline_restore import put_upload_chunk

    archive = _archive_bytes({"database.sql": b"select 1;"})
    created = _new_session(tmp_path, archive, chunk_size=len(archive))
    session = tmp_path / created["upload_id"]
    outside = tmp_path / "outside-chunk"
    outside.write_bytes(b"outside")
    orphan = session / "chunks/00000000.part.tmp"
    orphan.symlink_to(outside)

    result = put_upload_chunk(
        root=tmp_path,
        upload_id=created["upload_id"],
        token=created["upload_token"],
        index=0,
        data=archive,
        sha256=_sha256(archive),
    )

    assert result["state"] == "uploaded"
    assert outside.read_bytes() == b"outside"
    assert not orphan.exists()


def test_idempotent_retry_rejects_corrupt_durable_chunk_bytes(tmp_path):
    """Metadata alone must never authorize a corrupted committed chunk."""
    from app.services.offline_restore import RestoreConflict, put_upload_chunk

    archive = _archive_bytes({"database.sql": b"select 1;"})
    created = _new_session(tmp_path, archive, chunk_size=len(archive))
    put_upload_chunk(
        root=tmp_path,
        upload_id=created["upload_id"],
        token=created["upload_token"],
        index=0,
        data=archive,
        sha256=_sha256(archive),
    )
    durable = tmp_path / created["upload_id"] / "chunks/00000000.part"
    durable.write_bytes(b"x" * len(archive))

    with pytest.raises(RestoreConflict, match="durable chunk"):
        put_upload_chunk(
            root=tmp_path,
            upload_id=created["upload_id"],
            token=created["upload_token"],
            index=0,
            data=archive,
            sha256=_sha256(archive),
        )


def test_chunk_and_chunks_directory_are_fsynced_before_metadata_advance(tmp_path, monkeypatch):
    """A durable cursor may advance only after file and rename directory fsyncs."""
    from app.services import offline_restore

    archive = _archive_bytes({"database.sql": b"select 1;"})
    created = _new_session(tmp_path, archive, chunk_size=len(archive))
    events: list[str] = []
    real_fsync = offline_restore.os.fsync
    real_atomic_json = offline_restore._atomic_json

    def observed_fsync(fd: int) -> None:
        events.append(f"fsync:{os.readlink(f'/proc/self/fd/{fd}')}")
        real_fsync(fd)

    def observed_atomic_json(path: Path, value: dict, *, mode: int = 0o600) -> None:
        if path.name == "metadata.json":
            events.append("metadata")
        real_atomic_json(path, value, mode=mode)

    monkeypatch.setattr(offline_restore.os, "fsync", observed_fsync)
    monkeypatch.setattr(offline_restore, "_atomic_json", observed_atomic_json)

    offline_restore.put_upload_chunk(
        root=tmp_path,
        upload_id=created["upload_id"],
        token=created["upload_token"],
        index=0,
        data=archive,
        sha256=_sha256(archive),
    )

    chunk_fsync = next(index for index, event in enumerate(events) if event.endswith("00000000.part.tmp"))
    directory_fsync = next(index for index, event in enumerate(events) if event.endswith(f"{created['upload_id']}/chunks"))
    metadata = events.index("metadata")
    assert chunk_fsync < directory_fsync < metadata


def test_session_rejects_wrong_token_invalid_shape_and_insufficient_space(tmp_path, monkeypatch):
    """A guessed UUID, oversized declaration, or low disk must fail before writes."""
    from app.services import offline_restore
    from app.services.offline_restore import RestoreForbidden, RestoreValidationError

    archive = _archive_bytes({"database.sql": b"select 1;"})
    created = _new_session(tmp_path, archive)
    with pytest.raises(RestoreForbidden):
        offline_restore.get_upload_session(
            root=tmp_path,
            upload_id=created["upload_id"],
            token="wrong-token",
        )
    with pytest.raises(RestoreValidationError, match="filename"):
        offline_restore.create_upload_session(
            root=tmp_path,
            filename="../backup.tar.gz",
            size_bytes=1,
            sha256="0" * 64,
            chunk_size=1,
            total_chunks=1,
        )

    monkeypatch.setattr(
        offline_restore.shutil,
        "disk_usage",
        lambda _path: os.statvfs_result((4096, 4096, 1, 1, 1, 0, 0, 255, 255, 255)),
    )
    with pytest.raises(RestoreValidationError, match="free space"):
        offline_restore.create_upload_session(
            root=tmp_path,
            filename="backup.tar.gz",
            size_bytes=1024,
            sha256="0" * 64,
            chunk_size=1024,
            total_chunks=1,
        )


@pytest.mark.parametrize(
    ("member", "message"),
    [
        (tarfile.TarInfo("../outside"), "path"),
        (tarfile.TarInfo("/absolute"), "path"),
        (_link_member(), "entry type"),
    ],
)
def test_validation_rejects_unsafe_archive_entries(tmp_path, member, message):
    """Accepting traversal, absolute paths, or links lets staging escape its root."""
    from app.services.offline_restore import RestoreValidationError, validate_upload

    archive = _archive_bytes(
        {"database.sql": b"select 1;"},
        extra_members=[member],
    )
    created = _new_session(tmp_path, archive)
    _upload_all(tmp_path, created, archive)
    with pytest.raises(RestoreValidationError, match=message):
        validate_upload(
            root=tmp_path,
            upload_id=created["upload_id"],
            task_id="00000000-0000-0000-0000-000000000001",
        )
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize(
    ("archive", "message"),
    [
        (_archive_bytes({"unexpected.bin": b"x"}), "allowed restore path"),
        (
            _archive_bytes(
                {"database.sql": b"select 1;"},
                manifest_entries={"database.sql": {"size": 9, "sha256": "0" * 64}},
            ),
            "hash",
        ),
        (
            _archive_bytes(
                {"database.sql": b"select 1;"},
                manifest_entries={"database.sql": {"size": 99, "sha256": _sha256(b"select 1;")}},
            ),
            "size",
        ),
    ],
)
def test_validation_enforces_allowed_paths_manifest_hashes_and_sizes(tmp_path, archive, message):
    """A missing allowlist/hash/size branch could hand a mutated payload to host."""
    from app.services.offline_restore import RestoreValidationError, validate_upload

    created = _new_session(tmp_path, archive)
    _upload_all(tmp_path, created, archive)
    with pytest.raises(RestoreValidationError, match=message):
        validate_upload(
            root=tmp_path,
            upload_id=created["upload_id"],
            task_id="00000000-0000-0000-0000-000000000002",
        )


def _stage_archive_bytes(tmp_path: Path, archive: bytes):
    created = _new_session(tmp_path, archive, chunk_size=len(archive))
    _upload_all(tmp_path, created, archive, chunk_size=len(archive))
    return created


def _validate_staged_archive(tmp_path: Path, created: dict, task_suffix: int):
    from app.services.offline_restore import validate_upload

    return validate_upload(
        root=tmp_path,
        upload_id=created["upload_id"],
        task_id=f"00000000-0000-0000-0000-{task_suffix:012d}",
    )


def test_database_payload_honest_archive_becomes_ready(tmp_path):
    """One verified canonical dump is the payload authorized for host restore."""
    archive = _archive_bytes({"database.dump": b"custom-dump"})

    created = _stage_archive_bytes(tmp_path, archive)
    result = _validate_staged_archive(tmp_path, created, 21)

    assert result["state"] == "ready"
    session = tmp_path / created["upload_id"]
    assert (session / "payload/database.dump").read_bytes() == b"custom-dump"
    assert (session / "ready-request.json").is_file()


def test_database_payload_missing_archive_never_becomes_ready(tmp_path):
    """A database component label cannot substitute for an actual dump."""
    from app.services.offline_restore import RestoreValidationError

    archive = _archive_bytes(
        {"app-config/config.json": b"{}"},
        contents=["database", "app-config"],
    )
    created = _stage_archive_bytes(tmp_path, archive)

    with pytest.raises(RestoreValidationError, match="database payload"):
        _validate_staged_archive(tmp_path, created, 22)
    assert not (tmp_path / created["upload_id"] / "ready-request.json").exists()


def test_database_payload_ambiguous_archive_never_becomes_ready(tmp_path):
    """Two supported dump names are ambiguous even when both hashes are honest."""
    from app.services.offline_restore import RestoreValidationError

    archive = _archive_bytes(
        {
            "database.dump": b"custom-dump",
            "database.sql": b"select 1;",
        }
    )
    created = _stage_archive_bytes(tmp_path, archive)

    with pytest.raises(RestoreValidationError, match="database payload"):
        _validate_staged_archive(tmp_path, created, 23)
    assert not (tmp_path / created["upload_id"] / "ready-request.json").exists()


def test_database_payload_duplicate_archive_member_never_becomes_ready(tmp_path):
    """A repeated canonical dump member cannot hide behind one manifest entry."""
    from app.services.offline_restore import RestoreValidationError

    duplicate = tarfile.TarInfo("database.dump")
    duplicate.size = 0
    duplicate.mode = 0o600
    archive = _archive_bytes(
        {"database.dump": b"custom-dump"},
        extra_members=[duplicate],
    )
    created = _stage_archive_bytes(tmp_path, archive)

    with pytest.raises(RestoreValidationError, match="duplicate"):
        _validate_staged_archive(tmp_path, created, 24)
    assert not (tmp_path / created["upload_id"] / "ready-request.json").exists()


def test_database_payload_wrong_type_archive_never_becomes_ready(tmp_path):
    """A directory named like a dump is not a database payload."""
    from app.services.offline_restore import RestoreValidationError

    directory = tarfile.TarInfo("database.dump")
    directory.type = tarfile.DIRTYPE
    directory.mode = 0o700
    archive = _archive_bytes(
        {},
        manifest_entries={
            "database.dump": {"size": 0, "sha256": _sha256(b"")},
        },
        extra_members=[directory],
    )
    created = _stage_archive_bytes(tmp_path, archive)

    with pytest.raises(RestoreValidationError, match="database payload|entries"):
        _validate_staged_archive(tmp_path, created, 25)
    assert not (tmp_path / created["upload_id"] / "ready-request.json").exists()


def test_database_payload_hash_mismatch_archive_never_becomes_ready(tmp_path):
    """The selected dump earns readiness only after its own hash validation."""
    from app.services.offline_restore import RestoreValidationError

    archive = _archive_bytes(
        {"database.dump": b"custom-dump"},
        manifest_entries={
            "database.dump": {"size": 11, "sha256": "0" * 64},
        },
    )
    created = _stage_archive_bytes(tmp_path, archive)

    with pytest.raises(RestoreValidationError, match="hash"):
        _validate_staged_archive(tmp_path, created, 26)
    assert not (tmp_path / created["upload_id"] / "ready-request.json").exists()


@pytest.mark.parametrize("field", ["entry", "total"])
@pytest.mark.parametrize(
    "representation",
    ["bool", "string", "float", "null", "negative", "out-of-range"],
)
def test_manifest_sizes_require_bounded_json_integers_before_readiness(
    tmp_path,
    field,
    representation,
):
    """Actual archives never gain readiness through JSON number coercion."""
    from app.services.offline_restore import (
        MAX_ARCHIVE_SIZE,
        RestoreValidationError,
    )

    data = b"x"
    valid_size = len(data)
    malformed = {
        "bool": True,
        "string": str(valid_size),
        "float": float(valid_size),
        "null": None,
        "negative": -1,
        "out-of-range": MAX_ARCHIVE_SIZE + 1,
    }[representation]
    entry_size = malformed if field == "entry" else valid_size
    total = malformed if field == "total" else valid_size
    archive = _archive_bytes(
        {"database.dump": data},
        manifest_entries={
            "database.dump": {
                "size": entry_size,
                "sha256": _sha256(data),
            }
        },
        manifest_total=total,
    )
    created = _stage_archive_bytes(tmp_path, archive)

    with pytest.raises(RestoreValidationError, match="size"):
        _validate_staged_archive(tmp_path, created, 30)

    assert not (tmp_path / created["upload_id"] / "ready-request.json").exists()


def test_database_payload_unrecognized_filename_never_becomes_ready(tmp_path):
    """A self-consistent but unsupported database filename fails closed."""
    from app.services.offline_restore import RestoreValidationError

    archive = _archive_bytes({"database.backup": b"custom-dump"})
    created = _stage_archive_bytes(tmp_path, archive)

    with pytest.raises(RestoreValidationError, match="database payload|allowed"):
        _validate_staged_archive(tmp_path, created, 27)
    assert not (tmp_path / created["upload_id"] / "ready-request.json").exists()


def test_validation_atomically_publishes_read_only_ready_request(tmp_path):
    """Publishing readiness before verified extraction could authorize bad input."""
    from app.services.offline_restore import seal_upload_for_validation, validate_upload

    files = {
        "database.sql": b"select 1;",
        "gallerydl-config/config.json": b"{}",
        "download-archives/archive-pixiv.sqlite3": b"sqlite",
    }
    archive = _archive_bytes(
        files,
        contents=["database", "gallerydl-config", "download-archives"],
    )
    created = _new_session(tmp_path, archive, chunk_size=29)
    _upload_all(tmp_path, created, archive, chunk_size=29)
    result = validate_upload(
        root=tmp_path,
        upload_id=created["upload_id"],
        task_id="00000000-0000-0000-0000-000000000003",
    )

    session_dir = tmp_path / created["upload_id"]
    ready_path = session_dir / "ready-request.json"
    assert result["state"] == "ready"
    assert result["request_id"] == created["upload_id"]
    assert ready_path.is_file()
    assert stat.S_IMODE(ready_path.stat().st_mode) == 0o444
    ready = json.loads(ready_path.read_text())
    assert ready["archive_sha256"] == _sha256(archive)
    assert ready["task_id"] == "00000000-0000-0000-0000-000000000003"
    assert ready["manifest"]["entries"]["database.sql"]["sha256"] == _sha256(b"select 1;")
    assert (session_dir / "payload" / "database.sql").read_bytes() == b"select 1;"
    assert not list(session_dir.glob("*.tmp"))

    resealed = seal_upload_for_validation(
        root=tmp_path,
        upload_id=created["upload_id"],
        token=created["upload_token"],
        task_id="00000000-0000-0000-0000-000000000003",
    )
    assert resealed["state"] == "ready"


def test_registered_validation_stages_attempt_without_publishing_host_readiness(tmp_path):
    """A cancelled validation thread must not authorize an obsolete attempt."""
    from app.services.offline_restore import publish_validated_upload, validate_upload

    archive = _archive_bytes({"database.sql": b"select 1;"})
    created = _stage_archive_bytes(tmp_path, archive)
    task_id = "00000000-0000-0000-0000-000000000004"

    staged = validate_upload(
        root=tmp_path,
        upload_id=created["upload_id"],
        task_id=task_id,
        attempt=3,
        publish_ready=False,
    )

    session = tmp_path / created["upload_id"]
    assert staged["state"] == "validated"
    assert not (session / "ready-request.json").exists()
    assert (session / f"archive.{task_id}.attempt-3.tar.gz").is_file()
    assert (session / f"payload.{task_id}.attempt-3/database.sql").is_file()

    published = publish_validated_upload(
        root=tmp_path,
        upload_id=created["upload_id"],
        task_id=task_id,
        attempt=3,
        staged=staged,
    )
    ready = json.loads((session / "ready-request.json").read_text())
    assert published["state"] == "ready"
    assert ready["attempt"] == 3
    assert ready["archive"] == f"archive.{task_id}.attempt-3.tar.gz"
    assert ready["payload"] == f"payload.{task_id}.attempt-3"


def test_registered_validation_retry_removes_previous_attempt_candidates(tmp_path):
    """Only the current validation attempt may consume persistent staging space."""
    from app.services.offline_restore import validate_upload

    archive = _archive_bytes({"database.sql": b"select 1;"})
    created = _stage_archive_bytes(tmp_path, archive)
    task_id = "00000000-0000-0000-0000-000000000014"
    session = tmp_path / created["upload_id"]

    validate_upload(
        root=tmp_path,
        upload_id=created["upload_id"],
        task_id=task_id,
        attempt=1,
        publish_ready=False,
    )
    first_archive = session / f"archive.{task_id}.attempt-1.tar.gz"
    first_payload = session / f"payload.{task_id}.attempt-1"
    assert first_archive.is_file()
    assert first_payload.is_dir()

    validate_upload(
        root=tmp_path,
        upload_id=created["upload_id"],
        task_id=task_id,
        attempt=2,
        publish_ready=False,
    )

    assert not first_archive.exists()
    assert not first_payload.exists()
    assert (session / f"archive.{task_id}.attempt-2.tar.gz").is_file()
    assert (session / f"payload.{task_id}.attempt-2").is_dir()


def test_failed_registered_validation_removes_current_attempt_candidates(tmp_path):
    """A rejected archive must not retain its assembled attempt archive or payload."""
    from app.services.offline_restore import RestoreValidationError, validate_upload

    archive = _archive_bytes(
        {"database.sql": b"select 1;"},
        manifest_entries={
            "database.sql": {"size": 9, "sha256": "0" * 64},
        },
    )
    created = _stage_archive_bytes(tmp_path, archive)
    task_id = "00000000-0000-0000-0000-000000000015"
    session = tmp_path / created["upload_id"]

    with pytest.raises(RestoreValidationError, match="hash does not match"):
        validate_upload(
            root=tmp_path,
            upload_id=created["upload_id"],
            task_id=task_id,
            attempt=1,
            publish_ready=False,
        )

    assert not (session / f"archive.{task_id}.attempt-1.tar.gz").exists()
    assert not (session / f"payload.{task_id}.attempt-1").exists()


def _upload_all(root: Path, created: dict, archive: bytes, *, chunk_size: int = 32):
    from app.services.offline_restore import put_upload_chunk

    for index, offset in enumerate(range(0, len(archive), chunk_size)):
        chunk = archive[offset : offset + chunk_size]
        put_upload_chunk(
            root=root,
            upload_id=created["upload_id"],
            token=created["upload_token"],
            index=index,
            data=chunk,
            sha256=_sha256(chunk),
        )


def test_created_backup_manifest_carries_every_file_hash(tmp_path, monkeypatch):
    """A backup without entry hashes cannot cross the restore trust boundary."""
    from app.api.admin import backup

    source = tmp_path / "config-source"
    source.mkdir()
    (source / "settings.json").write_text('{"safe":true}')
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setenv("APP_CONFIG_ROOT", str(source))

    result = backup._create_backup_sync({"contents": ["app-config"]})

    with tarfile.open(backup_dir / result["filename"], "r:gz") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
    expected = b'{"safe":true}'
    assert manifest["version"] == "0.3.0"
    assert manifest["total_uncompressed_bytes"] == len(expected)
    assert manifest["entries"] == {
        "app-config/settings.json": {
            "size": len(expected),
            "sha256": _sha256(expected),
        }
    }


def test_database_backup_excludes_pgpass_and_passes_restore_validation(
    tmp_path,
    monkeypatch,
):
    """Generated database backups contain only portable restore payloads."""
    from app.api.admin import backup
    from app.services.offline_restore import validate_upload

    secret = "backup-passfile-canary"
    backup_dir = tmp_path / "backups"
    temporary_directories: list[Path] = []
    observed: dict[str, Path] = {}
    real_mkdtemp = backup.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        path = Path(real_mkdtemp(*args, **kwargs))
        temporary_directories.append(path)
        return str(path)

    def fake_pg_dump(command, *, capture_output, text, env, timeout):
        assert capture_output is True
        assert text is True
        assert timeout == 120
        dump_path = Path(command[command.index("-f") + 1])
        passfile_path = Path(env["PGPASSFILE"])
        assert passfile_path.is_file()
        observed.update(dump=dump_path, passfile=passfile_path)
        dump_path.write_bytes(b"portable-custom-dump")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(
        backup.settings,
        "database_url",
        f"postgresql+asyncpg://autogallery:{secret}@postgres:5432/autogallery",
    )
    monkeypatch.setattr(backup.tempfile, "mkdtemp", tracked_mkdtemp)
    monkeypatch.setattr(backup.subprocess, "run", fake_pg_dump)

    result = backup._create_backup_sync({"contents": ["database"]})
    archive_path = backup_dir / result["filename"]
    archive_bytes = archive_path.read_bytes()

    assert observed["passfile"].parent != observed["dump"].parent
    assert not observed["passfile"].exists()
    assert all(not directory.exists() for directory in temporary_directories)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        regular_names = {
            member.name for member in archive.getmembers() if member.isreg()
        }
        manifest = json.load(archive.extractfile("manifest.json"))
        archived_bytes = b"".join(
            archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isreg()
        )
    assert regular_names == {"database.dump", "manifest.json"}
    assert set(manifest["entries"]) == {"database.dump"}
    assert secret.encode() not in archived_bytes

    restore_root = tmp_path / "restore"
    created = _new_session(restore_root, archive_bytes, chunk_size=len(archive_bytes))
    _upload_all(
        restore_root,
        created,
        archive_bytes,
        chunk_size=len(archive_bytes),
    )
    validated = validate_upload(
        root=restore_root,
        upload_id=created["upload_id"],
        task_id="00000000-0000-0000-0000-000000000101",
    )

    assert validated["state"] == "ready"
    payload = restore_root / created["upload_id"] / "payload" / "database.dump"
    assert payload.read_bytes() == b"portable-custom-dump"


def test_database_backup_failure_removes_credentials_payload_and_candidate(
    tmp_path,
    monkeypatch,
):
    """A failed pg_dump leaves no credential, payload, or candidate file."""
    from app.api.admin import backup

    backup_dir = tmp_path / "backups"
    temporary_directories: list[Path] = []
    observed: dict[str, Path] = {}
    real_mkdtemp = backup.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        path = Path(real_mkdtemp(*args, **kwargs))
        temporary_directories.append(path)
        return str(path)

    def failed_pg_dump(command, *, capture_output, text, env, timeout):
        dump_path = Path(command[command.index("-f") + 1])
        passfile_path = Path(env["PGPASSFILE"])
        assert passfile_path.is_file()
        observed.update(dump=dump_path, passfile=passfile_path)
        return SimpleNamespace(returncode=1, stderr="injected pg_dump failure")

    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(backup.tempfile, "mkdtemp", tracked_mkdtemp)
    monkeypatch.setattr(backup.subprocess, "run", failed_pg_dump)

    with pytest.raises(RuntimeError, match="Database dump failed"):
        backup._create_backup_sync(
            {"contents": ["database"]},
            publish=False,
            candidate_token="00000000-0000-0000-0000-000000000102-attempt-1",
        )

    assert observed["passfile"].parent != observed["dump"].parent
    assert all(not directory.exists() for directory in temporary_directories)
    assert list((backup_dir / ".pending").iterdir()) == []


def test_registered_backup_stage_neither_publishes_nor_prunes(tmp_path, monkeypatch):
    """A cancelled backup thread may leave a candidate, never a visible backup."""
    from app.api.admin import backup

    source = tmp_path / "config-source"
    source.mkdir()
    (source / "settings.json").write_text("{}", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for index in range(11):
        (backup_dir / f"auto-gallery-backup_20260827_1200{index:02d}.tar.gz").write_bytes(b"old")
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setenv("APP_CONFIG_ROOT", str(source))
    pending_dir = backup_dir / ".pending"
    pending_dir.mkdir()
    stale = pending_dir / (
        "auto-gallery-backup_20260827_010101.tar.gz."
        "00000000-0000-0000-0000-000000000005-attempt-1.pending"
    )
    stale.write_bytes(b"stale")

    staged = backup._create_backup_sync(
        {"contents": ["app-config"]},
        publish=False,
        candidate_token="00000000-0000-0000-0000-000000000005-attempt-2",
    )

    assert len(backup._list_backup_files()) == 11
    assert not (backup_dir / staged["filename"]).exists()
    assert Path(staged["_candidate_path"]).is_file()
    assert not stale.exists()
    assert list(pending_dir.iterdir()) == [Path(staged["_candidate_path"])]

    published = backup.publish_backup_candidate(staged)
    assert (backup_dir / published["filename"]).is_file()
    assert "_candidate_path" not in published
    assert len(backup._list_backup_files()) == 10


def test_registered_backup_rejects_symlinked_pending_directory(tmp_path, monkeypatch):
    """Candidate cleanup must never follow .pending outside the backup root."""
    from app.api.admin import backup

    source = tmp_path / "config-source"
    source.mkdir()
    (source / "settings.json").write_text("{}", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "preserve.pending"
    victim.write_bytes(b"preserve")
    (backup_dir / ".pending").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setenv("APP_CONFIG_ROOT", str(source))

    with pytest.raises(OSError):
        backup._create_backup_sync(
            {"contents": ["app-config"]},
            publish=False,
            candidate_token="00000000-0000-0000-0000-000000000005-attempt-3",
        )

    assert victim.read_bytes() == b"preserve"


def test_registered_backup_publish_rejects_replaced_pending_directory(
    tmp_path,
    monkeypatch,
):
    """Atomic publication must reopen .pending without following replacement."""
    from app.api.admin import backup

    source = tmp_path / "config-source"
    source.mkdir()
    (source / "settings.json").write_text("{}", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setenv("APP_CONFIG_ROOT", str(source))
    staged = backup._create_backup_sync(
        {"contents": ["app-config"]},
        publish=False,
        candidate_token="00000000-0000-0000-0000-000000000005-attempt-4",
    )

    pending = backup_dir / ".pending"
    original_pending = backup_dir / ".pending-original"
    pending.rename(original_pending)
    outside = tmp_path / "outside-publish"
    outside.mkdir()
    external = outside / Path(staged["_candidate_path"]).name
    external.write_bytes(b"external")
    pending.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        backup.publish_backup_candidate(staged)

    assert external.read_bytes() == b"external"
    assert (original_pending / external.name).is_file()


def test_registered_backup_exception_cleanup_uses_original_pending_directory(
    tmp_path,
    monkeypatch,
):
    """Failure cleanup must retain its no-follow directory authority."""
    from app.api.admin import backup

    source = tmp_path / "config-source"
    source.mkdir()
    (source / "settings.json").write_text("{}", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    outside = tmp_path / "outside-cleanup"
    outside.mkdir()
    original_pending = backup_dir / ".pending-original"
    external: Path | None = None
    real_copytree = backup.shutil.copytree

    def replace_pending_then_fail(*args, **kwargs):
        nonlocal external
        pending = backup_dir / ".pending"
        pending.rename(original_pending)
        candidate_name = next(original_pending.iterdir()).name
        external = outside / candidate_name
        external.write_bytes(b"external")
        pending.symlink_to(outside, target_is_directory=True)
        raise RuntimeError("injected backup failure")

    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setenv("APP_CONFIG_ROOT", str(source))
    monkeypatch.setattr(backup.shutil, "copytree", replace_pending_then_fail)
    try:
        with pytest.raises(RuntimeError, match="injected backup failure"):
            backup._create_backup_sync(
                {"contents": ["app-config"]},
                publish=False,
                candidate_token=(
                    "00000000-0000-0000-0000-000000000005-attempt-5"
                ),
            )
    finally:
        monkeypatch.setattr(backup.shutil, "copytree", real_copytree)

    assert external is not None
    assert external.read_bytes() == b"external"
    assert list(original_pending.iterdir()) == []


API_PREFIX = "offline_restore_"


def test_restore_receipt_reader_rejects_symlink(tmp_path):
    from app.services.offline_restore import (
        RestoreValidationError,
        _atomic_json,
        _token_hash,
        read_restore_receipt,
    )

    request_id = "00000000-0000-0000-0000-000000000091"
    token = "receipt-capability"
    staging = tmp_path / "staging"
    receipts = tmp_path / "receipts"
    session = staging / request_id
    session.mkdir(parents=True)
    receipts.mkdir()
    _atomic_json(
        session / "metadata.json",
        {
            "upload_id": request_id,
            "state": "ready",
            "token_hash": _token_hash(token),
        },
    )
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"request_id": request_id, "status": "success"}),
        encoding="utf-8",
    )
    (receipts / f"{request_id}.json").symlink_to(outside)

    with pytest.raises(RestoreValidationError, match="invalid"):
        read_restore_receipt(
            staging=staging,
            receipts=receipts,
            request_id=request_id,
            token=token,
        )


async def _seed_api_user(db, username: str, permissions: list[str]) -> None:
    from app.auth import hash_password
    from app.models.user import User

    db.add(
        User(
            username=username,
            password_hash=hash_password("hunter22"),
            is_admin=False,
            is_active=True,
            permissions=permissions,
            must_change_password=False,
        )
    )
    await db.commit()


def _api_headers(username: str) -> dict[str, str]:
    from app.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(username, must_change_password=False)}"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_api_is_authorized_isolated_and_dispatches_validation_task(tmp_path, monkeypatch):
    """Bypassing system auth/token isolation or inline validation breaks this flow."""
    from app.api.admin import backup
    from app.database import async_session, engine
    from app.main import app
    from app.models import TaskEvent, TaskRun
    from app.services import operations

    monkeypatch.setenv("RESTORE_STAGING_ROOT", str(tmp_path / "staging"))
    monkeypatch.setenv("RESTORE_RECEIPTS_ROOT", str(tmp_path / "receipts"))
    monkeypatch.setattr(
        operations,
        "_fetch_admin_rq",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        operations,
        "_enqueue_admin_rq",
        lambda *_args, rq_job_id, **_kwargs: SimpleNamespace(id=rq_job_id),
    )
    archive = _archive_bytes({"database.sql": b"select 1;"})
    body = {
        "filename": "auto-gallery-backup_20260824_120000.tar.gz",
        "size_bytes": len(archive),
        "sha256": _sha256(archive),
        "chunk_size": 16 * 1024,
        "total_chunks": 1,
    }
    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            await db.execute(
                text("DELETE FROM users WHERE username LIKE :prefix"),
                {"prefix": f"{API_PREFIX}%"},
            )
            await db.commit()
            await _seed_api_user(db, f"{API_PREFIX}system", ["system"])
            await _seed_api_user(db, f"{API_PREFIX}tasks", ["tasks"])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/api/v1/admin/backup/restore/uploads", json=body)).status_code == 401
            assert (
                await client.post(
                    "/api/v1/admin/backup/restore/uploads",
                    json=body,
                    headers=_api_headers(f"{API_PREFIX}tasks"),
                )
            ).status_code == 403
            created_response = await client.post(
                "/api/v1/admin/backup/restore/uploads",
                json=body,
                headers=_api_headers(f"{API_PREFIX}system"),
            )
            assert created_response.status_code == 201
            created = created_response.json()
            upload_path = f"/api/v1/admin/backup/restore/uploads/{created['upload_id']}"
            assert (
                await client.get(
                    upload_path,
                    headers={
                        **_api_headers(f"{API_PREFIX}system"),
                        "X-Restore-Token": "wrong",
                    },
                )
            ).status_code == 403
            chunk_response = await client.put(
                f"{upload_path}/chunks/0",
                content=archive,
                headers={
                    **_api_headers(f"{API_PREFIX}system"),
                    "Content-Type": "application/octet-stream",
                    "X-Restore-Token": created["upload_token"],
                    "X-Chunk-SHA256": _sha256(archive),
                },
            )
            assert chunk_response.status_code == 200
            accepted_response = await client.post(
                f"{upload_path}/validate",
                headers={
                    **_api_headers(f"{API_PREFIX}system"),
                    "X-Restore-Token": created["upload_token"],
                },
            )
            assert accepted_response.status_code == 202
            accepted = accepted_response.json()
            assert accepted["operation_type"] == "admin-restore-validate"
            assert set(accepted) == {"task_id", "job_id", "status", "operation_type"}
            resumed_validation = await client.post(
                f"{upload_path}/validate",
                headers={
                    **_api_headers(f"{API_PREFIX}system"),
                    "X-Restore-Token": created["upload_token"],
                },
            )
            assert resumed_validation.status_code == 202
            assert resumed_validation.json() == accepted

        from app.jobs.admin_operations import _run_registered_admin_operation

        result = await _run_registered_admin_operation(accepted["task_id"], 1)
        assert result["state"] == "ready"
        assert result["request_id"] == created["upload_id"]
        async with async_session() as db:
            task = await db.get(TaskRun, UUID(accepted["task_id"]))
            assert task.status == "complete"
            options = task.meta["admin_dispatch"]["options"]
            assert options == {"upload_id": created["upload_id"]}
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [None, 400])
async def test_restore_chunk_stream_stops_at_cumulative_limit_without_buffering(
    tmp_path,
    monkeypatch,
    declared_length,
):
    """Missing or dishonest length cannot make FastAPI buffer the full body."""
    from app.database import async_session, engine
    from app.main import app
    from app.models import TaskEvent, TaskRun
    from app.services import offline_restore

    monkeypatch.setenv("RESTORE_STAGING_ROOT", str(tmp_path / "staging"))
    archive = b"123456789"
    created = offline_restore.create_upload_session(
        root=tmp_path / "staging",
        filename="auto-gallery-backup_20260824_120000.tar.gz",
        size_bytes=len(archive),
        sha256=_sha256(archive),
        chunk_size=len(archive),
        total_chunks=1,
    )
    monkeypatch.setattr(offline_restore, "MAX_CHUNK_SIZE", 8)
    username = f"{API_PREFIX}stream"
    yielded = 0

    async def oversized_body():
        nonlocal yielded
        for _ in range(100):
            yielded += 1
            yield b"1234"
            await asyncio.sleep(0)

    headers = {
        **_api_headers(username),
        "Content-Type": "application/octet-stream",
        "X-Restore-Token": created["upload_token"],
        "X-Chunk-SHA256": _sha256(archive),
    }
    if declared_length is not None:
        headers["Content-Length"] = str(declared_length)
    try:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            await db.execute(
                text("DELETE FROM users WHERE username = :username"),
                {"username": username},
            )
            await db.commit()
            await _seed_api_user(db, username, ["system"])

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.put(
                (
                    "/api/v1/admin/backup/restore/uploads/"
                    f"{created['upload_id']}/chunks/0"
                ),
                content=oversized_body(),
                headers=headers,
            )

        assert response.status_code == 413
        assert yielded <= (0 if declared_length is not None else 3)
        chunks = list(
            (tmp_path / "staging" / created["upload_id"] / "chunks").iterdir()
        )
        assert chunks == []
    finally:
        async with async_session() as db:
            await db.execute(
                text("DELETE FROM users WHERE username = :username"),
                {"username": username},
            )
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_validation_prepare_attachment_adopts_exact_task_after_loss_and_redis_outage(
    tmp_path,
    monkeypatch,
):
    """No committed validation intent may become orphaned from its upload scope."""
    from app.database import async_session, engine
    from app.main import app
    from app.models import TaskEvent, TaskRun
    from app.services import offline_restore, operations

    monkeypatch.setenv("RESTORE_STAGING_ROOT", str(tmp_path / "staging"))
    monkeypatch.setenv("RESTORE_RECEIPTS_ROOT", str(tmp_path / "receipts"))
    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_args, **_kwargs: None)

    def unavailable_enqueue(*_args, **_kwargs):
        raise RuntimeError("redis publication unavailable")

    monkeypatch.setattr(operations, "_enqueue_admin_rq", unavailable_enqueue)
    archive = _archive_bytes({"database.sql": b"select 1;"})
    created = _new_session(tmp_path / "staging", archive, chunk_size=len(archive))
    _upload_all(
        tmp_path / "staging",
        created,
        archive,
        chunk_size=len(archive),
    )
    username = f"{API_PREFIX}attach"
    original_seal = offline_restore.seal_upload_for_validation
    first_attach = True

    def fail_first_attach(**kwargs):
        nonlocal first_attach
        if first_attach:
            first_attach = False
            raise RuntimeError("injected commit-to-attach boundary")
        return original_seal(**kwargs)

    monkeypatch.setattr(offline_restore, "seal_upload_for_validation", fail_first_attach)
    path = (
        "/api/v1/admin/backup/restore/uploads/"
        f"{created['upload_id']}/validate"
    )
    headers = {
        **_api_headers(username),
        "X-Restore-Token": created["upload_token"],
    }
    try:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            await db.execute(
                text("DELETE FROM users WHERE username = :username"),
                {"username": username},
            )
            await db.commit()
            await _seed_api_user(db, username, ["system"])

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            first = await client.post(path, headers=headers)
            assert first.status_code == 500

            accepted_response = await client.post(path, headers=headers)
            assert accepted_response.status_code == 202
            accepted = accepted_response.json()

            # Simulate durable filesystem response/attachment loss after the
            # database accepted this exact scope. A remounted request adopts it.
            metadata_path = (
                tmp_path
                / "staging"
                / created["upload_id"]
                / "metadata.json"
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.pop("validation_task_id", None)
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            adopted_response = await client.post(path, headers=headers)
            assert adopted_response.status_code == 202
            assert adopted_response.json() == accepted

        session = offline_restore.get_upload_session(
            root=tmp_path / "staging",
            upload_id=created["upload_id"],
            token=created["upload_token"],
        )
        assert session["validation_task_id"] == accepted["task_id"]
        async with async_session() as db:
            tasks = list(
                (
                    await db.execute(
                        select(TaskRun).where(
                            TaskRun.kind == "admin",
                            TaskRun.operation_type == "admin-restore-validate",
                        )
                    )
                ).scalars()
            )
            assert [str(task.id) for task in tasks] == [accepted["task_id"]]
            assert tasks[0].meta[operations.ADMIN_DISPATCH_META_KEY][
                "publication_state"
            ] == operations.ADMIN_DISPATCH_PENDING

        # A ready filesystem handoff whose TaskRun row was externally lost can
        # be reattached to a fresh durable validation attempt without upload.
        session_dir = tmp_path / "staging" / created["upload_id"]
        metadata_path = session_dir / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["state"] = "ready"
        metadata["validation_task_id"] = accepted["task_id"]
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        ready_path = session_dir / "ready-request.json"
        ready_path.write_text("{}", encoding="utf-8")
        ready_path.chmod(0o444)
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(
                delete(TaskRun).where(TaskRun.id == UUID(accepted["task_id"]))
            )
            await db.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            replacement_response = await client.post(path, headers=headers)
        assert replacement_response.status_code == 202
        replacement = replacement_response.json()
        assert replacement["task_id"] != accepted["task_id"]
        assert not ready_path.exists()
        recovered = offline_restore.get_upload_session(
            root=tmp_path / "staging",
            upload_id=created["upload_id"],
            token=created["upload_token"],
        )
        assert recovered["state"] == "validating"
        assert recovered["validation_task_id"] == replacement["task_id"]
    finally:
        async with async_session() as db:
            await db.execute(delete(TaskEvent))
            await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
            await db.execute(
                text("DELETE FROM users WHERE username = :username"),
                {"username": username},
            )
            await db.commit()
        await engine.dispose()


def test_component_only_archive_is_downloadable_but_never_restore_ready(
    tmp_path,
    monkeypatch,
):
    """Host restore always switches PostgreSQL, so readiness requires a DB dump."""
    from app.api.admin import backup
    from app.services.offline_restore import RestoreValidationError, validate_upload

    archive = _archive_bytes(
        {"app-config/config.json": b"{}"},
        contents=["app-config"],
    )
    created = _new_session(tmp_path / "staging", archive, chunk_size=len(archive))
    _upload_all(
        tmp_path / "staging",
        created,
        archive,
        chunk_size=len(archive),
    )
    with pytest.raises(RestoreValidationError, match="database|restorable"):
        validate_upload(
            root=tmp_path / "staging",
            upload_id=created["upload_id"],
            task_id="00000000-0000-0000-0000-000000000099",
        )

    backup_root = tmp_path / "backups"
    app_config = tmp_path / "app-config"
    app_config.mkdir()
    (app_config / "config.json").write_text("{}", encoding="utf-8")
    original_backup_dir = backup.BACKUP_DIR
    try:
        backup.BACKUP_DIR = backup_root
        monkeypatch.setenv("APP_CONFIG_ROOT", str(app_config))
        result = backup._create_backup_sync({"contents": ["app-config"]})
        assert result["restorable"] is False
        assert (backup_root / result["filename"]).is_file()
    finally:
        backup.BACKUP_DIR = original_backup_dir
