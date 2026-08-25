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
import hmac
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

PHASES = (
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
PG_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
SHA256 = re.compile(r"[0-9a-f]{64}")
DATABASE_PAYLOADS = frozenset({"database.dump", "database.sql"})
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)


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
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW
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


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


@contextmanager
def safe_directory_fd(
    root: Path,
    relative_parts: tuple[str, ...] = (),
    *,
    create: bool = False,
):
    """Traverse a directory tree without ever following a symlink parent."""

    root = Path(root)
    root_stat = root.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise RestoreHostError(f"Unsafe restore directory: {root}")
    descriptor = os.open(root, os.O_RDONLY | DIRECTORY | NOFOLLOW)
    current = root
    try:
        for part in relative_parts:
            if part in {"", ".", ".."} or "/" in part:
                raise RestoreHostError("Unsafe restore parent component")
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | DIRECTORY | NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(
                    part,
                    os.O_RDONLY | DIRECTORY | NOFOLLOW,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise RestoreHostError(
                    f"Restore parent is a symlink or non-directory: {current / part}"
                ) from exc
            os.close(descriptor)
            descriptor = child
            current /= part
        yield current, descriptor
    finally:
        os.close(descriptor)


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
        payload_rel = safe_relative(
            str(self.request.get("payload") or ""), "payload path"
        )
        preflight_payload = self.session.joinpath(*payload_rel.parts)
        if (
            preflight_payload.parent != self.session
            or preflight_payload.is_symlink()
            or not preflight_payload.is_dir()
        ):
            raise RestoreHostError(
                "validated payload is missing or escaped its session"
            )
        self.database_payload_name = self._require_database_payload(
            self.request.get("manifest"),
            preflight_payload,
        )
        self.receipt_path = self.receipts / f"{self.request_id}.json"
        self.rollback_dir = self.receipts / "rollbacks" / self.request_id
        self.rollback_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.journal_path = self.rollback_dir / "journal.json"
        self.phase = "validate_request"
        self.started_at = now()
        suffix = self.request_id.replace("-", "")[:12]
        self.compose_command = shlex.split(
            os.environ.get("RESTORE_COMPOSE_COMMAND", "docker compose")
        )
        if not self.compose_command:
            raise RestoreHostError("RESTORE_COMPOSE_COMMAND is empty")
        self.postgres_user, self.live_database = self._discover_postgres_identity()
        self.temp_database = f"ag_restore_{suffix}"
        self.rollback_database = f"ag_rollback_{suffix}"
        self.failed_database = f"ag_failed_{suffix}"
        self.payload = self.session
        self.archive = self.session
        self.manifest: dict[str, Any] = {}
        self.journal: dict[str, Any] = {
            "version": 1,
            "request_id": self.request_id,
            "project": str(self.project),
            "postgres_user": self.postgres_user,
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

    @staticmethod
    def _require_database_payload(manifest: Any, payload: Path) -> str:
        if not isinstance(manifest, dict) or manifest.get("version") != "0.3.0":
            raise RestoreHostError("validated manifest is invalid")
        contents = manifest.get("contents")
        if not isinstance(contents, list) or "database" not in contents:
            raise RestoreHostError(
                "validated manifest is not restorable without a database component"
            )
        entries = manifest.get("entries")
        if not isinstance(entries, dict) or not entries:
            raise RestoreHostError("validated manifest entries are missing")
        declared = [name for name in entries if name in DATABASE_PAYLOADS]
        actual = [
            name
            for name in DATABASE_PAYLOADS
            if os.path.lexists(payload / name)
        ]
        if len(declared) != 1 or actual != declared:
            raise RestoreHostError(
                "validated restore must contain exactly one recognized database payload"
            )
        name = declared[0]
        target = payload / name
        target_stat = target.lstat()
        if not stat.S_ISREG(target_stat.st_mode):
            raise RestoreHostError(
                "validated database payload must be a regular file"
            )
        expected = entries[name]
        if not isinstance(expected, dict):
            raise RestoreHostError(
                "validated database payload manifest entry is invalid"
            )
        expected_size = expected.get("size")
        expected_hash = str(expected.get("sha256") or "")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
            or not SHA256.fullmatch(expected_hash)
            or target_stat.st_size != expected_size
            or sha256_file(target) != expected_hash
        ):
            raise RestoreHostError(
                "validated database payload hash or size changed"
            )
        return name

    def _discover_postgres_identity(self) -> tuple[str, str]:
        """Read the effective service identity, never host libpq variables."""

        result = self.compose(
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'printf "%s\\n%s\\n" "$POSTGRES_USER" "$POSTGRES_DB"',
        )
        lines = (result.stdout or b"").decode("utf-8").splitlines()
        if len(lines) != 2 or not all(PG_NAME.fullmatch(line) for line in lines):
            raise RestoreHostError(
                "Compose PostgreSQL user/database identity is invalid"
            )
        return lines[0], lines[1]

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
        fd = os.open(
            rollback,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
            0o700,
        )
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

    def _fault_boundary(self, boundary: str) -> None:
        if os.environ.get("RESTORE_FAULT_BOUNDARY") != boundary:
            return
        action = os.environ.get("RESTORE_FAULT_ACTION", "fail")
        if action == "kill":
            os.kill(os.getpid(), signal.SIGKILL)
        if action != "fail":
            raise RestoreHostError("Invalid restore fault action")
        raise RestoreHostError(f"Injected failure at {boundary}")

    def _rollback_fault_boundary(self, swap: dict[str, Any], boundary: str) -> None:
        rendered = f"{swap['kind']}:{swap['component']}:{boundary}"
        if os.environ.get("RESTORE_ROLLBACK_FAULT_BOUNDARY") != rendered:
            return
        action = os.environ.get("RESTORE_ROLLBACK_FAULT_ACTION", "fail")
        if action == "kill":
            os.kill(os.getpid(), signal.SIGKILL)
        if action != "fail":
            raise RestoreHostError("Invalid rollback fault action")
        raise RestoreHostError(f"Injected rollback failure at {rendered}")

    @staticmethod
    def _fsync_tree(root: Path) -> None:
        """Make a copied directory tree durable before it can become live."""

        for directory, _names, filenames in os.walk(root, topdown=False):
            directory_path = Path(directory)
            for name in filenames:
                target = directory_path / name
                fd = os.open(target, os.O_RDONLY | NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            fd = os.open(directory_path, os.O_RDONLY | DIRECTORY | NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _prepare_file_swap(
        self,
        *,
        kind: str,
        component: str,
        target: Path,
        old_path: Path,
        new_path: Path,
        root: Path,
        relative: str,
        existed: bool,
        original_identity: dict[str, int] | None,
        candidate_identity: dict[str, int],
    ) -> dict[str, Any]:
        swap = {
            "kind": kind,
            "component": component,
            "target": str(target),
            "old": str(old_path),
            "new": str(new_path),
            "root": str(root),
            "relative": relative,
            "existed": existed,
            "original_identity": original_identity,
            "candidate_identity": candidate_identity,
            "state": "prepared",
        }
        self.journal["file_swaps"].append(swap)
        self._save_journal()
        self._fault_boundary(f"{kind}:{component}:prepared-journal")
        return swap

    def _transition_file_swap(
        self,
        swap: dict[str, Any],
        state: str,
        boundary: str,
    ) -> None:
        swap["state"] = state
        swap["updated_at"] = now()
        self._save_journal()
        self._fault_boundary(
            f"{swap['kind']}:{swap['component']}:{boundary}"
        )

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
        contents = manifest.get("contents")
        if not isinstance(contents, list) or "database" not in contents:
            raise RestoreHostError(
                "validated manifest is not restorable without a database component"
            )
        entries = manifest.get("entries")
        if not isinstance(entries, dict) or not entries:
            raise RestoreHostError("validated manifest entries are missing")
        database_payload_name = self._require_database_payload(
            manifest,
            self.payload,
        )
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
        self.manifest = manifest
        self.database_payload_name = database_payload_name

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

    def freeze_inputs(self) -> None:
        """Copy post-stop, revalidated bytes into host-owned restore storage."""

        self.enter("freeze_inputs")
        frozen = self.rollback_dir / "frozen-inputs"
        os.mkdir(frozen, 0o700)
        frozen_payload = frozen / "payload"
        os.mkdir(frozen_payload, 0o700)
        frozen_archive = frozen / "archive.tar.gz"

        archive_expected_size = int(self.request.get("archive_size", -1))
        archive_expected_hash = str(self.request.get("archive_sha256") or "")
        archive_size, archive_hash = self._copy_regular_file(
            self.archive.parent,
            (),
            self.archive.name,
            frozen,
            (),
            frozen_archive.name,
        )
        if archive_size != archive_expected_size or not hmac.compare_digest(
            archive_hash, archive_expected_hash
        ):
            raise RestoreHostError("validated archive changed after writer stop")

        entries = self.manifest["entries"]
        actual_files: set[str] = set()
        for directory, names, filenames in os.walk(self.payload, followlinks=False):
            directory_path = Path(directory)
            for name in names:
                if (directory_path / name).is_symlink():
                    raise RestoreHostError("validated payload contains a symlink")
            for name in filenames:
                source = directory_path / name
                if source.is_symlink() or not source.is_file():
                    raise RestoreHostError("validated payload contains an unsafe entry")
                actual_files.add(source.relative_to(self.payload).as_posix())
        if actual_files != set(entries):
            raise RestoreHostError(
                "validated payload entries changed after writer stop"
            )

        total = 0
        for name, expected in entries.items():
            relative = safe_relative(name, "manifest entry")
            if not isinstance(expected, dict):
                raise RestoreHostError("validated manifest entry is invalid")
            size, digest = self._copy_regular_file(
                self.payload,
                relative.parent.parts,
                relative.name,
                frozen_payload,
                relative.parent.parts,
                relative.name,
            )
            expected_size = int(expected.get("size", -1))
            expected_hash = str(expected.get("sha256") or "")
            if size != expected_size or not hmac.compare_digest(digest, expected_hash):
                raise RestoreHostError("validated payload changed after writer stop")
            total += size
        if total != int(self.manifest.get("total_uncompressed_bytes", -1)):
            raise RestoreHostError("validated manifest total changed after writer stop")

        for directory, names, filenames in os.walk(frozen_payload, topdown=False):
            directory_path = Path(directory)
            for name in filenames:
                os.chmod(directory_path / name, 0o400)
            for name in names:
                os.chmod(directory_path / name, 0o500)
        os.chmod(frozen_payload, 0o500)
        os.chmod(frozen_archive, 0o400)
        os.chmod(frozen, 0o500)
        directory_fd = os.open(self.rollback_dir, os.O_RDONLY | DIRECTORY | NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self.archive = frozen_archive
        self.payload = frozen_payload
        self.journal["frozen_inputs"] = str(frozen)
        self._save_journal()

    @staticmethod
    def _copy_regular_file(
        source_root: Path,
        source_parents: tuple[str, ...],
        source_name: str,
        destination_root: Path,
        destination_parents: tuple[str, ...],
        destination_name: str,
    ) -> tuple[int, str]:
        with safe_directory_fd(source_root, source_parents) as (_, source_parent_fd):
            source_stat = os.stat(
                source_name, dir_fd=source_parent_fd, follow_symlinks=False
            )
            if not stat.S_ISREG(source_stat.st_mode):
                raise RestoreHostError("Restore input is not a regular file")
            source_fd = os.open(
                source_name, os.O_RDONLY | NOFOLLOW, dir_fd=source_parent_fd
            )
            with safe_directory_fd(
                destination_root, destination_parents, create=True
            ) as (_, destination_parent_fd):
                destination_fd = os.open(
                    destination_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
                    0o600,
                    dir_fd=destination_parent_fd,
                )
                digest = hashlib.sha256()
                size = 0
                with (
                    os.fdopen(source_fd, "rb") as source,
                    os.fdopen(destination_fd, "wb") as destination,
                ):
                    while block := source.read(1024 * 1024):
                        destination.write(block)
                        digest.update(block)
                        size += len(block)
                    destination.flush()
                    os.fsync(destination.fileno())
                os.fsync(destination_parent_fd)
        return size, digest.hexdigest()

    def snapshot(self) -> None:
        self.enter("snapshot")
        postgres_dump = self.rollback_dir / "postgres.dump"
        self.compose(
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "--username",
            self.postgres_user,
            "--dbname",
            self.live_database,
            "--format=custom",
            "--compress=3",
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
            try:
                with safe_directory_fd(
                    live_root, relative.parent.parts, create=False
                ) as (_, current_parent_fd):
                    try:
                        current_stat = os.stat(
                            relative.name,
                            dir_fd=current_parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(current_stat.st_mode):
                        continue
                    target = destination_root / relative
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    source_fd = os.open(
                        relative.name,
                        os.O_RDONLY | NOFOLLOW,
                        dir_fd=current_parent_fd,
                    )
                    destination_fd = os.open(
                        target,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
                        stat.S_IMODE(current_stat.st_mode) & 0o600 or 0o600,
                    )
                    with (
                        os.fdopen(source_fd, "rb") as current_file,
                        os.fdopen(destination_fd, "wb") as snapshot_file,
                    ):
                        shutil.copyfileobj(current_file, snapshot_file)
                        snapshot_file.flush()
                        os.fsync(snapshot_file.fileno())
            except FileNotFoundError:
                continue

    def temp_database_restore(self) -> None:
        self.enter("temp_database")
        self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "--username",
            self.postgres_user,
            "--dbname",
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
            "--username",
            self.postgres_user,
            "--dbname",
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
                "--username",
                self.postgres_user,
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "--dbname",
                self.temp_database,
                input_path=custom,
            )
        elif plain.is_file():
            self.compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "--username",
                self.postgres_user,
                "--dbname",
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
            "--username",
            self.postgres_user,
            "--dbname",
            self.temp_database,
            "-v",
            "ON_ERROR_STOP=1",
            "--set=offline_restore_phase=integrity",
            "-c",
            "SELECT 1 / CASE WHEN count(*) > 0 THEN 1 ELSE 0 END FROM pg_catalog.pg_tables WHERE schemaname='public'; SELECT count(*) FROM alembic_version;",
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
            "--username",
            self.postgres_user,
            "--dbname",
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
        new_path = target.with_name(f".{target.name}.restore-new-{self.request_id}")
        old_path = target.with_name(f".{target.name}.restore-old-{self.request_id}")
        with safe_directory_fd(target.parent) as (_, parent_fd):
            if _entry_exists(parent_fd, new_path.name) or _entry_exists(
                parent_fd, old_path.name
            ):
                raise RestoreHostError("Restore directory swap paths already exist")
            os.mkdir(new_path.name, 0o700, dir_fd=parent_fd)
            shutil.copytree(source, new_path, symlinks=False, dirs_exist_ok=True)
            for copied_directory, names, filenames in os.walk(new_path):
                copied_path = Path(copied_directory)
                os.chmod(copied_path, 0o700)
                for name in names:
                    os.chmod(copied_path / name, 0o700)
                for name in filenames:
                    os.chmod(copied_path / name, 0o600)
            self._fsync_tree(new_path)
            os.fsync(parent_fd)
            candidate_identity = self._entry_identity(parent_fd, new_path.name)
            if candidate_identity is None:
                raise RestoreHostError("Prepared restore directory disappeared")
            original_identity = self._entry_identity(parent_fd, target.name)
            existed = original_identity is not None
            swap = self._prepare_file_swap(
                kind="directory",
                component=component,
                target=target,
                old_path=old_path,
                new_path=new_path,
                root=target.parent,
                relative=target.name,
                existed=existed,
                original_identity=original_identity,
                candidate_identity=candidate_identity,
            )
            if existed:
                os.rename(
                    target.name,
                    old_path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            self._fault_boundary(f"directory:{component}:old-renamed")
            os.fsync(parent_fd)
            self._fault_boundary(f"directory:{component}:old-fsynced")
            self._transition_file_swap(swap, "old_moved", "old-journal")
            os.rename(
                new_path.name,
                target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            self._fault_boundary(f"directory:{component}:new-renamed")
            os.fsync(parent_fd)
            self._fault_boundary(f"directory:{component}:new-fsynced")
            self._transition_file_swap(swap, "applied", "applied-journal")
            self._transition_file_swap(swap, "committed", "committed-journal")

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
            new_path = target.with_name(f".{target.name}.restore-new-{self.request_id}")
            old_path = target.with_name(f".{target.name}.restore-old-{self.request_id}")
            with safe_directory_fd(target_root, relative.parent.parts, create=True) as (
                _,
                parent_fd,
            ):
                if _entry_exists(parent_fd, new_path.name) or _entry_exists(
                    parent_fd, old_path.name
                ):
                    raise RestoreHostError("Restore file swap paths already exist")
                source_fd = os.open(source, os.O_RDONLY | NOFOLLOW)
                new_fd = os.open(
                    new_path.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
                    stat.S_IMODE(source.stat().st_mode) & 0o600 or 0o600,
                    dir_fd=parent_fd,
                )
                with (
                    os.fdopen(source_fd, "rb") as source_file,
                    os.fdopen(new_fd, "wb") as new_file,
                ):
                    shutil.copyfileobj(source_file, new_file)
                    new_file.flush()
                    os.fsync(new_file.fileno())
                os.fsync(parent_fd)
                candidate_identity = self._entry_identity(parent_fd, new_path.name)
                if candidate_identity is None:
                    raise RestoreHostError("Prepared restore file disappeared")
                original_identity = self._entry_identity(parent_fd, target.name)
                existed = original_identity is not None
                swap = self._prepare_file_swap(
                    kind="file",
                    component=component,
                    target=target,
                    old_path=old_path,
                    new_path=new_path,
                    root=target_root,
                    relative=relative.as_posix(),
                    existed=existed,
                    original_identity=original_identity,
                    candidate_identity=candidate_identity,
                )
                if existed:
                    os.rename(
                        target.name,
                        old_path.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                self._fault_boundary(f"file:{component}:old-renamed")
                os.fsync(parent_fd)
                self._fault_boundary(f"file:{component}:old-fsynced")
                self._transition_file_swap(swap, "old_moved", "old-journal")
                os.rename(
                    new_path.name,
                    target.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                self._fault_boundary(f"file:{component}:new-renamed")
                os.fsync(parent_fd)
                self._fault_boundary(f"file:{component}:new-fsynced")
                self._transition_file_swap(swap, "applied", "applied-journal")
                self._transition_file_swap(swap, "committed", "committed-journal")

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
        self.freeze_inputs()
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
        rollback_components: dict[str, dict[str, str]] | None = None,
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
        if rollback_components is not None:
            receipt["rollback_components"] = rollback_components
        atomic_json(self.receipt_path, receipt, immutable=True)

    def rollback(self) -> dict[str, dict[str, str]]:
        outcomes: dict[str, dict[str, str]] = {}

        def attempt(name: str, action) -> None:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - independent recovery components
                outcomes[name] = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            else:
                outcomes[name] = {"status": "complete"}

        attempt(
            "stop_writers",
            lambda: self.compose("stop", "-t", "120", *WRITERS, "admin-web"),
        )
        attempt("files", self._rollback_files)
        if self.journal.get("switch_attempted"):
            attempt("database", self._rollback_database)
        else:
            outcomes["database"] = {"status": "not_required"}
        if self.journal.get("redis_snapshot"):
            attempt("redis", self._rollback_redis)
        else:
            outcomes["redis"] = {"status": "not_required"}
        # Always reach a diagnostic foreground-only state, even when every
        # preceding recovery component failed independently.
        attempt(
            "foreground",
            lambda: self.compose(
                "up", "-d", "--wait", "--wait-timeout", "180", *FOREGROUND
            ),
        )
        return outcomes

    def _rollback_redis(self) -> None:
        redis_data = self.rollback_dir / "redis-data"
        self.compose("stop", "-t", "30", "redis")
        self.compose("cp", f"{redis_data}/.", "redis:/data")

    def _rollback_database(self) -> None:
        identity_query = (
            "SELECT datname FROM pg_database "
            f"WHERE datname IN ('{self.live_database}','{self.rollback_database}');"
        )

        def probe() -> set[str]:
            identity_result = self.compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "--username",
                self.postgres_user,
                "--dbname",
                "postgres",
                "-At",
                "--set=offline_restore_phase=rollback-identities",
                "-c",
                identity_query,
            )
            identities = set(
                (identity_result.stdout or b"").decode("utf-8").splitlines()
            )
            allowed = {self.live_database, self.rollback_database}
            if not identities or not identities.issubset(allowed):
                raise RestoreHostError("PostgreSQL database identity output is invalid")
            return identities

        try:
            identities = probe()
        except Exception:  # noqa: BLE001 - retry after ensuring PostgreSQL is available
            self.compose("up", "-d", "--wait", "--wait-timeout", "180", "postgres")
            identities = probe()
        if (
            self.journal.get("database_switched")
            and self.rollback_database not in identities
        ):
            raise RestoreHostError("Post-switch rollback database identity is unproven")
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
            "--username",
            self.postgres_user,
            "--dbname",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            terminate,
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
            "--username",
            self.postgres_user,
            "--dbname",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            rollback_sql,
        )

    def _rollback_files(self) -> None:
        errors: list[str] = []
        for swap in reversed(list(self.journal.get("file_swaps") or [])):
            try:
                self._rollback_file_swap(swap)
            except Exception as exc:  # noqa: BLE001 - reconcile all swaps
                errors.append(
                    f"{swap.get('component', 'unknown')}:"
                    f"{swap.get('relative', 'unknown')}: "
                    f"{type(exc).__name__}: {exc}"
                )
        if errors:
            raise RestoreHostError(
                "One or more filesystem swaps could not be rolled back: "
                + "; ".join(errors)
            )

    def _transition_file_rollback(
        self,
        swap: dict[str, Any],
        state: str,
        boundary: str,
    ) -> None:
        swap["rollback_state"] = state
        swap["rollback_updated_at"] = now()
        if state == "rolled_back":
            swap["state"] = "rolled_back"
            swap["rolled_back_at"] = swap["rollback_updated_at"]
            swap.pop("rollback_intent", None)
        self._save_journal()
        self._rollback_fault_boundary(swap, boundary)

    @staticmethod
    def _entry_identity(parent_fd: int, name: str) -> dict[str, int] | None:
        try:
            entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return {
            "device": int(entry.st_dev),
            "inode": int(entry.st_ino),
            "mode": int(stat.S_IFMT(entry.st_mode)),
        }

    @staticmethod
    def _require_rollback_identity(
        actual: dict[str, int] | None,
        expected: Any,
        *,
        kind: str,
        name: str,
    ) -> None:
        if actual is None:
            raise RestoreHostError(f"Rollback mutation source is missing: {name}")
        if kind == "directory":
            expected_mode = stat.S_IFDIR
        elif kind == "file":
            expected_mode = stat.S_IFREG
        else:
            raise RestoreHostError("Unknown rollback cleanup kind")
        if actual.get("mode") != expected_mode:
            raise RestoreHostError(f"Rollback mutation source has wrong type: {name}")
        if not isinstance(expected, dict) or any(
            int(expected.get(field, -1)) != actual[field]
            for field in ("device", "inode", "mode")
        ):
            raise RestoreHostError(f"Rollback mutation source changed: {name}")

    def _begin_rollback_intent(
        self,
        swap: dict[str, Any],
        *,
        action: str,
        identity: dict[str, int],
        state: str,
        boundary: str,
    ) -> None:
        swap["rollback_intent"] = {
            "action": action,
            "identity": identity,
        }
        self._transition_file_rollback(swap, state, boundary)

    def _durable_rollback_rename(
        self,
        swap: dict[str, Any],
        parent_fd: int,
        *,
        source: str,
        destination: str,
        action: str,
        expected_identity: Any,
    ) -> None:
        prefix = action.replace("-", "_")
        state = str(swap.get("rollback_state") or "")
        intent = swap.get("rollback_intent")
        if not isinstance(intent, dict) or intent.get("action") != action:
            if _entry_exists(parent_fd, destination):
                raise RestoreHostError(
                    f"Rollback destination already exists: {destination}"
                )
            identity = self._entry_identity(parent_fd, source)
            if identity is None:
                raise RestoreHostError(
                    f"Rollback mutation source is missing: {source}"
                )
            self._require_rollback_identity(
                identity,
                expected_identity if isinstance(expected_identity, dict) else identity,
                kind=str(swap["kind"]),
                name=source,
            )
            self._begin_rollback_intent(
                swap,
                action=action,
                identity=identity,
                state=f"{prefix}_intent",
                boundary=f"{action}-intent-journal",
            )
            state = f"{prefix}_intent"
            intent = swap["rollback_intent"]

        expected = intent.get("identity")
        source_identity = self._entry_identity(parent_fd, source)
        destination_identity = self._entry_identity(parent_fd, destination)
        if source_identity is not None:
            self._require_rollback_identity(
                source_identity,
                expected,
                kind=str(swap["kind"]),
                name=source,
            )
            if destination_identity is not None:
                raise RestoreHostError(
                    f"Rollback destination already exists: {destination}"
                )
            os.rename(
                source,
                destination,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            self._rollback_fault_boundary(swap, f"{action}-renamed")
            self._transition_file_rollback(
                swap,
                f"{prefix}_renamed",
                f"{action}-renamed-journal",
            )
            state = f"{prefix}_renamed"
        elif destination_identity is not None:
            self._require_rollback_identity(
                destination_identity,
                expected,
                kind=str(swap["kind"]),
                name=destination,
            )
            if state == f"{prefix}_intent":
                self._transition_file_rollback(
                    swap,
                    f"{prefix}_renamed",
                    f"{action}-renamed-journal",
                )
                state = f"{prefix}_renamed"
        else:
            raise RestoreHostError(
                f"Rollback rename paths are both missing: {source}, {destination}"
            )

        if state != f"{prefix}_fsynced":
            os.fsync(parent_fd)
            self._rollback_fault_boundary(swap, f"{action}-fsynced")
            self._transition_file_rollback(
                swap,
                f"{prefix}_fsynced",
                f"{action}-fsynced-journal",
            )

    def _durable_rollback_cleanup(
        self,
        swap: dict[str, Any],
        parent_fd: int,
        *,
        name: str,
        slot: str,
        expected_identity: Any,
    ) -> None:
        action = f"{slot}-cleanup"
        prefix = action.replace("-", "_")
        state = str(swap.get("rollback_state") or "")
        intent = swap.get("rollback_intent")
        if not isinstance(intent, dict) or intent.get("action") != action:
            identity = self._entry_identity(parent_fd, name)
            if identity is None:
                return
            self._require_rollback_identity(
                identity,
                expected_identity if isinstance(expected_identity, dict) else identity,
                kind=str(swap["kind"]),
                name=name,
            )
            self._begin_rollback_intent(
                swap,
                action=action,
                identity=identity,
                state=f"{prefix}_intent",
                boundary=f"{action}-intent-journal",
            )
            state = f"{prefix}_intent"
            intent = swap["rollback_intent"]

        identity = self._entry_identity(parent_fd, name)
        if identity is not None:
            self._require_rollback_identity(
                identity,
                intent.get("identity"),
                kind=str(swap["kind"]),
                name=name,
            )
            self._remove_explicit_at(parent_fd, name, str(swap["kind"]))
            self._rollback_fault_boundary(swap, f"{slot}-cleaned")
            self._transition_file_rollback(
                swap,
                f"{prefix}_cleaned",
                f"{slot}-cleaned-journal",
            )
            state = f"{prefix}_cleaned"
        elif state == f"{prefix}_intent":
            self._transition_file_rollback(
                swap,
                f"{prefix}_cleaned",
                f"{slot}-cleaned-journal",
            )
            state = f"{prefix}_cleaned"

        if state != f"{prefix}_fsynced":
            os.fsync(parent_fd)
            self._rollback_fault_boundary(swap, f"{action}-fsynced")
            self._transition_file_rollback(
                swap,
                f"{prefix}_fsynced",
                f"{action}-fsynced-journal",
            )

    def _resume_file_rollback_intent(
        self,
        swap: dict[str, Any],
        parent_fd: int,
        *,
        target_name: str,
        old_name: str,
        new_name: str,
        failed_name: str,
    ) -> None:
        state = str(swap.get("rollback_state") or "")
        if not state or state == "rolled_back":
            return
        intent = swap.get("rollback_intent")
        if not isinstance(intent, dict):
            raise RestoreHostError("Rollback state has no durable mutation intent")
        action = str(intent.get("action") or "")
        if action == "live-to-failed":
            self._durable_rollback_rename(
                swap,
                parent_fd,
                source=target_name,
                destination=failed_name,
                action=action,
                expected_identity=swap.get("candidate_identity"),
            )
        elif action == "old-to-live":
            self._durable_rollback_rename(
                swap,
                parent_fd,
                source=old_name,
                destination=target_name,
                action=action,
                expected_identity=swap.get("original_identity"),
            )
        elif action == "target-cleanup":
            self._durable_rollback_cleanup(
                swap,
                parent_fd,
                name=target_name,
                slot="target",
                expected_identity=swap.get("candidate_identity"),
            )
        elif action == "failed-cleanup":
            self._durable_rollback_cleanup(
                swap,
                parent_fd,
                name=failed_name,
                slot="failed",
                expected_identity=swap.get("candidate_identity"),
            )
        elif action == "new-cleanup":
            self._durable_rollback_cleanup(
                swap,
                parent_fd,
                name=new_name,
                slot="new",
                expected_identity=swap.get("candidate_identity"),
            )
        else:
            raise RestoreHostError(f"Unknown rollback mutation intent: {action}")

    def _rollback_file_swap(self, swap: dict[str, Any]) -> None:
        root = explicit_root(str(Path(swap["root"])), "rollback file root")
        relative = safe_relative(str(swap["relative"]), "rollback relative path")
        target = root.joinpath(*relative.parts)
        old = target.with_name(f".{target.name}.restore-old-{self.request_id}")
        new = target.with_name(f".{target.name}.restore-new-{self.request_id}")
        if (
            target != Path(swap["target"])
            or old != Path(swap["old"])
            or new != Path(swap.get("new", new))
        ):
            raise RestoreHostError("Rollback file journal paths are inconsistent")
        kind = str(swap["kind"])
        with safe_directory_fd(root, relative.parent.parts) as (_, parent_fd):
            failed_name = f".{target.name}.restore-failed-{self.request_id}"
            self._resume_file_rollback_intent(
                swap,
                parent_fd,
                target_name=target.name,
                old_name=old.name,
                new_name=new.name,
                failed_name=failed_name,
            )

            old_exists = _entry_exists(parent_fd, old.name)
            target_exists = _entry_exists(parent_fd, target.name)
            new_exists = _entry_exists(parent_fd, new.name)
            failed_exists = _entry_exists(parent_fd, failed_name)
            if swap.get("rollback_state") == "rolled_back":
                expected_target = bool(swap.get("existed"))
                if (
                    target_exists != expected_target
                    or old_exists
                    or new_exists
                    or failed_exists
                ):
                    raise RestoreHostError(
                        "Rolled-back filesystem paths changed after completion"
                    )
                return

            if bool(swap.get("existed")):
                if old_exists and target_exists and (new_exists or failed_exists):
                    occupied = new.name if new_exists else failed_name
                    raise RestoreHostError(
                        f"Rollback destination already exists: {occupied}"
                    )
                if old_exists:
                    if target_exists:
                        self._durable_rollback_rename(
                            swap,
                            parent_fd,
                            source=target.name,
                            destination=failed_name,
                            action="live-to-failed",
                            expected_identity=swap.get("candidate_identity"),
                        )
                    elif not failed_exists:
                        # The forward process can die after live-to-old while
                        # the candidate still occupies the deterministic new path.
                        pass
                    self._durable_rollback_rename(
                        swap,
                        parent_fd,
                        source=old.name,
                        destination=target.name,
                        action="old-to-live",
                        expected_identity=swap.get("original_identity"),
                    )
                elif not target_exists:
                    raise RestoreHostError(
                        f"Rollback original and live path are missing: {old.name}"
                    )
                else:
                    original_identity = swap.get("original_identity")
                    if isinstance(original_identity, dict):
                        self._require_rollback_identity(
                            self._entry_identity(parent_fd, target.name),
                            original_identity,
                            kind=kind,
                            name=target.name,
                        )
                # With no old path, the only deterministic live value is the
                # original restored by an earlier rollback invocation. This is
                # also the legacy-journal reconciliation for the old-to-live gap.
                if _entry_exists(parent_fd, failed_name):
                    self._durable_rollback_cleanup(
                        swap,
                        parent_fd,
                        name=failed_name,
                        slot="failed",
                        expected_identity=swap.get("candidate_identity"),
                    )
                if _entry_exists(parent_fd, new.name):
                    self._durable_rollback_cleanup(
                        swap,
                        parent_fd,
                        name=new.name,
                        slot="new",
                        expected_identity=swap.get("candidate_identity"),
                    )
            else:
                if old_exists:
                    raise RestoreHostError(
                        f"Unexpected rollback original exists: {old.name}"
                    )
                if target_exists:
                    if swap.get("state") == "prepared" and new_exists:
                        raise RestoreHostError(
                            f"Unrelated live path appeared before apply: {target.name}"
                        )
                    self._durable_rollback_cleanup(
                        swap,
                        parent_fd,
                        name=target.name,
                        slot="target",
                        expected_identity=swap.get("candidate_identity"),
                    )
                if _entry_exists(parent_fd, new.name):
                    self._durable_rollback_cleanup(
                        swap,
                        parent_fd,
                        name=new.name,
                        slot="new",
                        expected_identity=swap.get("candidate_identity"),
                    )
                if _entry_exists(parent_fd, failed_name):
                    self._durable_rollback_cleanup(
                        swap,
                        parent_fd,
                        name=failed_name,
                        slot="failed",
                        expected_identity=swap.get("candidate_identity"),
                    )

            self._transition_file_rollback(
                swap,
                "rolled_back",
                "rolled-back-journal",
            )

    @staticmethod
    def _remove_explicit_at(parent_fd: int, name: str, kind: str) -> None:
        if name in {"", ".", ".."} or "/" in name:
            raise RestoreHostError("Refusing unsafe rollback cleanup target")
        if kind == "directory":
            shutil.rmtree(name, dir_fd=parent_fd)
        elif kind == "file":
            os.unlink(name, dir_fd=parent_fd)
        else:
            raise RestoreHostError("Unknown rollback cleanup kind")


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
    runner.postgres_user = str(journal.get("postgres_user") or "")
    if not PG_NAME.fullmatch(runner.postgres_user):
        raise RestoreHostError("Rollback PostgreSQL user identity is invalid")
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
            outcomes = runner.rollback()
            for component, outcome in outcomes.items():
                if outcome["status"] == "failed":
                    print(
                        f"Rollback {component} failed: {outcome.get('error', 'unknown error')}",
                        file=sys.stderr,
                    )
            return (
                0
                if all(
                    outcome["status"] in {"complete", "not_required"}
                    for outcome in outcomes.values()
                )
                else 1
            )
        try:
            runner = RestoreRunner(args.request)
        except Exception as exc:  # noqa: BLE001 - fail closed on every request parser error
            print(
                f"Restore request rejected: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 2
        try:
            runner.run()
            return 0
        except Exception as exc:  # noqa: BLE001 - every phase failure enters rollback
            failed_phase = runner.phase
            outcomes = runner.rollback()
            rollback_status = (
                "complete"
                if all(
                    outcome["status"] in {"complete", "not_required"}
                    for outcome in outcomes.values()
                )
                else "failed"
            )
            foreground_running = (
                outcomes.get("foreground", {}).get("status") == "complete"
            )
            try:
                runner._write_receipt(
                    status=(
                        "rolled_back"
                        if rollback_status == "complete"
                        else "recovery_failed"
                    ),
                    phase=failed_phase,
                    rollback_performed=True,
                    rollback_status=rollback_status,
                    diagnostic=(
                        "Foreground services only; background writers remain stopped."
                        if foreground_running
                        else "Foreground recovery failed; background writers remain stopped."
                    ),
                    error=f"Restore failed during {failed_phase}: {type(exc).__name__}",
                    rollback_components=outcomes,
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
