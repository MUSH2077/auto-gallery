import os
import tarfile
from pathlib import Path

import pytest
from fastapi import HTTPException


def test_validate_backup_filename_accepts_only_backup_names(tmp_path, monkeypatch):
    from app.api import admin

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(admin, "BACKUP_DIR", backup_dir)
    expected = backup_dir / "auto-gallery-backup_20260610_120000.tar.gz"
    expected.touch()

    target = admin._validate_backup_filename("auto-gallery-backup_20260610_120000.tar.gz")

    assert target == expected.resolve()


@pytest.mark.parametrize("filename", ["../auto-gallery-backup_x.tar.gz", "/tmp/auto-gallery-backup_x.tar.gz", r"..\\auto-gallery-backup_x.tar.gz", "notes.tar.gz"])
def test_validate_backup_filename_rejects_traversal_and_non_backup_names(tmp_path, monkeypatch, filename):
    from app.api import admin

    monkeypatch.setattr(admin, "BACKUP_DIR", tmp_path)

    with pytest.raises(HTTPException) as exc:
        admin._validate_backup_filename(filename)

    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid filename"


def test_validate_backup_filename_rejects_missing_and_symlinked_files(tmp_path, monkeypatch):
    from app.api import admin

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(admin, "BACKUP_DIR", backup_dir)
    outside = tmp_path / "auto-gallery-backup_20260610_120000.tar.gz"
    outside.touch()
    (backup_dir / outside.name).symlink_to(outside)

    with pytest.raises(HTTPException) as exc:
        admin._validate_backup_filename(outside.name)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Backup not found"


def test_pg_env_uses_pgpass_file_not_pgpassword(tmp_path, monkeypatch):
    from app.api import admin

    monkeypatch.setenv("PGPASSWORD", "must-not-leak")
    env = admin._pg_env_with_passfile(str(tmp_path), {
        "host": "postgres",
        "port": "5432",
        "dbname": "autogallery",
        "user": "autogallery",
        "password": "pa:ss\\word",
    })

    pgpass = Path(env["PGPASSFILE"])
    assert "PGPASSWORD" not in env
    assert pgpass.read_text() == "postgres:5432:autogallery:autogallery:pa\\:ss\\\\word\n"
    assert oct(pgpass.stat().st_mode & 0o777) == "0o600"


def test_safe_extract_tar_skips_path_traversal_members(tmp_path):
    from app.api import admin

    tar_path = tmp_path / "backup.tar.gz"
    source = tmp_path / "source.txt"
    source.write_text("ok")
    evil = tmp_path / "evil.txt"
    evil.write_text("bad")

    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(source, arcname="manifest.json")
        tar.add(evil, arcname="../evil.txt")

    dest = tmp_path / "dest"
    dest.mkdir()
    admin._safe_extract_tar(str(tar_path), str(dest))

    assert (dest / "manifest.json").read_text() == "ok"
    assert (tmp_path / "evil.txt").read_text() == "bad"


def test_media_signatures_validate_expiry_and_size():
    from app.services.media_signing import sign_media_token, verify_media_token

    asset_id = "asset-1"
    expires = 4_102_444_800
    token = sign_media_token(asset_id, "preview", expires)

    assert verify_media_token(asset_id, "preview", expires, token) is True
    assert verify_media_token(asset_id, "original", expires, token) is False
    assert verify_media_token("asset-2", "preview", expires, token) is False
    assert verify_media_token(asset_id, "preview", 1, sign_media_token(asset_id, "preview", 1)) is False
