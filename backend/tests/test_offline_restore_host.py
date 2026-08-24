"""Behavioral tests for the non-interactive host restore executable."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


PHASES = [
    "validate_request",
    "stop_writers",
    "snapshot",
    "temp_database",
    "migrate",
    "integrity",
    "switch_database",
    "restore_files",
    "clear_redis",
    "restart_foreground",
    "restart_background",
    "receipt_success",
]


def _write_fake_compose(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys, time
log = pathlib.Path(os.environ["FAKE_COMPOSE_LOG"])
args = sys.argv[1:]
with log.open("a", encoding="utf-8") as output:
    output.write(json.dumps(args) + "\\n")
if os.environ.get("FAKE_COMPOSE_SLEEP") and "stop" in args:
    time.sleep(float(os.environ["FAKE_COMPOSE_SLEEP"]))
if "--set=offline_restore_phase=rollback-identities" in args:
    sys.stdout.write(os.environ.get("FAKE_DATABASE_IDENTITIES", ""))
elif any("pg_dump" in arg for arg in args):
    sys.stdout.buffer.write(b"disposable-postgres-snapshot")
failure = os.environ.get("FAKE_COMPOSE_FAIL_CONTAINS")
if failure and failure in " ".join(args):
    sys.exit(1)
sys.exit(0)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


def _fixture(tmp_path: Path):
    project = tmp_path / "project"
    staging = tmp_path / "staging"
    receipts = tmp_path / "receipts"
    request_id = "00000000-0000-0000-0000-000000000123"
    session = staging / request_id
    payload = session / "payload"
    live_app = project / "data/config/app"
    live_gallery = project / "data/config/gallery-dl"
    downloads = project / "data/downloads"
    library = project / "data/library"
    for directory in (payload / "app-config", payload / "gallerydl-config"):
        directory.mkdir(parents=True)
    for directory in (live_app, live_gallery, downloads, library, receipts):
        directory.mkdir(parents=True)
    (live_app / "value.txt").write_text("old-app", encoding="utf-8")
    (live_gallery / "value.txt").write_text("old-gallery", encoding="utf-8")
    (payload / "app-config/value.txt").write_text("new-app", encoding="utf-8")
    (payload / "gallerydl-config/value.txt").write_text("new-gallery", encoding="utf-8")
    (payload / "database.dump").write_text("select 1;", encoding="utf-8")
    archive = session / "archive.tar.gz"
    archive.write_bytes(b"validated-archive")
    entries = {
        "database.dump": {
            "size": 9,
            "sha256": hashlib.sha256(b"select 1;").hexdigest(),
        },
        "app-config/value.txt": {
            "size": 7,
            "sha256": hashlib.sha256(b"new-app").hexdigest(),
        },
        "gallerydl-config/value.txt": {
            "size": 11,
            "sha256": hashlib.sha256(b"new-gallery").hexdigest(),
        },
    }
    request = {
        "version": 1,
        "request_id": request_id,
        "upload_id": request_id,
        "task_id": "00000000-0000-0000-0000-000000000456",
        "archive": "archive.tar.gz",
        "payload": "payload",
        "archive_size": archive.stat().st_size,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "manifest": {
            "version": "0.3.0",
            "contents": ["database", "app-config", "gallerydl-config"],
            "entries": entries,
            "total_uncompressed_bytes": sum(item["size"] for item in entries.values()),
        },
        "validated_at": "2026-08-24T12:00:00+00:00",
    }
    request_path = session / "ready-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    request_path.chmod(0o444)
    fake = tmp_path / "fake-compose.py"
    _write_fake_compose(fake)
    log = tmp_path / "compose.jsonl"
    env = {
        **os.environ,
        "PROJECT_ROOT": str(project),
        "RESTORE_STAGING_ROOT": str(staging),
        "RESTORE_RECEIPTS_ROOT": str(receipts),
        "RESTORE_COMPOSE_COMMAND": str(fake),
        "FAKE_COMPOSE_LOG": str(log),
        "HOST_CONFIG_APP": str(live_app),
        "HOST_CONFIG_GALLERYDL": str(live_gallery),
        "HOST_DOWNLOADS": str(downloads),
        "HOST_LIBRARY": str(library),
        "POSTGRES_DB": "autogallery",
        "FAKE_DATABASE_IDENTITIES": "autogallery\nag_rollback_000000000000\n",
    }
    script = Path(__file__).parents[2] / "scripts/offline-restore.py"
    return {
        "script": script,
        "request": request_path,
        "request_id": request_id,
        "receipts": receipts,
        "live_app": live_app,
        "live_gallery": live_gallery,
        "log": log,
        "env": env,
    }


def _run(fixture, *, fail_phase: str | None = None):
    env = dict(fixture["env"])
    if fail_phase:
        env["RESTORE_FAIL_PHASE"] = fail_phase
    return subprocess.run(
        [sys.executable, str(fixture["script"]), "--request", str(fixture["request"])],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _commands(fixture) -> list[list[str]]:
    if not fixture["log"].exists():
        return []
    return [json.loads(line) for line in fixture["log"].read_text().splitlines()]


def test_success_runs_every_offline_phase_and_writes_immutable_receipt(tmp_path):
    """Skipping a real host phase or writing a mutable/in-tree receipt breaks this."""
    fixture = _fixture(tmp_path)
    result = _run(fixture)
    assert result.returncode == 0, result.stderr
    assert (fixture["live_app"] / "value.txt").read_text() == "new-app"
    assert (fixture["live_gallery"] / "value.txt").read_text() == "new-gallery"

    receipt_path = fixture["receipts"] / f"{fixture['request_id']}.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "success"
    assert receipt["phase"] == "complete"
    assert receipt["rollback_performed"] is False
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o444
    assert not str(receipt_path).startswith(str(fixture["request"].parent))

    commands = _commands(fixture)
    flattened = [" ".join(command) for command in commands]
    assert any("stop" in command and "worker-download" in command for command in flattened)
    assert any("pg_dump" in command for command in flattened)
    assert any("CREATE DATABASE" in command for command in flattened)
    assert any("pg_restore" in command for command in flattened)
    assert any("alembic upgrade head" in command for command in flattened)
    assert any("integrity" in command for command in flattened)
    assert any("ALTER DATABASE" in command for command in flattened)
    assert any("FLUSHDB" in command for command in flattened)
    assert any("up" in command and "scheduler" in command for command in flattened)
    database_create = next(command for command in flattened if "CREATE DATABASE" in command)
    database_drop = next(command for command in flattened if "DROP DATABASE" in command)
    assert database_create != database_drop

    rollback = Path(receipt["rollback_command"])
    assert rollback.is_file()
    assert os.access(rollback, os.X_OK)
    rolled_back = subprocess.run(
        [str(rollback)],
        env=fixture["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert (fixture["live_app"] / "value.txt").read_text() == "old-app"
    assert (fixture["live_gallery"] / "value.txt").read_text() == "old-gallery"


@pytest.mark.parametrize("phase", PHASES)
def test_failure_at_every_phase_rolls_back_and_starts_foreground_only(tmp_path, phase):
    """Any phase exception must restore originals and keep background writers down."""
    fixture = _fixture(tmp_path)
    result = _run(fixture, fail_phase=phase)
    assert result.returncode != 0
    assert (fixture["live_app"] / "value.txt").read_text() == "old-app"
    assert (fixture["live_gallery"] / "value.txt").read_text() == "old-gallery"
    receipt = json.loads((fixture["receipts"] / f"{fixture['request_id']}.json").read_text())
    assert receipt["status"] == "rolled_back"
    assert receipt["rollback_performed"] is True
    assert receipt["phase"] == phase
    assert receipt["diagnostic"] == "Foreground services only; background writers remain stopped."

    commands = _commands(fixture)
    recovery_starts = [command for command in commands if "up" in command and "backend" in command]
    assert recovery_starts
    last = recovery_starts[-1]
    assert "admin-web" in last and "postgres" in last and "redis" in last
    assert "scheduler" not in last
    assert not any(part.startswith("worker-") for part in last)


def test_exclusive_lock_rejects_a_second_restore(tmp_path):
    """Two host restores may never overlap even for different requests."""
    fixture = _fixture(tmp_path)
    first_env = {**fixture["env"], "FAKE_COMPOSE_SLEEP": "2"}
    first = subprocess.Popen(
        [sys.executable, str(fixture["script"]), "--request", str(fixture["request"])],
        env=first_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any("stop" in command for command in _commands(fixture)):
                break
            time.sleep(0.05)
        second = _run(fixture)
        assert second.returncode == 75
        assert "lock" in second.stderr.lower()
    finally:
        first.communicate(timeout=20)


def test_request_must_be_regular_and_contained_by_explicit_staging_root(tmp_path):
    """A symlink or sibling ready request must fail before any compose command."""
    fixture = _fixture(tmp_path)
    outside = tmp_path / "outside-ready.json"
    outside.write_text(fixture["request"].read_text())
    result = subprocess.run(
        [sys.executable, str(fixture["script"]), "--request", str(outside)],
        env=fixture["env"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert _commands(fixture) == []

    link = fixture["request"].with_name("ready-link.json")
    link.symlink_to(fixture["request"])
    linked = subprocess.run(
        [sys.executable, str(fixture["script"]), "--request", str(link)],
        env=fixture["env"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert linked.returncode != 0
    assert _commands(fixture) == []


def test_live_restore_targets_cannot_overlap_staging_control_data(tmp_path):
    """A recursive config swap must never consume its own ready request."""
    fixture = _fixture(tmp_path)
    fixture["env"]["HOST_CONFIG_APP"] = str(fixture["request"].parent)

    result = _run(fixture)

    assert result.returncode != 0
    assert fixture["request"].is_file()
    assert (fixture["live_app"] / "value.txt").read_text() == "old-app"


def test_host_rejects_payload_files_added_after_validation(tmp_path):
    """The ready handoff must not permit an unmanifested config file."""
    fixture = _fixture(tmp_path)
    payload = fixture["request"].parent / "payload"
    (payload / "app-config/injected.json").write_text("{}", encoding="utf-8")

    result = _run(fixture)

    assert result.returncode != 0
    assert not (fixture["live_app"] / "injected.json").exists()


def test_failed_atomic_database_switch_does_not_rename_the_untouched_live_database(
    tmp_path,
):
    """A switch command failure before commit must leave the sole live DB named."""
    fixture = _fixture(tmp_path)
    fixture["env"].update(
        {
            "FAKE_COMPOSE_FAIL_CONTAINS": ("ALTER DATABASE autogallery RENAME TO ag_rollback_000000000000"),
            "FAKE_DATABASE_IDENTITIES": "autogallery\n",
        }
    )

    result = _run(fixture)

    assert result.returncode != 0
    flattened = [" ".join(command) for command in _commands(fixture)]
    assert not any("RENAME TO ag_failed_000000000000" in command for command in flattened)
    assert not any("ag_rollback_000000000000 RENAME TO autogallery" in command for command in flattened)


def test_rollback_receipt_reports_a_failed_redis_restore(tmp_path):
    """A failed external Redis copy must not be reported as complete recovery."""
    fixture = _fixture(tmp_path)
    redis_snapshot = fixture["receipts"] / "rollbacks" / fixture["request_id"] / "redis-data"
    fixture["env"]["FAKE_COMPOSE_FAIL_CONTAINS"] = f"{redis_snapshot}/."

    result = _run(fixture, fail_phase="clear_redis")

    assert result.returncode != 0
    receipt = json.loads((fixture["receipts"] / f"{fixture['request_id']}.json").read_text())
    assert receipt["rollback_status"] == "failed"


def test_snapshot_does_not_follow_live_symlinks_outside_explicit_targets(tmp_path):
    """Capturing config rollback data must not widen through a live symlink."""
    fixture = _fixture(tmp_path)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do-not-copy", encoding="utf-8")
    (fixture["live_app"] / "outside-link").symlink_to(outside)

    result = _run(fixture)

    assert result.returncode == 0, result.stderr
    rollback_link = fixture["receipts"] / "rollbacks" / fixture["request_id"] / "app-config" / "outside-link"
    assert rollback_link.is_symlink()
    assert rollback_link.readlink() == outside
