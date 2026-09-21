"""Private download authentication must never cross a durable boundary."""

from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path

import pytest


def test_backup_estimate_and_archive_exclude_auth_runtime_files(tmp_path, monkeypatch):
    """A concurrent auth overlay must be absent from both backup surfaces."""

    from app.api.admin import backup

    gallerydl_root = tmp_path / "gallerydl"
    jobs_root = gallerydl_root / "jobs"
    jobs_root.mkdir(parents=True)
    durable = gallerydl_root / "config.json"
    durable.write_bytes(b'{"extractor":{}}')
    secret = "private-auth-backup-boundary-canary"
    (jobs_root / "auth-abandoned.json").write_text(secret, encoding="utf-8")

    backup_root = tmp_path / "backups"
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_root)
    monkeypatch.setenv("GALLERYDL_CONFIG_ROOT", str(gallerydl_root))

    estimate = backup._estimate_component_sizes()
    assert estimate["gallerydl-config"] == durable.stat().st_size

    result = backup._create_backup_sync({"contents": ["gallerydl-config"]})
    archive_path = backup_root / result["filename"]
    with tarfile.open(archive_path, "r:gz") as archive:
        names = archive.getnames()
        payload = b"".join(
            archive.extractfile(name).read()
            for name in names
            if archive.getmember(name).isfile()
        )
    assert not any(name.startswith("gallerydl-config/jobs") for name in names)
    assert secret.encode() not in payload


def test_runtime_auth_root_is_private_nonpersistent_and_outside_backup_roots(
    tmp_path,
    monkeypatch,
):
    """The secret root must be 0700 on tmpfs and outside every durable root."""

    from app.services import personal_auth_storage

    secret_root = tmp_path / "run" / "auto-gallery-secrets"
    monkeypatch.setattr(personal_auth_storage, "_filesystem_type", lambda _path: "tmpfs")
    monkeypatch.setattr(
        personal_auth_storage,
        "_durable_roots",
        lambda: (
            tmp_path / "gallerydl",
            tmp_path / "app-config",
            tmp_path / "downloads",
            tmp_path / "library",
        ),
    )

    prepared = personal_auth_storage.prepare_personal_auth_root(secret_root)

    assert prepared == secret_root.resolve()
    assert os.stat(prepared).st_mode & 0o777 == 0o700

    with pytest.raises(personal_auth_storage.PersonalAuthStorageError):
        personal_auth_storage.prepare_personal_auth_root(
            tmp_path / "gallerydl" / "jobs"
        )


def test_abandoned_sweep_removes_crash_file_but_preserves_live_concurrent_file(
    tmp_path,
    monkeypatch,
):
    """Pre-job recovery removes SIGKILL debris without breaking another worker."""

    from app.services import personal_auth_storage

    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    current_ticks = personal_auth_storage._process_start_ticks(os.getpid())
    assert current_ticks is not None
    live = root / f"auth-{os.getpid()}-{current_ticks}-live.json"
    abandoned = root / "auth-999999999-1-crashed.json"
    legacy = root / "auth-legacy-abandoned.json"
    unrelated = root / "operator-note"
    for path in (live, abandoned, legacy, unrelated):
        path.write_text("private-auth-sweep-canary", encoding="utf-8")
    monkeypatch.setattr(personal_auth_storage, "_filesystem_type", lambda _path: "tmpfs")
    monkeypatch.setattr(personal_auth_storage, "_durable_roots", lambda: ())

    removed = personal_auth_storage.sweep_abandoned_personal_auth_configs(root)

    assert removed == 2
    assert live.exists()
    assert unrelated.exists()
    assert not abandoned.exists()
    assert not legacy.exists()


def test_private_config_creation_uses_0600_and_never_serializes_path(tmp_path, monkeypatch):
    """Runtime materialization exposes only a cleanup-owned handle in memory."""

    from app.services import personal_auth_storage

    monkeypatch.setattr(personal_auth_storage, "_filesystem_type", lambda _path: "tmpfs")
    monkeypatch.setattr(personal_auth_storage, "_durable_roots", lambda: ())
    secret = "private-auth-file-mode-canary"

    path = personal_auth_storage.write_personal_auth_config(
        tmp_path / "secrets",
        job_id="00000000-0000-0000-0000-000000000001",
        payload={"extractor": {"pixiv": {"refresh-token": secret}}},
    )
    try:
        assert os.stat(path).st_mode & 0o777 == 0o600
        assert secret in path.read_text(encoding="utf-8")
        assert str(path) not in json.dumps({"job_id": "00000000-0000-0000-0000-000000000001"})
    finally:
        personal_auth_storage.remove_personal_auth_config(path)
    assert not path.exists()


def test_download_worker_startup_sweeps_auth_crash_debris(monkeypatch):
    """Only a download supervisor owns the startup credential sweep."""

    import worker_entrypoint
    from app.services import personal_auth_storage

    calls: list[str] = []
    monkeypatch.setattr(
        personal_auth_storage,
        "sweep_abandoned_personal_auth_configs",
        lambda: calls.append("sweep") or 1,
    )

    assert worker_entrypoint._sweep_personal_auth_startup(["downloads:pixiv"]) == 1
    assert worker_entrypoint._sweep_personal_auth_startup(["imports"]) == 0
    assert calls == ["sweep"]
