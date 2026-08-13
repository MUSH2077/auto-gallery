"""Crash-recoverable, no-overwrite promotion for one gallery-dl job.

The staging tree lives below the canonical download root, so promotion can use
same-filesystem hard links.  A small manifest is written before any staged file
is linked into the canonical tree.  On retry, inode identities distinguish a
file linked by the interrupted promotion from an unrelated pre-existing file.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


MANIFEST_NAME = ".auto-gallery-stage.json"
MANIFEST_VERSION = 1
_INCOMPLETE_SUFFIXES = (
    ".part",
    ".part-frag",
    ".tmp",
    ".temp",
    ".ytdl",
    ".download",
)


class DownloadStageError(RuntimeError):
    """Base class for staging failures that must not overwrite canonical data."""


class DownloadStageManifestError(DownloadStageError):
    """The recovery manifest is corrupt or belongs to another job."""


class DownloadStageDiscoveryError(DownloadStageError):
    """Promoted metadata could not be identified and needs operator repair."""

    def __init__(self, invalid_paths: list[str]):
        self.invalid_paths = invalid_paths
        preview = ", ".join(invalid_paths[:5])
        if len(invalid_paths) > 5:
            preview += f" (+{len(invalid_paths) - 5} more)"
        super().__init__(f"download metadata discovery failed: {preview}")


class DownloadStageConflict(DownloadStageError):
    """A canonical target exists with different content or identity."""

    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts
        preview = ", ".join(conflicts[:5])
        if len(conflicts) > 5:
            preview += f" (+{len(conflicts) - 5} more)"
        super().__init__(f"download staging conflict: {preview}")


@dataclass(frozen=True, slots=True)
class StagePromotion:
    """Canonical files attributable to exactly one download job."""

    paths: tuple[Path, ...]
    conflicts: tuple[str, ...] = ()


def staging_enabled() -> bool:
    """Compatibility switch; staging is enabled unless explicitly disabled."""

    value = os.environ.get("DOWNLOAD_STAGING_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def validate_gallerydl_staging_config(config: dict[str, Any]) -> None:
    """Reject configured output paths that could bypass ``--destination``.

    Provider templates remain supported, but an absolute base/directory or a
    literal parent traversal could make gallery-dl archive a file that never
    entered this job's recovery manifest. Failing before the subprocess is
    safer than silently falling back to a whole-source scan.
    """

    def validate_path_value(value: Any, *, key: str) -> None:
        values = value if isinstance(value, (list, tuple)) else (value,)
        for item in values:
            if not isinstance(item, str) or not item:
                continue
            candidate = PurePosixPath(item.replace("\\", "/"))
            if candidate.is_absolute() or ".." in candidate.parts:
                raise DownloadStageManifestError(
                    f"gallery-dl {key} escapes managed staging"
                )

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).strip().lower().replace("_", "-")
                if normalized == "base-directory" and item not in (None, ""):
                    raise DownloadStageManifestError(
                        "gallery-dl base-directory cannot override managed staging"
                    )
                if normalized in {"directory", "filename"}:
                    validate_path_value(item, key=normalized)
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(config)


class DownloadStage:
    """Own and recover ``DOWNLOAD_ROOT/.staging/{job_id}``."""

    def __init__(
        self,
        download_root: Path,
        stage_root: Path,
        *,
        job_id: str,
        source: str | None,
    ) -> None:
        self.download_root = download_root.resolve()
        self.root = stage_root
        self.job_id = job_id
        self.source = source
        self.manifest_path = self.root / MANIFEST_NAME
        self._manifest: dict[str, Any] = {}

    @classmethod
    def open(cls, download_root: Path, job_id: str, source: str) -> "DownloadStage":
        """Open or create the stable stage used by all retries of one job."""

        if not job_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for char in job_id):
            raise DownloadStageManifestError("invalid staging job id")
        canonical_root = download_root.resolve()
        stage_root = canonical_root / ".staging" / job_id
        stage_root.mkdir(parents=True, exist_ok=True)
        stage = cls(canonical_root, stage_root, job_id=job_id, source=source)
        stage._validate_stage_root()
        stage._load_or_initialize()
        return stage

    @classmethod
    def from_existing(cls, stage_root: Path, download_root: Path) -> "DownloadStage":
        """Compatibility adapter used by the legacy promotion helper/tests."""

        stage_root.mkdir(parents=True, exist_ok=True)
        stage = cls(
            download_root,
            stage_root,
            job_id=stage_root.name or "legacy-stage",
            source=None,
        )
        stage._validate_stage_root(require_managed_parent=False)
        stage._load_or_initialize()
        return stage

    @property
    def has_recovery_state(self) -> bool:
        if self.manifest_path.exists():
            return True
        return any(path.name != MANIFEST_NAME for path in self.root.iterdir())

    def mark_running(self) -> None:
        self._manifest["state"] = "running"
        self._write_manifest()

    def mark_discovery_failed(self, invalid_paths: list[Path]) -> None:
        """Quarantine a promoted plan whose metadata cannot be identified.

        Promotion is intentionally not rolled back: canonical files are
        immutable and the manifest remains the durable recovery record.  A
        retry/operator can repair the metadata and replay ledger registration
        without downloading the media again.
        """

        relative_paths: list[str] = []
        for path in invalid_paths:
            try:
                relative = path.resolve().relative_to(self.download_root).as_posix()
            except (OSError, ValueError):
                relative = str(path)
            relative_paths.append(relative)
        self._manifest["state"] = "discovery_failed"
        self._manifest["invalid_metadata"] = sorted(set(relative_paths))
        self._write_manifest()

    def promote(self) -> StagePromotion:
        """Promote completed files without ever replacing an existing target."""

        staged_files = self._completed_staged_files()
        planned = self._manifest.setdefault("planned", {})
        promoted = self._manifest.setdefault("promoted", {})
        for relative, staged in staged_files.items():
            planned[relative] = _file_identity(staged)

        self._manifest["state"] = "promoting"
        self._manifest["conflicts"] = []
        # The complete recovery plan is durable before the first canonical link.
        self._write_manifest()

        conflicts: list[str] = []
        for relative in sorted(planned):
            staged = self.root / Path(PurePosixPath(relative))
            target = self._canonical_target(relative)
            if staged.exists() or staged.is_symlink():
                self._validate_regular_file(staged, label="staged")
                if target.exists() or target.is_symlink():
                    if target.is_symlink() or not target.is_file() or not _files_equal(staged, target):
                        conflicts.append(relative)
            elif target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_file():
                    conflicts.append(relative)
                    continue
                target_identity = _file_identity(target)
                if not (
                    _same_inode(planned.get(relative), target_identity)
                    or _same_inode(promoted.get(relative), target_identity)
                ):
                    conflicts.append(relative)
            else:
                raise DownloadStageError(
                    f"planned staged file is missing from both trees: {relative}"
                )

        if conflicts:
            self._manifest["state"] = "conflict"
            self._manifest["conflicts"] = conflicts
            self._write_manifest()
            raise DownloadStageConflict(conflicts)

        canonical_paths: list[Path] = []
        for relative in sorted(planned):
            staged = self.root / Path(PurePosixPath(relative))
            target = self._canonical_target(relative)
            if staged.exists() or staged.is_symlink():
                self._validate_regular_file(staged, label="staged")
                if target.exists() or target.is_symlink():
                    # Re-check at the mutation boundary.  A target can appear
                    # or be replaced after preflight while another process is
                    # writing the canonical tree.  Never discard the staged
                    # copy merely because the path now exists.
                    if (
                        target.is_symlink()
                        or not target.is_file()
                        or not _files_equal(staged, target)
                    ):
                        self._record_runtime_conflict(relative)
                        raise DownloadStageConflict([relative])
                    target_identity = _file_identity(target)
                    if not os.path.samefile(staged, target):
                        # Record the accepted canonical identity before removing
                        # the redundant staged copy.  This closes the recovery
                        # gap for identical files that were not hard-linked by us.
                        promoted[relative] = target_identity
                        self._write_manifest()
                        if not _same_inode(target_identity, _file_identity(target)):
                            self._record_runtime_conflict(relative)
                            raise DownloadStageConflict([relative])
                    staged.unlink()
                else:
                    try:
                        os.link(staged, target, follow_symlinks=False)
                    except FileExistsError:
                        if target.is_symlink() or not target.is_file() or not _files_equal(staged, target):
                            self._record_runtime_conflict(relative)
                            raise DownloadStageConflict([relative])
                        promoted[relative] = _file_identity(target)
                        self._write_manifest()
                    except OSError as exc:
                        raise DownloadStageError(
                            f"same-volume promotion failed for {relative}: {type(exc).__name__}"
                        ) from exc
                    # Make the canonical directory entry durable before the
                    # staged name is removed.  This preserves at least one
                    # recoverable link across a power loss.
                    _fsync_directory(target.parent)
                    promoted[relative] = _file_identity(target)
                    self._write_manifest()
                    staged.unlink()
            if not target.exists() or target.is_symlink() or not target.is_file():
                raise DownloadStageError(f"promoted target is unavailable: {relative}")
            promoted[relative] = _file_identity(target)
            canonical_paths.append(target)

        self._manifest["state"] = "promoted"
        self._manifest["promoted"] = promoted
        self._write_manifest()
        self._remove_empty_directories()
        return StagePromotion(tuple(canonical_paths))

    def mark_registered(self) -> None:
        """Persist ledger completion, then remove an empty completed stage."""

        remaining = self._non_manifest_entries()
        if remaining:
            # Incomplete gallery-dl files intentionally survive for a retry.
            self._manifest["state"] = "registered"
            self._write_manifest()
            return
        try:
            # With no retryable files left, the committed ledger is already the
            # durable marker.  Deleting the promoted manifest avoids one extra
            # NAS fsync; a crash before deletion merely causes idempotent replay.
            self.manifest_path.unlink(missing_ok=True)
            self._remove_empty_directories()
            self.root.rmdir()
            try:
                self.root.parent.rmdir()
            except OSError:
                pass
        except OSError:
            # Cleanup is optional; the registered manifest makes replay safe.
            return

    def _load_or_initialize(self) -> None:
        if self.manifest_path.is_symlink():
            raise DownloadStageManifestError("download staging manifest cannot be a symlink")
        if self.manifest_path.exists():
            try:
                payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DownloadStageManifestError("invalid download staging manifest") from exc
            if not isinstance(payload, dict) or payload.get("version") != MANIFEST_VERSION:
                raise DownloadStageManifestError("unsupported download staging manifest")
            if payload.get("job_id") != self.job_id:
                raise DownloadStageManifestError("download staging manifest job mismatch")
            if self.source is not None and payload.get("source") not in (None, self.source):
                raise DownloadStageManifestError("download staging manifest source mismatch")
            if not isinstance(payload.get("planned", {}), dict) or not isinstance(payload.get("promoted", {}), dict):
                raise DownloadStageManifestError("invalid download staging recovery plan")
            self._manifest = payload
            return
        now = _now_iso()
        self._manifest = {
            "version": MANIFEST_VERSION,
            "job_id": self.job_id,
            "source": self.source,
            "state": "created",
            "planned": {},
            "promoted": {},
            "conflicts": [],
            "created_at": now,
            "updated_at": now,
        }
        self._write_manifest()

    def _write_manifest(self) -> None:
        self._manifest["updated_at"] = _now_iso()
        temp_path = self.root / f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
        try:
            with temp_path.open("x", encoding="utf-8") as handle:
                json.dump(self._manifest, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.manifest_path)
            try:
                directory_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _completed_staged_files(self) -> dict[str, Path]:
        completed: dict[str, Path] = {}
        for current_root, directory_names, file_names in os.walk(self.root, followlinks=False):
            current = Path(current_root)
            for directory_name in list(directory_names):
                directory = current / directory_name
                if directory.is_symlink():
                    raise DownloadStageError(f"staged directory symlink is not allowed: {directory_name}")
            for file_name in file_names:
                path = current / file_name
                if path == self.manifest_path or file_name.startswith(f".{MANIFEST_NAME}."):
                    continue
                if _is_incomplete(file_name):
                    continue
                self._validate_regular_file(path, label="staged")
                relative = path.relative_to(self.root).as_posix()
                _validate_relative(relative)
                completed[relative] = path
        return completed

    def _canonical_target(self, relative: str) -> Path:
        _validate_relative(relative)
        target = self.download_root.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.parent.resolve().relative_to(self.download_root)
        except ValueError as exc:
            raise DownloadStageError(f"canonical path escapes download root: {relative}") from exc
        return target

    def _validate_stage_root(self, *, require_managed_parent: bool = True) -> None:
        if self.root.is_symlink():
            raise DownloadStageError("download staging root cannot be a symlink")
        resolved = self.root.resolve()
        if require_managed_parent:
            expected_parent = self.download_root / ".staging"
            try:
                resolved.relative_to(expected_parent)
            except ValueError as exc:
                raise DownloadStageError("download staging root escapes download root") from exc

    @staticmethod
    def _validate_regular_file(path: Path, *, label: str) -> None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise DownloadStageError(f"{label} path is not a regular file: {path.name}")

    def _record_runtime_conflict(self, relative: str) -> None:
        conflicts = list(self._manifest.get("conflicts") or [])
        if relative not in conflicts:
            conflicts.append(relative)
        self._manifest["conflicts"] = conflicts
        self._manifest["state"] = "conflict"
        self._write_manifest()

    def _non_manifest_entries(self) -> list[Path]:
        return [
            path
            for path in self.root.rglob("*")
            if path.is_file()
            and path != self.manifest_path
            and not path.name.startswith(f".{MANIFEST_NAME}.")
        ]

    def _remove_empty_directories(self) -> None:
        directories = [path for path in self.root.rglob("*") if path.is_dir()]
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass


def _validate_relative(relative: str) -> None:
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise DownloadStageManifestError(f"invalid staged relative path: {relative!r}")


def _is_incomplete(name: str) -> bool:
    lowered = name.lower()
    return (
        any(lowered.endswith(suffix) for suffix in _INCOMPLETE_SUFFIXES)
        or ".part-frag" in lowered
    )


def _file_identity(path: Path) -> dict[str, int]:
    info = path.stat(follow_symlinks=False)
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
    }


def _same_inode(expected: object, actual: dict[str, int]) -> bool:
    if not isinstance(expected, dict):
        return False
    return (
        expected.get("device") == actual["device"]
        and expected.get("inode") == actual["inode"]
        and expected.get("size") == actual["size"]
        and expected.get("mtime_ns") == actual["mtime_ns"]
    )


def _files_equal(first: Path, second: Path) -> bool:
    first_info = first.stat(follow_symlinks=False)
    second_info = second.stat(follow_symlinks=False)
    if first_info.st_size != second_info.st_size:
        return False
    if first_info.st_dev == second_info.st_dev and first_info.st_ino == second_info.st_ino:
        return True
    with first.open("rb") as left, second.open("rb") as right:
        while True:
            left_chunk = left.read(1024 * 1024)
            right_chunk = right.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _fsync_directory(path: Path) -> None:
    """Best-effort durability barrier for a directory entry."""

    try:
        directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Some NAS filesystems do not support fsync on directories.  The hard
        # link/no-overwrite invariants still hold; only the power-loss window
        # cannot be tightened on those mounts.
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
