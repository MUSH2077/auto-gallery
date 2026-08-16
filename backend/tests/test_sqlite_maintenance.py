def test_file_index_is_excluded_from_scheduled_sqlite_maintenance(tmp_path, monkeypatch):
    from app.jobs import sqlite_maintenance

    archive = tmp_path / "archive-pixiv.sqlite3"
    archive.write_bytes(b"archive")
    file_index = tmp_path / ".file-index.sqlite3"
    file_index.write_bytes(b"index")
    monkeypatch.setattr(sqlite_maintenance.settings, "download_root", str(tmp_path))

    assert sqlite_maintenance._database_paths() == [archive]
