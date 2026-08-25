"""Behavioral tests for the non-interactive host restore executable."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest


PHASES = [
    "validate_request",
    "stop_writers",
    "freeze_inputs",
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
import json, os, pathlib, shutil, sys, time
log = pathlib.Path(os.environ["FAKE_COMPOSE_LOG"])
args = sys.argv[1:]
with log.open("a", encoding="utf-8") as output:
    output.write(json.dumps(args) + "\\n")
if os.environ.get("FAKE_COMPOSE_SLEEP") and "stop" in args:
    time.sleep(float(os.environ["FAKE_COMPOSE_SLEEP"]))
joined = " ".join(args)
mutate_on = os.environ.get("FAKE_MUTATE_PATH_ON")
if mutate_on and mutate_on in joined:
    pathlib.Path(os.environ["FAKE_MUTATE_PATH"]).write_bytes(
        os.environ.get("FAKE_MUTATE_CONTENT", "mutated!!").encode()
    )
sabotage_on = os.environ.get("FAKE_REMOVE_PATH_ON")
if sabotage_on and sabotage_on in joined:
    sabotage = pathlib.Path(os.environ["FAKE_REMOVE_PATH"])
    if sabotage.is_dir() and not sabotage.is_symlink():
        shutil.rmtree(sabotage)
    else:
        sabotage.unlink(missing_ok=True)
if "POSTGRES_USER" in joined and "POSTGRES_DB" in joined and "pg_dump" not in joined:
    sys.stdout.write(os.environ.get("FAKE_POSTGRES_IDENTITY", "autogallery\\nautogallery\\n"))
elif "--set=offline_restore_phase=rollback-identities" in args:
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
    for directory in (
        payload / "app-config",
        payload / "gallerydl-config",
        payload / "library-metadata/creator",
    ):
        directory.mkdir(parents=True)
    for directory in (live_app, live_gallery, downloads, library, receipts):
        directory.mkdir(parents=True)
    (live_app / "value.txt").write_text("old-app", encoding="utf-8")
    (live_gallery / "value.txt").write_text("old-gallery", encoding="utf-8")
    (payload / "app-config/value.txt").write_text("new-app", encoding="utf-8")
    (payload / "gallerydl-config/value.txt").write_text("new-gallery", encoding="utf-8")
    (payload / "library-metadata/creator/metadata.json").write_text('{"name":"new"}', encoding="utf-8")
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
        "library-metadata/creator/metadata.json": {
            "size": 14,
            "sha256": hashlib.sha256(b'{"name":"new"}').hexdigest(),
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
            "contents": [
                "database",
                "app-config",
                "gallerydl-config",
                "library-metadata",
            ],
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
        "library": library,
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


@pytest.mark.parametrize(
    "case",
    ["missing", "ambiguous", "unrecognized", "wrong-type", "hash-mismatch"],
)
def test_database_payload_preflight_rejects_before_writer_stop(tmp_path, case):
    """The host must prove one real dump before issuing any Compose command."""
    fixture = _fixture(tmp_path)
    request = json.loads(fixture["request"].read_text(encoding="utf-8"))
    payload = fixture["request"].parent / "payload"
    dump = payload / "database.dump"
    entries = request["manifest"]["entries"]
    original = entries["database.dump"]
    if case == "missing":
        dump.unlink()
        entries.pop("database.dump")
        request["manifest"]["total_uncompressed_bytes"] -= original["size"]
    elif case == "ambiguous":
        sql = b"select 2;"
        (payload / "database.sql").write_bytes(sql)
        entries["database.sql"] = {
            "size": len(sql),
            "sha256": hashlib.sha256(sql).hexdigest(),
        }
        request["manifest"]["total_uncompressed_bytes"] += len(sql)
    elif case == "unrecognized":
        dump.rename(payload / "database.backup")
        entries["database.backup"] = entries.pop("database.dump")
    elif case == "wrong-type":
        dump.unlink()
        dump.mkdir()
    elif case == "hash-mismatch":
        dump.write_bytes(b"select 2;")
    else:  # pragma: no cover - the literal parameter list is exhaustive
        raise AssertionError(case)
    fixture["request"].chmod(0o600)
    fixture["request"].write_text(json.dumps(request), encoding="utf-8")
    fixture["request"].chmod(0o444)

    result = _run(fixture)

    assert result.returncode == 2
    assert "database payload" in result.stderr
    assert _commands(fixture) == []
    assert not (
        fixture["receipts"] / "rollbacks" / fixture["request_id"]
    ).exists()


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
    assert receipt["rollback_components"]["redis"]["status"] == "failed"
    assert receipt["rollback_components"]["files"]["status"] == "complete"
    assert receipt["rollback_components"]["database"]["status"] == "complete"
    assert receipt["rollback_components"]["foreground"]["status"] == "complete"
    commands = _commands(fixture)
    assert any("up" in command and "backend" in command for command in commands)
    assert not any("up" in command and "scheduler" in command for command in commands)


def test_file_rollback_failure_still_attempts_database_redis_and_foreground(tmp_path):
    """One damaged file rollback must not abort independent service recovery."""
    fixture = _fixture(tmp_path)
    old_app = fixture["live_app"].with_name(f".{fixture['live_app'].name}.restore-old-{fixture['request_id']}")
    assert old_app.is_relative_to(tmp_path)
    assert not old_app.is_symlink()
    fixture["env"].update(
        {
            "FAKE_REMOVE_PATH_ON": "admin-web",
            "FAKE_REMOVE_PATH": str(old_app),
        }
    )

    result = _run(fixture, fail_phase="clear_redis")

    assert result.returncode != 0
    receipt = json.loads((fixture["receipts"] / f"{fixture['request_id']}.json").read_text())
    assert receipt["rollback_status"] == "failed"
    components = receipt["rollback_components"]
    assert components["files"]["status"] == "failed"
    assert components["database"]["status"] == "complete"
    assert components["redis"]["status"] == "complete"
    assert components["foreground"]["status"] == "complete"
    commands = [" ".join(command) for command in _commands(fixture)]
    assert any("ag_rollback_000000000000 RENAME TO autogallery" in command for command in commands)
    assert any("redis:/data" in command for command in commands)
    recovery = [command for command in _commands(fixture) if "up" in command and "backend" in command]
    assert recovery
    assert "scheduler" not in recovery[-1]
    assert not any(part.startswith("worker-") for part in recovery[-1])


def test_unproven_post_switch_database_identity_marks_recovery_failed(tmp_path):
    """A failed identity probe after switching can never count as DB recovery."""
    fixture = _fixture(tmp_path)
    fixture["env"]["FAKE_COMPOSE_FAIL_CONTAINS"] = "rollback-identities"

    result = _run(fixture, fail_phase="clear_redis")

    assert result.returncode != 0
    receipt = json.loads((fixture["receipts"] / f"{fixture['request_id']}.json").read_text())
    assert receipt["status"] == "recovery_failed"
    assert receipt["rollback_status"] == "failed"
    assert receipt["rollback_components"]["database"]["status"] == "failed"
    assert receipt["rollback_components"]["foreground"]["status"] == "complete"
    commands = _commands(fixture)
    probes = [command for command in commands if "--set=offline_restore_phase=rollback-identities" in command]
    assert len(probes) == 2
    assert any("up" in command and "postgres" in command for command in commands)
    assert not any("up" in command and "scheduler" in command for command in commands)


def test_staging_mutation_after_writer_stop_fails_before_snapshot(tmp_path):
    """Post-stop bytes, not a racy preflight hash, define the frozen restore input."""
    fixture = _fixture(tmp_path)
    staged = fixture["request"].parent / "payload/app-config/value.txt"
    fixture["env"].update(
        {
            "FAKE_MUTATE_PATH_ON": "worker-import",
            "FAKE_MUTATE_PATH": str(staged),
            "FAKE_MUTATE_CONTENT": "tampered",
        }
    )

    result = _run(fixture)

    assert result.returncode != 0
    assert (fixture["live_app"] / "value.txt").read_text() == "old-app"
    commands = [" ".join(command) for command in _commands(fixture)]
    assert not any("pg_dump" in command for command in commands)
    receipt = json.loads((fixture["receipts"] / f"{fixture['request_id']}.json").read_text())
    assert receipt["phase"] == "freeze_inputs"


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


def test_nested_live_parent_symlink_cannot_redirect_a_restore_write(tmp_path):
    """A relative payload parent must not traverse a live symlink outside its root."""
    fixture = _fixture(tmp_path)
    outside = tmp_path / "outside-library"
    outside.mkdir()
    outside_metadata = outside / "metadata.json"
    outside_metadata.write_text('{"name":"outside"}', encoding="utf-8")
    (fixture["library"] / "creator").symlink_to(outside, target_is_directory=True)

    result = _run(fixture)

    assert result.returncode != 0
    assert outside_metadata.read_text(encoding="utf-8") == '{"name":"outside"}'


def test_dangling_restore_temp_symlink_is_rejected_without_following_it(tmp_path):
    """A dangling deterministic restore-new path must never become a write target."""
    fixture = _fixture(tmp_path)
    creator = fixture["library"] / "creator"
    creator.mkdir()
    outside = tmp_path / "outside-created.json"
    temp_name = f".metadata.json.restore-new-{fixture['request_id']}"
    (creator / temp_name).symlink_to(outside)

    result = _run(fixture)

    assert result.returncode != 0
    assert not outside.exists()


def test_postgres_identity_comes_from_compose_and_every_command_is_explicit(tmp_path):
    """Host libpq variables must never silently select a host user/database."""
    fixture = _fixture(tmp_path)
    fixture["env"].update(
        {
            "POSTGRES_USER": "wrong_host_user",
            "POSTGRES_DB": "wrong_host_database",
            "FAKE_POSTGRES_IDENTITY": "compose_restore_user\ncompose_restore_db\n",
            "FAKE_DATABASE_IDENTITIES": (
                "compose_restore_db\nag_rollback_000000000000\n"
            ),
        }
    )

    result = _run(fixture)

    assert result.returncode == 0, result.stderr
    commands = _commands(fixture)
    database_commands = [
        command
        for command in commands
        if any(tool in " ".join(command) for tool in ("pg_dump", "pg_restore", "psql"))
    ]
    assert database_commands
    for command in database_commands:
        rendered = " ".join(command)
        assert "compose_restore_user" in rendered
        assert "wrong_host_user" not in rendered
    rendered_all = "\n".join(" ".join(command) for command in commands)
    assert "ALTER DATABASE compose_restore_db RENAME TO" in rendered_all
    assert "ALTER DATABASE wrong_host_database" not in rendered_all


SWAP_BOUNDARIES = [
    f"{kind}:{component}:{boundary}"
    for kind, component in (
        ("directory", "app-config"),
        ("file", "library-metadata"),
    )
    for boundary in (
        "prepared-journal",
        "old-renamed",
        "old-fsynced",
        "old-journal",
        "new-renamed",
        "new-fsynced",
        "applied-journal",
        "committed-journal",
    )
]

ROLLBACK_COMMITTED_BOUNDARIES = [
    f"{kind}:{component}:{boundary}"
    for kind, component in (
        ("directory", "app-config"),
        ("file", "library-metadata"),
    )
    for boundary in (
        "live-to-failed-intent-journal",
        "live-to-failed-renamed",
        "live-to-failed-renamed-journal",
        "live-to-failed-fsynced",
        "live-to-failed-fsynced-journal",
        "old-to-live-intent-journal",
        "old-to-live-renamed",
        "old-to-live-renamed-journal",
        "old-to-live-fsynced",
        "old-to-live-fsynced-journal",
        "failed-cleanup-intent-journal",
        "failed-cleaned",
        "failed-cleaned-journal",
        "failed-cleanup-fsynced",
        "failed-cleanup-fsynced-journal",
        "rolled-back-journal",
    )
]

ROLLBACK_NEW_CLEANUP_BOUNDARIES = [
    f"{kind}:{component}:{boundary}"
    for kind, component in (
        ("directory", "app-config"),
        ("file", "library-metadata"),
    )
    for boundary in (
        "new-cleanup-intent-journal",
        "new-cleaned",
        "new-cleaned-journal",
        "new-cleanup-fsynced",
        "new-cleanup-fsynced-journal",
    )
]

ROLLBACK_CREATED_TARGET_BOUNDARIES = [
    f"{kind}:{component}:{boundary}"
    for kind, component in (
        ("directory", "app-config"),
        ("file", "library-metadata"),
    )
    for boundary in (
        "target-cleanup-intent-journal",
        "target-cleaned",
        "target-cleaned-journal",
        "target-cleanup-fsynced",
        "target-cleanup-fsynced-journal",
    )
]


def _rollback_path(fixture) -> Path:
    return (
        fixture["receipts"]
        / "rollbacks"
        / fixture["request_id"]
        / "rollback.sh"
    )


def _run_rollback(fixture, *, boundary: str | None = None, action: str = "fail"):
    env = dict(fixture["env"])
    if boundary is not None:
        env["RESTORE_ROLLBACK_FAULT_BOUNDARY"] = boundary
        env["RESTORE_ROLLBACK_FAULT_ACTION"] = action
    return subprocess.run(
        [str(_rollback_path(fixture))],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _swap_target(fixture, kind: str) -> tuple[Path, bytes]:
    if kind == "directory":
        return fixture["live_app"], b"old-app"
    target = fixture["library"] / "creator" / "metadata.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'{"name":"old"}')
    return target, b'{"name":"old"}'


def _assert_original_restored(target: Path, kind: str, expected: bytes) -> None:
    if kind == "directory":
        assert (target / "value.txt").read_bytes() == expected
    else:
        assert target.read_bytes() == expected


def _assert_foreground_only_restart(fixture) -> None:
    starts = [
        command
        for command in _commands(fixture)
        if "up" in command and "backend" in command
    ]
    assert starts
    assert "admin-web" in starts[-1]
    assert "scheduler" not in starts[-1]
    assert not any(part.startswith("worker-") for part in starts[-1])


@pytest.mark.parametrize("action", ["fail", "kill"])
@pytest.mark.parametrize("boundary", ROLLBACK_COMMITTED_BOUNDARIES)
def test_rollback_resumes_at_every_committed_swap_boundary(
    tmp_path,
    boundary,
    action,
):
    """Every durable rollback boundary must converge on a second invocation."""
    fixture = _fixture(tmp_path)
    kind, component, _boundary_name = boundary.split(":", 2)
    target, expected = _swap_target(fixture, kind)
    applied = _run(fixture)
    assert applied.returncode == 0, applied.stderr

    interrupted = _run_rollback(fixture, boundary=boundary, action=action)
    if action == "fail":
        assert interrupted.returncode == 1, interrupted.stderr
    else:
        assert interrupted.returncode < 0, interrupted.stderr

    resumed = _run_rollback(fixture)
    assert resumed.returncode == 0, resumed.stderr
    _assert_original_restored(target, kind, expected)
    for marker in ("old", "new", "failed"):
        residual = target.with_name(
            f".{target.name}.restore-{marker}-{fixture['request_id']}"
        )
        assert not os.path.lexists(residual)
    journal = json.loads((_rollback_path(fixture).parent / "journal.json").read_text())
    swap = next(item for item in journal["file_swaps"] if item["component"] == component)
    assert swap["state"] == "rolled_back"
    assert swap["rollback_state"] == "rolled_back"
    _assert_foreground_only_restart(fixture)


@pytest.mark.parametrize("action", ["fail", "kill"])
@pytest.mark.parametrize("boundary", ROLLBACK_NEW_CLEANUP_BOUNDARIES)
def test_rollback_resumes_at_every_partial_new_cleanup_boundary(
    tmp_path,
    boundary,
    action,
):
    """A forward crash with old+new paths must survive rollback cleanup crashes."""
    fixture = _fixture(tmp_path)
    kind, component, _boundary_name = boundary.split(":", 2)
    target, expected = _swap_target(fixture, kind)
    forward_boundary = f"{kind}:{component}:old-journal"
    env = {
        **fixture["env"],
        "RESTORE_FAULT_BOUNDARY": forward_boundary,
        "RESTORE_FAULT_ACTION": "kill",
    }
    forward = subprocess.run(
        [sys.executable, str(fixture["script"]), "--request", str(fixture["request"])],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert forward.returncode < 0, forward.stderr

    interrupted = _run_rollback(fixture, boundary=boundary, action=action)
    if action == "fail":
        assert interrupted.returncode == 1, interrupted.stderr
    else:
        assert interrupted.returncode < 0, interrupted.stderr
    resumed = _run_rollback(fixture)
    assert resumed.returncode == 0, resumed.stderr
    _assert_original_restored(target, kind, expected)
    _assert_foreground_only_restart(fixture)


@pytest.mark.parametrize("action", ["fail", "kill"])
@pytest.mark.parametrize("boundary", ROLLBACK_CREATED_TARGET_BOUNDARIES)
def test_rollback_resumes_at_every_created_target_cleanup_boundary(
    tmp_path,
    boundary,
    action,
):
    """A restore-created file or directory remains removably crash-resumable."""
    fixture = _fixture(tmp_path)
    kind, _component, _boundary_name = boundary.split(":", 2)
    if kind == "directory":
        target = fixture["live_app"]
        (target / "value.txt").unlink()
        target.rmdir()
    else:
        target = fixture["library"] / "creator" / "metadata.json"
    assert not target.exists()
    applied = _run(fixture)
    assert applied.returncode == 0, applied.stderr
    assert target.exists()

    interrupted = _run_rollback(fixture, boundary=boundary, action=action)
    if action == "fail":
        assert interrupted.returncode == 1, interrupted.stderr
    else:
        assert interrupted.returncode < 0, interrupted.stderr
    resumed = _run_rollback(fixture)
    assert resumed.returncode == 0, resumed.stderr
    assert not target.exists()
    _assert_foreground_only_restart(fixture)


@pytest.mark.parametrize(
    ("kind", "component"),
    [("directory", "app-config"), ("file", "library-metadata")],
)
def test_rollback_refuses_unrelated_failed_path_and_continues_other_swaps(
    tmp_path,
    kind,
    component,
):
    """Rollback must neither overwrite an occupied failed path nor stop peers."""
    fixture = _fixture(tmp_path)
    target, expected = _swap_target(fixture, kind)
    applied = _run(fixture)
    assert applied.returncode == 0, applied.stderr
    old = target.with_name(f".{target.name}.restore-old-{fixture['request_id']}")
    failed = target.with_name(f".{target.name}.restore-failed-{fixture['request_id']}")
    sentinel = b"unrelated-path"
    if kind == "directory":
        failed.mkdir()
        (failed / "sentinel").write_bytes(sentinel)
    else:
        failed.write_bytes(sentinel)

    result = _run_rollback(fixture)

    assert result.returncode == 1
    relative = (
        target.name
        if kind == "directory"
        else target.relative_to(fixture["library"]).as_posix()
    )
    assert f"{component}:{relative}" in result.stderr
    assert "already exists" in result.stderr
    assert old.exists()
    if kind == "directory":
        assert (failed / "sentinel").read_bytes() == sentinel
        assert (target / "value.txt").read_bytes() != expected
        assert (fixture["live_gallery"] / "value.txt").read_text() == "old-gallery"
    else:
        assert failed.read_bytes() == sentinel
        assert target.read_bytes() != expected
        assert (fixture["live_app"] / "value.txt").read_text() == "old-app"
    _assert_foreground_only_restart(fixture)


def _replace_rollback_slot_with_sentinel(path: Path, kind: str) -> bytes:
    sentinel = b"unrelated-replacement"
    assert os.path.lexists(path)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    if kind == "directory":
        path.mkdir()
        (path / "sentinel").write_bytes(sentinel)
    else:
        path.write_bytes(sentinel)
    return sentinel


@pytest.mark.parametrize(
    ("kind", "component"),
    [("directory", "app-config"), ("file", "library-metadata")],
)
@pytest.mark.parametrize("slot", ["target", "new", "failed", "created-target"])
def test_rollback_refuses_replaced_candidate_at_every_mutable_slot(
    tmp_path,
    kind,
    component,
    slot,
):
    """A deterministic rollback name never authorizes deletion of a new inode."""
    fixture = _fixture(tmp_path)
    target, _expected = _swap_target(fixture, kind)

    if slot == "created-target":
        if kind == "directory":
            shutil.rmtree(target)
        else:
            target.unlink()
        applied = _run(fixture)
        assert applied.returncode == 0, applied.stderr
        tampered = target
    elif slot == "new":
        boundary = f"{kind}:{component}:old-journal"
        env = {
            **fixture["env"],
            "RESTORE_FAULT_BOUNDARY": boundary,
            "RESTORE_FAULT_ACTION": "kill",
        }
        interrupted = subprocess.run(
            [sys.executable, str(fixture["script"]), "--request", str(fixture["request"])],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert interrupted.returncode < 0, interrupted.stderr
        tampered = target.with_name(
            f".{target.name}.restore-new-{fixture['request_id']}"
        )
    else:
        applied = _run(fixture)
        assert applied.returncode == 0, applied.stderr
        if slot == "failed":
            boundary = f"{kind}:{component}:old-to-live-fsynced-journal"
            interrupted = _run_rollback(fixture, boundary=boundary)
            assert interrupted.returncode == 1, interrupted.stderr
            tampered = target.with_name(
                f".{target.name}.restore-failed-{fixture['request_id']}"
            )
        else:
            tampered = target

    sentinel = _replace_rollback_slot_with_sentinel(tampered, kind)

    result = _run_rollback(fixture)

    assert result.returncode == 1
    assert "mutation source changed" in result.stderr
    if kind == "directory":
        assert (tampered / "sentinel").read_bytes() == sentinel
    else:
        assert tampered.read_bytes() == sentinel
    _assert_foreground_only_restart(fixture)


@pytest.mark.parametrize("action", ["fail", "kill"])
@pytest.mark.parametrize("boundary", SWAP_BOUNDARIES)
def test_every_filesystem_swap_boundary_has_deterministic_recovery(
    tmp_path,
    boundary,
    action,
):
    """Prepared/applied journal states make each rename/fsync crash recoverable."""
    fixture = _fixture(tmp_path)
    fixture["env"].update(
        {
            "RESTORE_FAULT_BOUNDARY": boundary,
            "RESTORE_FAULT_ACTION": action,
        }
    )

    result = _run(fixture)

    if action == "fail":
        assert result.returncode == 1, result.stderr
    else:
        assert result.returncode < 0, result.stderr
        rollback_dir = (
            fixture["receipts"] / "rollbacks" / fixture["request_id"]
        )
        assert rollback_dir.is_relative_to(tmp_path)
        rollback = subprocess.run(
            [str(rollback_dir / "rollback.sh")],
            env=fixture["env"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert rollback.returncode == 0, rollback.stderr
    assert (fixture["live_app"] / "value.txt").read_text() == "old-app"
    assert (fixture["live_gallery"] / "value.txt").read_text() == "old-gallery"
    assert not (fixture["library"] / "creator" / "metadata.json").exists()


def test_file_rollback_aggregates_swap_errors_and_continues_earlier_records(tmp_path):
    """A broken later swap cannot prevent an earlier independent swap rollback."""
    fixture = _fixture(tmp_path)
    result = _run(fixture)
    assert result.returncode == 0, result.stderr
    old_gallery = fixture["live_gallery"].with_name(
        f".{fixture['live_gallery'].name}.restore-old-{fixture['request_id']}"
    )
    assert old_gallery.is_relative_to(tmp_path)
    assert old_gallery.is_dir()
    for child in old_gallery.iterdir():
        assert child.is_relative_to(tmp_path)
        child.unlink()
    old_gallery.rmdir()

    rollback = subprocess.run(
        [
            str(
                fixture["receipts"]
                / "rollbacks"
                / fixture["request_id"]
                / "rollback.sh"
            )
        ],
        env=fixture["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert rollback.returncode == 1
    assert (fixture["live_app"] / "value.txt").read_text() == "old-app"
    assert (fixture["live_gallery"] / "value.txt").read_text() == "new-gallery"


@pytest.mark.integration
def test_real_disposable_postgres_redis_switch_and_snapshot_rollback(tmp_path):
    """Exercise host switch/rollback against isolated non-default real services."""

    if os.environ.get("RUN_REAL_OFFLINE_RESTORE_INTEGRATION") != "1":
        pytest.skip("set RUN_REAL_OFFLINE_RESTORE_INTEGRATION=1 for Docker services")

    suffix = f"{os.getpid()}_{uuid4().hex[:8]}"
    pg_container = f"ag_finalfix_host_pg_{suffix}"
    redis_container = f"ag_finalfix_host_redis_{suffix}"
    assert pg_container.startswith("ag_finalfix_host_pg_")
    assert redis_container.startswith("ag_finalfix_host_redis_")
    assert "/" not in pg_container and "/" not in redis_container
    pg_user = "compose_restore_user"
    pg_password = "disposable_restore_password"
    live_database = "compose_restore_db"

    def command(*args, **kwargs):
        return subprocess.run(args, check=True, capture_output=True, **kwargs)

    subprocess.run(
        [
            "docker", "run", "--detach", "--name", pg_container,
            "--env", f"POSTGRES_USER={pg_user}",
            "--env", f"POSTGRES_PASSWORD={pg_password}",
            "--env", f"POSTGRES_DB={live_database}",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["docker", "run", "--detach", "--name", redis_container, "redis:7-alpine"],
        check=True,
        capture_output=True,
    )
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            ready = subprocess.run(
                [
                    "docker", "exec", pg_container, "pg_isready",
                    "--username", pg_user, "--dbname", live_database,
                ],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")

        command(
            "docker", "exec", pg_container, "psql",
            "--username", pg_user, "--dbname", live_database,
            "--set", "ON_ERROR_STOP=1", "--command",
            (
                "CREATE TABLE restore_marker(value text NOT NULL);"
                "INSERT INTO restore_marker VALUES ('old');"
                "CREATE TABLE alembic_version(version_num varchar(32) NOT NULL);"
                "INSERT INTO alembic_version VALUES ('disposable');"
            ),
        )
        command(
            "docker", "exec", pg_container, "createdb",
            "--username", pg_user, "desired_restore",
        )
        command(
            "docker", "exec", pg_container, "psql",
            "--username", pg_user, "--dbname", "desired_restore",
            "--set", "ON_ERROR_STOP=1", "--command",
            (
                "CREATE TABLE restore_marker(value text NOT NULL);"
                "INSERT INTO restore_marker VALUES ('new');"
                "CREATE TABLE alembic_version(version_num varchar(32) NOT NULL);"
                "INSERT INTO alembic_version VALUES ('disposable');"
            ),
        )
        command(
            "docker", "exec", redis_container,
            "redis-cli", "SET", "restore:marker", "old",
        )

        fixture = _fixture(tmp_path)
        payload_dump = fixture["request"].parent / "payload/database.dump"
        with payload_dump.open("wb") as output:
            subprocess.run(
                [
                    "docker", "exec", pg_container, "pg_dump",
                    "--username", pg_user, "--dbname", "desired_restore",
                    "--format=custom", "--no-owner", "--no-acl",
                ],
                check=True,
                stdout=output,
            )
        request = json.loads(fixture["request"].read_text(encoding="utf-8"))
        old_size = request["manifest"]["entries"]["database.dump"]["size"]
        request["manifest"]["entries"]["database.dump"] = {
            "size": payload_dump.stat().st_size,
            "sha256": hashlib.sha256(payload_dump.read_bytes()).hexdigest(),
        }
        request["manifest"]["total_uncompressed_bytes"] += (
            payload_dump.stat().st_size - old_size
        )
        fixture["request"].chmod(0o600)
        fixture["request"].write_text(json.dumps(request), encoding="utf-8")
        fixture["request"].chmod(0o444)

        shim = tmp_path / "real-compose-shim.py"
        shim.write_text(
            """#!/usr/bin/env python3
