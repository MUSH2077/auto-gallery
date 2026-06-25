import os
import time

from app.jobs.import_list_cleanup import cleanup_import_list, prune_import_lists


def test_cleanup_import_list_removes_specific_and_is_idempotent(tmp_path):
    f = tmp_path / "abc.json"
    f.write_text("[]")
    assert cleanup_import_list(tmp_path, "abc") is True
    assert not f.exists()
    # Second call is a no-op, not an error.
    assert cleanup_import_list(tmp_path, "abc") is False


def test_prune_removes_only_stale(tmp_path):
    old = tmp_path / "old.json"
    old.write_text("[]")
    fresh = tmp_path / "fresh.json"
    fresh.write_text("[]")
    stale_mtime = time.time() - 100_000  # > 24h
    os.utime(old, (stale_mtime, stale_mtime))

    removed = prune_import_lists(tmp_path, max_age_seconds=86_400)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_prune_missing_dir_returns_zero(tmp_path):
    assert prune_import_lists(tmp_path / "does-not-exist", 86_400) == 0
