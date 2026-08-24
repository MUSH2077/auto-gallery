#!/usr/bin/env python3
"""Execute one validated Auto Gallery restore outside the live application.

The API can only publish ``ready-request.json``. This host executable owns the
exclusive lock, service lifecycle, rollback point, database switch, and
external receipt. It never prompts and it fails closed.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

PHASES = (
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
)
WRITERS = (
    "scheduler",
    "worker-download",
    "worker-import",
    "worker-operations",
    "backend",
)
FOREGROUND = ("postgres", "redis", "meilisearch", "migrate", "backend", "admin-web")
BACKGROUND = ("worker-download", "worker-import", "worker-operations", "scheduler")
PG_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class RestoreHostError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any], *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, separators=(",", ":"), sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if immutable:
            # A receipt is create-once. Never replace an existing receipt.
            try:
                os.link(temp, path)
            except FileExistsError as exc:
                raise RestoreHostError(
                    "External restore receipt already exists"
                ) from exc
            temp.unlink()
            os.chmod(path, 0o444)
        else:
            os.replace(temp, path)
            os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp.exists():
            temp.unlink()


def explicit_root(value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise RestoreHostError(f"{name} must be an absolute path")
    resolved = path.resolve(strict=False)
    if resolved == Path("/") or len(resolved.parts) < 3:
        raise RestoreHostError(f"{name} is too broad")
    return resolved


def contained_regular_file(path: Path, root: Path, name: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RestoreHostError(f"{name} must be a regular non-symlink file")
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RestoreHostError(
            f"{name} is outside the configured staging root"
        ) from exc
    return resolved


def safe_relative(value: str, name: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise RestoreHostError(f"{name} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RestoreHostError(f"{name} is unsafe")
    return path


class RestoreRunner:
    def __init__(self, request_path: Path):
        project_value = os.environ.get("PROJECT_ROOT")
        if not project_value:
            project_value = str(Path(__file__).resolve().parent.parent)
        self.project = explicit_root(str(Path(project_value).resolve()), "PROJECT_ROOT")
        self.staging = explicit_root(
            os.environ.get(
                "RESTORE_STAGING_ROOT", str(self.project / "data/restore-staging")
            ),
            "RESTORE_STAGING_ROOT",
        )
        self.receipts = explicit_root(
            os.environ.get(
                "RESTORE_RECEIPTS_ROOT", str(self.project / "data/restore-receipts")
            ),
            "RESTORE_RECEIPTS_ROOT",
        )
        self.receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.receipts == self.staging or self.receipts.is_relative_to(self.staging):
            raise RestoreHostError("Receipt root must be external to restore staging")
        self.request_path = contained_regular_file(
            request_path, self.staging, "ready request"
        )
        if stat.S_IMODE(self.request_path.stat().st_mode) & 0o222:
            raise RestoreHostError("ready request must be read-only")
        try:
            self.request = json.loads(self.request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RestoreHostError("ready request JSON is invalid") from exc
        if not isinstance(self.request, dict):
            raise RestoreHostError("ready request must be an object")
        try:
            self.request_id = str(UUID(str(self.request.get("request_id"))))
        except (TypeError, ValueError) as exc:
            raise RestoreHostError("ready request identity is invalid") from exc
        if self.request_id != self.request.get("request_id"):
            raise RestoreHostError("ready request identity is not canonical")
        self.session = self.request_path.parent
        if (
            self.session.name != self.request_id
            or self.session.parent.resolve() != self.staging
        ):
            raise RestoreHostError(
                "ready request is not in its isolated staging directory"
            )
        self.receipt_path = self.receipts / f"{self.request_id}.json"
        self.rollback_dir = self.receipts / "rollbacks" / self.request_id
        self.rollback_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.journal_path = self.rollback_dir / "journal.json"
        self.phase = "validate_request"
        self.started_at = now()
        suffix = self.request_id.replace("-", "")[:12]
        live_database = os.environ.get("POSTGRES_DB", "autogallery")
        if not PG_NAME.fullmatch(live_database):
            raise RestoreHostError("POSTGRES_DB is not a safe PostgreSQL identifier")
        self.live_database = live_database
        self.temp_database = f"ag_restore_{suffix}"
        self.rollback_database = f"ag_rollback_{suffix}"
        self.failed_database = f"ag_failed_{suffix}"
        self.compose_command = shlex.split(
            os.environ.get("RESTORE_COMPOSE_COMMAND", "docker compose")
        )
        if not self.compose_command:
            raise RestoreHostError("RESTORE_COMPOSE_COMMAND is empty")
        self.payload = self.session
        self.archive = self.session
        self.journal: dict[str, Any] = {
            "version": 1,
            "request_id": self.request_id,
            "project": str(self.project),
            "live_database": self.live_database,
            "temp_database": self.temp_database,
            "rollback_database": self.rollback_database,
            "failed_database": self.failed_database,
            "switch_attempted": False,
            "database_switched": False,
            "redis_snapshot": False,
            "file_swaps": [],
            "created_at": self.started_at,
        }
        self._write_rollback_point()
        self._save_journal()

    def _write_rollback_point(self) -> None:
        rollback = self.rollback_dir / "rollback.sh"
        command = " ".join(
            shlex.quote(part)
            for part in (
                sys.executable,
                str(Path(__file__).resolve()),
                "--rollback-dir",
                str(self.rollback_dir),
            )
        )
        fd = os.open(rollback, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write("#!/usr/bin/env bash\nset -euo pipefail\n")
            output.write(f"exec {command}\n")
            output.flush()
            os.fsync(output.fileno())

    def _save_journal(self) -> None:
        atomic_json(self.journal_path, self.journal)

    def compose(
        self,
        *args: str,
        input_path: Path | None = None,
        output_path: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        stdin = input_path.open("rb") if input_path else None
        stdout = output_path.open("wb") if output_path else subprocess.PIPE
        try:
            return subprocess.run(
                [*self.compose_command, *args],
                cwd=self.project,
                env=os.environ.copy(),
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                check=check,
                timeout=900,
            )
        finally:
            if stdin:
                stdin.close()
            if output_path and stdout:
                stdout.close()

    def enter(self, phase: str) -> None:
        if phase not in PHASES:
            raise RestoreHostError("Unknown restore phase")
        self.phase = phase
        self.journal["phase"] = phase
        self.journal["updated_at"] = now()
        self._save_journal()
        if os.environ.get("RESTORE_FAIL_PHASE") == phase:
            raise RestoreHostError(f"Injected failure at {phase}")

    def validate_request(self) -> None:
        self.enter("validate_request")
        if (
            self.request.get("version") != 1
            or self.request.get("upload_id") != self.request_id
        ):
            raise RestoreHostError(
                "ready request version or upload identity is invalid"
            )
        archive_rel = safe_relative(
            str(self.request.get("archive") or ""), "archive path"
        )
        payload_rel = safe_relative(
            str(self.request.get("payload") or ""), "payload path"
        )
        self.archive = (self.session / Path(*archive_rel.parts)).resolve()
        self.payload = (self.session / Path(*payload_rel.parts)).resolve()
        if (
            self.archive.parent != self.session
            or self.archive.is_symlink()
            or not self.archive.is_file()
        ):
            raise RestoreHostError(
                "validated archive is missing or escaped its session"
            )
        if (
            self.payload.parent != self.session
            or self.payload.is_symlink()
            or not self.payload.is_dir()
        ):
            raise RestoreHostError(
                "validated payload is missing or escaped its session"
            )
        archive_hash = str(self.request.get("archive_sha256") or "")
        if (
            not SHA256.fullmatch(archive_hash)
            or self.archive.stat().st_size != int(self.request.get("archive_size", -1))
            or sha256_file(self.archive) != archive_hash
        ):
            raise RestoreHostError("validated archive hash or size changed")
        manifest = self.request.get("manifest")
        if not isinstance(manifest, dict) or manifest.get("version") != "0.3.0":
            raise RestoreHostError("validated manifest is invalid")
        entries = manifest.get("entries")
        if not isinstance(entries, dict) or not entries:
            raise RestoreHostError("validated manifest entries are missing")
        actual_files: set[str] = set()
        for target in self.payload.rglob("*"):
            if target.is_symlink():
                raise RestoreHostError("validated payload contains a symlink")
            if target.is_file():
                actual_files.add(target.relative_to(self.payload).as_posix())
            elif not target.is_dir():
                raise RestoreHostError("validated payload contains an unsafe entry")
        if actual_files != set(entries):
            raise RestoreHostError("validated payload entries changed after handoff")
        total = 0
        for name, expected in entries.items():
            relative = safe_relative(name, "manifest entry")
            target = (self.payload / Path(*relative.parts)).resolve()
            if (
                not target.is_relative_to(self.payload)
                or target.is_symlink()
                or not target.is_file()
            ):
                raise RestoreHostError("validated payload entry is missing or unsafe")
            if not isinstance(expected, dict):
                raise RestoreHostError("validated manifest entry is invalid")
            size = int(expected.get("size", -1))
            digest = str(expected.get("sha256") or "")
            if (
                size < 0
                or not SHA256.fullmatch(digest)
                or target.stat().st_size != size
                or sha256_file(target) != digest
            ):
                raise RestoreHostError("validated payload hash or size changed")
            total += size
        if total != int(manifest.get("total_uncompressed_bytes", -1)):
            raise RestoreHostError("validated manifest total changed")

        live_paths = self._live_paths()
        for name, path in live_paths.items():
            if path == Path("/") or len(path.parts) < 3:
                raise RestoreHostError(f"{name} restore path is too broad")
            if any(
                path == control
                or path.is_relative_to(control)
                or control.is_relative_to(path)
                for control in (self.staging, self.receipts)
            ):
                raise RestoreHostError(
                    f"{name} restore path overlaps restore control data"
                )
        live_items = list(live_paths.items())
        for index, (left_name, left_path) in enumerate(live_items):
            for right_name, right_path in live_items[index + 1 :]:
                if (
                    left_path == right_path
                    or left_path.is_relative_to(right_path)
                    or right_path.is_relative_to(left_path)
                ):
                    raise RestoreHostError(
                        f"{left_name} restore path overlaps {right_name}"
                    )

    def _live_paths(self) -> dict[str, Path]:
        defaults = {
            "app-config": self.project / "data/config/app",
            "gallerydl-config": self.project / "data/config/gallery-dl",
            "downloads": self.project / "data/downloads",
            "library": self.project / "data/library",
        }
        env_names = {
            "app-config": "HOST_CONFIG_APP",
            "gallerydl-config": "HOST_CONFIG_GALLERYDL",
            "downloads": "HOST_DOWNLOADS",
            "library": "HOST_LIBRARY",
        }
        return {
            name: explicit_root(
                str(Path(os.environ.get(env_names[name], str(default))).resolve()),
                env_names[name],
            )
            for name, default in defaults.items()
        }

    def stop_writers(self) -> None:
        self.enter("stop_writers")
        self.compose("stop", "-t", "120", *WRITERS)

    def snapshot(self) -> None:
        self.enter("snapshot")
        postgres_dump = self.rollback_dir / "postgres.dump"
        self.compose(
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom --compress=3',
            output_path=postgres_dump,
        )
        if not postgres_dump.is_file() or postgres_dump.stat().st_size == 0:
            raise RestoreHostError("PostgreSQL rollback snapshot is empty")
        redis_data = self.rollback_dir / "redis-data"
        redis_data.mkdir(mode=0o700)
        self.compose(
            "exec",
            "-T",
            "redis",
            "sh",
            "-c",
            'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning SAVE',
        )
        self.compose("cp", "redis:/data/.", str(redis_data))
        self.journal["redis_snapshot"] = True

        for component in ("app-config", "gallerydl-config"):
            source = self._live_paths()[component]
            destination = self.rollback_dir / component
            if source.exists():
                shutil.copytree(source, destination, symlinks=True)
        self._snapshot_payload_files(
            "download-archives", self._live_paths()["downloads"]
        )
        self._snapshot_payload_files("library-metadata", self._live_paths()["library"])
        self.journal["snapshot_complete"] = True
        self._save_journal()

    def _snapshot_payload_files(self, component: str, live_root: Path) -> None:
        source_root = self.payload / component
        if not source_root.exists():
            return
        destination_root = self.rollback_dir / component
        for source in source_root.rglob("*"):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(source_root)
            current = live_root / relative
            if current.exists():
                target = destination_root / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(current, target, follow_symlinks=False)

    def temp_database_restore(self) -> None:
        self.enter("temp_database")
        self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f"DROP DATABASE IF EXISTS {self.temp_database};",
        )
        self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f"CREATE DATABASE {self.temp_database};",
        )
        custom = self.payload / "database.dump"
        plain = self.payload / "database.sql"
        if custom.is_file():
            self.compose(
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "-d",
                self.temp_database,
                input_path=custom,
            )
        elif plain.is_file():
            self.compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-d",
                self.temp_database,
                "-v",
                "ON_ERROR_STOP=1",
                input_path=plain,
            )
        else:
            raise RestoreHostError("validated database payload is missing")
        self.journal["temp_database_created"] = True
        self._save_journal()

    def migrate(self) -> None:
        self.enter("migrate")
        self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-e",
            f"RESTORE_DATABASE_NAME={self.temp_database}",
            "migrate",
            "sh",
            "-c",
            'DATABASE_URL="${DATABASE_URL%/*}/$RESTORE_DATABASE_NAME" alembic upgrade head',
        )

    def integrity(self) -> None:
        self.enter("integrity")
        self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-d",
            self.temp_database,
            "-v",
            "ON_ERROR_STOP=1",
            "--set=offline_restore_phase=integrity",
            "-c",
            "SELECT CASE WHEN count(*) > 0 THEN 1 ELSE 1/0 END FROM pg_catalog.pg_tables WHERE schemaname='public'; SELECT count(*) FROM alembic_version;",
        )

    def switch_database(self) -> None:
        self.enter("switch_database")
        self.journal["switch_attempted"] = True
        self._save_journal()
        sql = (
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('{self.live_database}','{self.temp_database}') AND pid <> pg_backend_pid(); "
            f"ALTER DATABASE {self.live_database} RENAME TO {self.rollback_database}; "
            f"ALTER DATABASE {self.temp_database} RENAME TO {self.live_database};"
        )
        self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        )
        self.journal["database_switched"] = True
        self._save_journal()

    def restore_files(self) -> None:
        self.enter("restore_files")
        live = self._live_paths()
        for component in ("app-config", "gallerydl-config"):
            source = self.payload / component
            if source.is_dir():
                self._atomic_directory_swap(source, live[component], component)
        self._atomic_file_group(
            self.payload / "download-archives", live["downloads"], "download-archives"
        )
        self._atomic_file_group(
            self.payload / "library-metadata", live["library"], "library-metadata"
        )

    def _atomic_directory_swap(
        self, source: Path, target: Path, component: str
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        new_path = target.with_name(f".{target.name}.restore-new-{self.request_id}")
        old_path = target.with_name(f".{target.name}.restore-old-{self.request_id}")
        if new_path.exists() or old_path.exists():
            raise RestoreHostError("Restore directory swap paths already exist")
        shutil.copytree(source, new_path, symlinks=False)
        existed = target.exists()
        if existed:
            os.replace(target, old_path)
        os.replace(new_path, target)
        self.journal["file_swaps"].append(
            {
                "kind": "directory",
                "component": component,
                "target": str(target),
                "old": str(old_path),
                "existed": existed,
            }
        )
        self._save_journal()

    def _atomic_file_group(
        self, source_root: Path, target_root: Path, component: str
    ) -> None:
        if not source_root.is_dir():
            return
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(source_root)
            target = target_root / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            new_path = target.with_name(f".{target.name}.restore-new-{self.request_id}")
            old_path = target.with_name(f".{target.name}.restore-old-{self.request_id}")
            if new_path.exists() or old_path.exists():
                raise RestoreHostError("Restore file swap paths already exist")
            shutil.copy2(source, new_path, follow_symlinks=False)
            existed = target.exists()
            if existed:
                os.replace(target, old_path)
            os.replace(new_path, target)
            self.journal["file_swaps"].append(
                {
                    "kind": "file",
                    "component": component,
                    "target": str(target),
                    "old": str(old_path),
                    "existed": existed,
                }
            )
            self._save_journal()

    def clear_redis(self) -> None:
        self.enter("clear_redis")
        self.compose(
            "exec",
            "-T",
            "redis",
            "sh",
            "-c",
            'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning FLUSHDB',
        )

    def restart_foreground(self) -> None:
        self.enter("restart_foreground")
        self.compose("up", "-d", "--wait", "--wait-timeout", "180", *FOREGROUND)

    def restart_background(self) -> None:
        self.enter("restart_background")
        self.compose("up", "-d", *BACKGROUND)

    def run(self) -> None:
        self.validate_request()
        self.stop_writers()
        self.snapshot()
        self.temp_database_restore()
        self.migrate()
        self.integrity()
        self.switch_database()
        self.restore_files()
        self.clear_redis()
        self.restart_foreground()
        self.restart_background()
        self.enter("receipt_success")
        self._write_receipt(
            status="success",
            phase="complete",
            rollback_performed=False,
            rollback_status="available",
            diagnostic="All foreground and background services restarted.",
        )

    def _write_receipt(
        self,
        *,
        status: str,
        phase: str,
        rollback_performed: bool,
        rollback_status: str,
        diagnostic: str,
        error: str | None = None,
    ) -> None:
        receipt = {
            "version": 1,
            "request_id": self.request_id,
            "status": status,
            "phase": phase,
            "started_at": self.started_at,
            "completed_at": now(),
            "rollback_performed": rollback_performed,
            "rollback_status": rollback_status,
            "diagnostic": diagnostic,
            "rollback_command": str(self.rollback_dir / "rollback.sh"),
        }
        if error:
            receipt["error"] = error
        atomic_json(self.receipt_path, receipt, immutable=True)

    def rollback(self) -> None:
        self.compose("stop", "-t", "120", *WRITERS, "admin-web", check=False)
        self._rollback_files()
        if self.journal.get("switch_attempted"):
            self._rollback_database()
        if self.journal.get("redis_snapshot"):
            redis_data = self.rollback_dir / "redis-data"
            self.compose("stop", "-t", "30", "redis", check=False)
            self.compose("cp", f"{redis_data}/.", "redis:/data")
        self.compose("up", "-d", "--wait", "--wait-timeout", "180", *FOREGROUND)

    def _rollback_database(self) -> None:
        identity_query = (
            "SELECT datname FROM pg_database "
            f"WHERE datname IN ('{self.live_database}','{self.rollback_database}');"
        )
        identity_result = self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-d",
            "postgres",
            "-At",
            "--set=offline_restore_phase=rollback-identities",
            "-c",
            identity_query,
            check=False,
        )
        identities = set((identity_result.stdout or b"").decode("utf-8").splitlines())
        if self.rollback_database not in identities:
            # A failed atomic switch leaves only the original live database.
            # Never rename that sole healthy database away during recovery.
            return
        terminate = f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('{self.live_database}','{self.rollback_database}') AND pid <> pg_backend_pid();"
        self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            terminate,
            check=False,
        )
        if self.live_database in identities:
            rollback_sql = (
                f"ALTER DATABASE {self.live_database} RENAME TO {self.failed_database}; "
                f"ALTER DATABASE {self.rollback_database} RENAME TO {self.live_database};"
            )
        else:
            rollback_sql = f"ALTER DATABASE {self.rollback_database} RENAME TO {self.live_database};"
        self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            rollback_sql,
        )

    def _rollback_files(self) -> None:
        for swap in reversed(list(self.journal.get("file_swaps") or [])):
            target = Path(swap["target"])
            old = Path(swap["old"])
            if bool(swap.get("existed")) and old.exists():
                failed = target.with_name(
                    f".{target.name}.restore-failed-{self.request_id}"
                )
                if target.exists():
                    os.replace(target, failed)
                os.replace(old, target)
                self._remove_explicit(failed, swap["kind"])
            elif not bool(swap.get("existed")) and target.exists():
                self._remove_explicit(target, swap["kind"])

    @staticmethod
    def _remove_explicit(path: Path, kind: str) -> None:
        if len(path.parts) < 3 or path == Path("/"):
            raise RestoreHostError("Refusing broad rollback cleanup target")
        if kind == "directory":
            shutil.rmtree(path)
        else:
            path.unlink()


def load_rollback_runner(rollback_dir: Path) -> RestoreRunner:
    rollback_dir = rollback_dir.resolve()
    journal_path = rollback_dir / "journal.json"
    if (
        rollback_dir.is_symlink()
        or journal_path.is_symlink()
        or not journal_path.is_file()
    ):
        raise RestoreHostError("Rollback point is invalid")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    receipts = explicit_root(
        os.environ.get("RESTORE_RECEIPTS_ROOT", ""), "RESTORE_RECEIPTS_ROOT"
    )
    if rollback_dir.parent.parent != receipts or rollback_dir.name != journal.get(
        "request_id"
    ):
        raise RestoreHostError("Rollback point is outside the receipt root")
    staging = explicit_root(
        os.environ.get("RESTORE_STAGING_ROOT", ""), "RESTORE_STAGING_ROOT"
    )
    request = staging / journal["request_id"] / "ready-request.json"
    runner = RestoreRunner.__new__(RestoreRunner)
    runner.project = explicit_root(str(Path(journal["project"])), "PROJECT_ROOT")
    runner.staging = staging
    runner.receipts = receipts
    runner.request_path = request
    runner.request_id = journal["request_id"]
    runner.receipt_path = receipts / f"{runner.request_id}.json"
    runner.rollback_dir = rollback_dir
    runner.journal_path = journal_path
    runner.journal = journal
    runner.phase = "rollback"
    runner.started_at = now()
    runner.live_database = journal["live_database"]
    runner.temp_database = journal["temp_database"]
    runner.rollback_database = journal["rollback_database"]
    runner.failed_database = journal["failed_database"]
    runner.compose_command = shlex.split(
        os.environ.get("RESTORE_COMPOSE_COMMAND", "docker compose")
    )
    return runner


def acquire_lock(receipts: Path):
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = receipts / ".offline-restore.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute a validated offline Auto Gallery restore"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--request", type=Path)
    group.add_argument("--rollback-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_value = os.environ.get("PROJECT_ROOT") or str(
        Path(__file__).resolve().parent.parent
    )
    project = explicit_root(str(Path(project_value).resolve()), "PROJECT_ROOT")
    receipts = explicit_root(
        os.environ.get("RESTORE_RECEIPTS_ROOT", str(project / "data/restore-receipts")),
        "RESTORE_RECEIPTS_ROOT",
    )
    lock_fd = acquire_lock(receipts)
    if lock_fd is None:
        print("Offline restore lock is already held", file=sys.stderr)
        return 75
    try:
        if args.rollback_dir:
            runner = load_rollback_runner(args.rollback_dir)
            runner.rollback()
            return 0
        try:
            runner = RestoreRunner(args.request)
        except Exception as exc:  # noqa: BLE001 - fail closed on every request parser error
            print(f"Restore request rejected: {type(exc).__name__}", file=sys.stderr)
            return 2
        try:
            runner.run()
            return 0
        except Exception as exc:  # noqa: BLE001 - every phase failure enters rollback
            failed_phase = runner.phase
            rollback_status = "complete"
            try:
                runner.rollback()
            except Exception:  # noqa: BLE001 - receipt must record rollback failure
                rollback_status = "failed"
            try:
                runner._write_receipt(
                    status="rolled_back",
                    phase=failed_phase,
                    rollback_performed=True,
                    rollback_status=rollback_status,
                    diagnostic="Foreground services only; background writers remain stopped.",
                    error=f"Restore failed during {failed_phase}: {type(exc).__name__}",
                )
            except Exception as receipt_exc:  # noqa: BLE001 - original phase error wins
                print(
                    f"External restore receipt failed: {type(receipt_exc).__name__}",
                    file=sys.stderr,
                )
            print(
                f"Restore failed during {failed_phase}; rollback {rollback_status}",
                file=sys.stderr,
            )
            return 1
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RestoreHostError as exc:
        print(f"Restore rejected: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