import os, subprocess, sys
args = sys.argv[1:]
pg = os.environ["REAL_PG_CONTAINER"]
redis = os.environ["REAL_REDIS_CONTAINER"]
if args[:3] == ["exec", "-T", "postgres"]:
    raise SystemExit(subprocess.run(["docker", "exec", "-i", pg, *args[3:]]).returncode)
if args[:3] == ["exec", "-T", "redis"]:
    raise SystemExit(subprocess.run(["docker", "exec", "-i", redis, *args[3:]]).returncode)
if args and args[0] == "cp":
    source, target = args[1], args[2]
    if source.startswith("redis:"):
        source = redis + source.removeprefix("redis")
    if target.startswith("redis:"):
        target = redis + target.removeprefix("redis")
    raise SystemExit(subprocess.run(["docker", "cp", source, target]).returncode)
if args and args[0] == "stop":
    if "redis" in args:
        subprocess.run(["docker", "stop", "--time", "5", redis], check=True)
    raise SystemExit(0)
if args and args[0] == "up":
    for service, container in (("postgres", pg), ("redis", redis)):
        if service in args:
            subprocess.run(["docker", "start", container], check=True, stdout=subprocess.DEVNULL)
    raise SystemExit(0)
raise SystemExit(0)
""",
            encoding="utf-8",
        )
        shim.chmod(0o700)
        fixture["env"].update(
            {
                "RESTORE_COMPOSE_COMMAND": str(shim),
                "REAL_PG_CONTAINER": pg_container,
                "REAL_REDIS_CONTAINER": redis_container,
                # These are deliberately wrong: discovery must use the service.
                "POSTGRES_USER": "wrong_host_user",
                "POSTGRES_DB": "wrong_host_database",
            }
        )

        result = _run(fixture)
        assert result.returncode == 0, result.stderr
        current = command(
            "docker", "exec", pg_container, "psql",
            "--username", pg_user, "--dbname", live_database,
            "--tuples-only", "--no-align", "--command",
            "SELECT value FROM restore_marker;",
        ).stdout.decode().strip()
        assert current == "new"
        cleared = command(
            "docker", "exec", redis_container,
            "redis-cli", "--raw", "GET", "restore:marker",
        ).stdout.decode().strip()
        assert cleared == ""

        rollback = (
            fixture["receipts"] / "rollbacks" / fixture["request_id"] / "rollback.sh"
        )
        rollback_result = subprocess.run(
            [str(rollback)],
            env=fixture["env"],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert rollback_result.returncode == 0, rollback_result.stderr
        restored_db = command(
            "docker", "exec", pg_container, "psql",
            "--username", pg_user, "--dbname", live_database,
            "--tuples-only", "--no-align", "--command",
            "SELECT value FROM restore_marker;",
        ).stdout.decode().strip()
        assert restored_db == "old"
        restored_redis = command(
            "docker", "exec", redis_container,
            "redis-cli", "--raw", "GET", "restore:marker",
        ).stdout.decode().strip()
        assert restored_redis == "old"
    finally:
        for container, prefix in (
            (pg_container, "ag_finalfix_host_pg_"),
            (redis_container, "ag_finalfix_host_redis_"),
        ):
            assert container.startswith(prefix) and "/" not in container
            subprocess.run(
                ["docker", "rm", "--force", container],
                check=False,
                capture_output=True,
            )
