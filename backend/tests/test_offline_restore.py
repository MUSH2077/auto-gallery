"""Staged offline restore trust-boundary regressions."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive_bytes(
    files: dict[str, bytes],
    *,
    manifest_entries: dict[str, dict[str, object]] | None = None,
    contents: list[str] | None = None,
    extra_members: list[tarfile.TarInfo] | None = None,
) -> bytes:
    entries = manifest_entries or {name: {"size": len(data), "sha256": _sha256(data)} for name, data in files.items()}
    manifest = json.dumps(
        {
            "version": "0.3.0",
            "created_at": "20260824_120000",
            "contents": contents or ["database"],
            "component_sizes": {"database": len(files.get("database.sql", b""))},
            "entries": entries,
            "total_uncompressed_bytes": sum(int(entry["size"]) for entry in entries.values()),
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


API_PREFIX = "offline_restore_"


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
