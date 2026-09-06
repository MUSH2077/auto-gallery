"""Crash-recoverable, no-overwrite promotion for one gallery-dl job.

The staging tree lives below the canonical download root, so promotion can use
same-filesystem hard links.  A small manifest is written before any staged file
is linked into the canonical tree.  On retry, inode identities distinguish a
file linked by the interrupted promotion from an unrelated pre-existing file.
"""

from __future__ import annotations

import json
import hashlib
import fcntl
import functools
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any


MANIFEST_NAME = ".auto-gallery-stage.json"
MANIFEST_VERSION = 1
RETAINED_PROMOTION_DIR = ".auto-gallery-promote-retained"
PROMOTION_LOCK_NAME = ".auto-gallery-promotion.lock"
MAX_SAFE_METADATA_BYTES = 8 * 1024 * 1024
_INCOMPLETE_SUFFIXES = (
    ".part",
    ".part-frag",
    ".tmp",
    ".temp",
    ".ytdl",
    ".download",
)


def _serialize_canonical_writes(method):
    @functools.wraps(method)
    def guarded(stage, *args, **kwargs):
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(stage.download_root / PROMOTION_LOCK_NAME, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # Another process may have completed this same stage while this
            # object was waiting. Only the persisted plan is authoritative.
            stage._load_or_initialize()
            return method(stage, *args, **kwargs)
        finally:
            os.close(fd)
    return guarded


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

    def __init__(self, conflicts: list[str], details: list[dict[str, Any]] | None = None):
        self.conflicts = conflicts
        self.details = details or []
        preview = ", ".join(conflicts[:5])
        if len(conflicts) > 5:
            preview += f" (+{len(conflicts) - 5} more)"
        super().__init__(f"download staging conflict: {preview}")


@dataclass(frozen=True, slots=True)
class StagePromotion:
    """Canonical files attributable to exactly one download job."""

    paths: tuple[Path, ...]
    conflicts: tuple[str, ...] = ()
    metadata_updates: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class StageConflictResolution:
    """A complete winner selection with a recoverable quarantined loser."""

    resolution_id: str
    entries: tuple[dict[str, Any], ...]
    expires_at: str


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

    @_serialize_canonical_writes
    def promote(self, *, provider: Any | None = None) -> StagePromotion:
        """Promote completed files using two durable whole-batch checkpoints."""

        staged_files = self._completed_staged_files()
        batch = self._manifest.get("promotion_batch")
        if isinstance(batch, dict) and batch.get("state") == "promoted":
            new_paths = self._new_paths_after_promoted_batch(batch, staged_files)
            if not new_paths:
                return self._finish_promoted_batch(batch)
            entries = self._validate_promoted_batch(batch)
            self._cleanup_promoted_sources(entries, preserve=new_paths)
            self._manifest.pop("promotion_batch", None)
            staged_files = self._completed_staged_files()
        if not isinstance(batch, dict) or batch.get("state") != "prepared":
            batch = self._prepare_promotion_batch(staged_files, provider=provider)
        return self._execute_promotion_batch(batch, provider=provider)

    def _prepare_promotion_batch(
        self,
        staged_files: dict[str, Path],
        *,
        provider: Any | None,
    ) -> dict[str, Any]:
        """Persist every mutation and recovery source before touching targets."""

        planned = dict(self._manifest.get("planned") or {})
        promoted = dict(self._manifest.get("promoted") or {})
        metadata_updates = dict(self._manifest.get("metadata_updates") or {})
        for relative, staged in staged_files.items():
            planned[relative] = _file_identity(staged)

        entries: dict[str, dict[str, Any]] = {}
        conflicts: list[str] = []
        conflict_details: list[dict[str, Any]] = []
        for relative in sorted(planned):
            staged = self.root / Path(PurePosixPath(relative))
            target = self._canonical_target(relative, create_parent=False)
            entry: dict[str, Any] = {
                "relative_path": relative,
                "planned_identity": planned[relative],
            }
            if staged.exists() or staged.is_symlink():
                self._validate_regular_file(staged, label="staged")
                if target.exists() or target.is_symlink():
                    if target.is_symlink() or not target.is_file():
                        conflicts.append(relative)
                        conflict_details.append(_conflict_detail(relative, staged, target))
                        continue
                    if _files_equal(staged, target):
                        target_identity = _file_identity(target)
                        entry["action"] = (
                            "recovered_link"
                            if _same_inode(planned[relative], target_identity)
                            else "accept_existing"
                        )
                        entry["target_identity"] = target_identity
                    else:
                        update = _safe_metadata_update(
                            relative,
                            staged,
                            target,
                            provider=provider,
                            expected_source=self.source,
                        )
                        if update is None:
                            conflicts.append(relative)
                            conflict_details.append(_conflict_detail(relative, staged, target))
                            continue
                        metadata_updates[relative] = update
                        entry.update({
                            "action": "replace_metadata",
                            "retained_path": self._retained_relative(relative),
                            "previous_sha256": update["previous_sha256"],
                            "replacement_sha256": update["replacement_sha256"],
                        })
                else:
                    entry["action"] = "link"
            elif target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_file():
                    conflicts.append(relative)
                    conflict_details.append(_conflict_detail(relative, staged, target))
                    continue
                target_identity = _file_identity(target)
                recovered_update = metadata_updates.get(relative)
                if (
                    isinstance(recovered_update, dict)
                    and recovered_update.get("replacement_sha256") == _sha256(target)
                ):
                    entry.update({
                        "action": "recovered_metadata",
                        "replacement_sha256": recovered_update["replacement_sha256"],
                    })
                elif (
                    _same_inode(planned.get(relative), target_identity)
                    or _same_inode(promoted.get(relative), target_identity)
                ):
                    entry["action"] = "recovered_link"
                else:
                    conflicts.append(relative)
                    conflict_details.append(_conflict_detail(relative, staged, target))
                    continue
                entry["target_identity"] = target_identity
            else:
                raise DownloadStageError(
                    f"planned staged file is missing from both trees: {relative}"
                )
            entries[relative] = entry

        if conflicts:
            self._manifest.update({
                "state": "conflict",
                "planned": planned,
                "promoted": promoted,
                "metadata_updates": metadata_updates,
                "conflicts": conflicts,
                "conflict_details": conflict_details,
            })
            self._write_manifest()
            raise DownloadStageConflict(conflicts, conflict_details)

        batch = {"state": "prepared", "entries": entries}
        self._manifest.update({
            "state": "promoting",
            "planned": planned,
            "promoted": promoted,
            "metadata_updates": metadata_updates,
            "conflicts": [],
            "conflict_details": [],
            "promotion_batch": batch,
        })
        self._write_manifest()
        return batch

    def _execute_promotion_batch(
        self,
        batch: dict[str, Any],
        *,
        provider: Any | None,
    ) -> StagePromotion:
        """Apply a prepared batch, sync directories once, and checkpoint it."""

        entries = batch.get("entries")
        if not isinstance(entries, dict):
            raise DownloadStageManifestError("invalid prepared promotion batch")
        promoted = dict(self._manifest.get("promoted") or {})
        metadata_updates = dict(self._manifest.get("metadata_updates") or {})
        affected_directories: set[Path] = set()
        canonical_paths: list[Path] = []

        # Replacements consume the original staged name. Make every retained
        # recovery name durable as one batch BEFORE any such consumption.
        recovery_directories: set[Path] = set()
        for relative, entry in entries.items():
            if not isinstance(entry, dict) or entry.get("relative_path") != relative:
                raise DownloadStageManifestError("invalid promotion batch entry")
            if entry.get("action") not in {"replace_metadata", "recovered_metadata"}:
                continue
            staged = self.root / Path(PurePosixPath(relative))
            retained = self._retained_path(relative, entry)
            source = staged if staged.exists() or staged.is_symlink() else retained
            if source.exists() or source.is_symlink():
                self._validate_regular_file(source, label="metadata recovery")
                if _sha256(source) != entry.get("replacement_sha256"):
                    self._runtime_conflict(relative, source, self._canonical_target(relative, create_parent=False))
                self._ensure_retained_copy(source, retained, recovery_directories)
                self._add_directory_barriers(retained.parent, boundary=self.root, affected_directories=recovery_directories)
        for directory in sorted(recovery_directories, key=lambda path: str(path)):
            _fsync_directory(directory)

        for relative in sorted(entries):
            entry = entries[relative]
            if not isinstance(entry, dict) or entry.get("relative_path") != relative:
                raise DownloadStageManifestError("invalid promotion batch entry")
            staged = self.root / Path(PurePosixPath(relative))
            target = self._canonical_target(relative, create_parent=False)
            action = entry.get("action")
            if action in {"replace_metadata", "recovered_metadata"}:
                self._apply_metadata_promotion(
                    relative,
                    entry,
                    staged,
                    target,
                    provider=provider,
                    affected_directories=affected_directories,
                )
                update = metadata_updates.get(relative)
                if not isinstance(update, dict):
                    raise DownloadStageManifestError(
                        f"metadata recovery plan is missing: {relative}"
                    )
                update["state"] = "applied"
                metadata_updates[relative] = update
            else:
                self._apply_file_promotion(
                    relative,
                    entry,
                    staged,
                    target,
                    affected_directories=affected_directories,
                )

            if action != "accept_existing":
                self._add_directory_barriers(
                    target.parent,
                    boundary=self.download_root,
                    affected_directories=affected_directories,
                )
            if action in {"replace_metadata", "recovered_metadata"}:
                self._add_directory_barriers(
                    staged.parent,
                    boundary=self.root,
                    affected_directories=affected_directories,
                )
                self._add_directory_barriers(
                    self._retained_path(relative, entry).parent,
                    boundary=self.root,
                    affected_directories=affected_directories,
                )

            if not target.exists() or target.is_symlink() or not target.is_file():
                raise DownloadStageError(f"promoted target is unavailable: {relative}")
            target_identity = _file_identity(target)
            if action not in {"replace_metadata", "recovered_metadata"} and (
                staged.exists() or staged.is_symlink()
            ):
                if not _files_equal(staged, target) or not _same_inode(
                    target_identity,
                    _file_identity(target),
                ):
                    self._runtime_conflict(relative, staged, target)
            promoted[relative] = target_identity
            canonical_paths.append(target)

        for directory in sorted(affected_directories, key=lambda path: str(path)):
            _fsync_directory(directory)

        for relative in sorted(entries):
            target = self._canonical_target(relative, create_parent=False)
            if (
                not target.exists()
                or target.is_symlink()
                or not target.is_file()
                or not _same_inode(promoted.get(relative), _file_identity(target))
            ):
                staged = self.root / Path(PurePosixPath(relative))
                self._runtime_conflict(relative, staged, target)

        batch["state"] = "promoted"
        self._manifest.update({
            "state": "promoted",
            "promoted": promoted,
            "metadata_updates": metadata_updates,
            "promotion_batch": batch,
        })
        # This one manifest commit covers every canonical mutation in the batch.
        self._write_manifest()
        self._cleanup_promoted_sources(entries)
        return self._promotion_result(canonical_paths, metadata_updates)

    def _apply_file_promotion(
        self,
        relative: str,
        entry: dict[str, Any],
        staged: Path,
        target: Path,
        *,
        affected_directories: set[Path],
    ) -> None:
        action = entry.get("action")
        if action not in {"link", "accept_existing", "recovered_link"}:
            raise DownloadStageManifestError(
                f"invalid file promotion action: {action!r}"
            )
        staged_exists = staged.exists() or staged.is_symlink()
        if staged_exists:
            self._validate_regular_file(staged, label="staged")
            if not _same_inode(entry.get("planned_identity"), _file_identity(staged)):
                raise DownloadStageManifestError(
                    f"staged file changed after promotion planning: {relative}"
                )

        if action == "recovered_link" and not staged_exists:
            if self._target_has_recorded_identity(relative, entry, target):
                return
            self._runtime_conflict(relative, staged, target)

        if not staged_exists:
            if self._target_has_recorded_identity(relative, entry, target):
                return
            raise DownloadStageError(
                f"planned staged file is missing from both trees: {relative}"
            )

        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or not _files_equal(staged, target):
                self._runtime_conflict(relative, staged, target)
            return

        self._ensure_parent(target.parent, affected_directories)
        try:
            os.link(staged, target, follow_symlinks=False)
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or not _files_equal(staged, target):
                self._runtime_conflict(relative, staged, target)
        except OSError as exc:
            raise DownloadStageError(
                f"same-volume promotion failed for {relative}: {type(exc).__name__}"
            ) from exc
        affected_directories.add(target.parent)

    def _apply_metadata_promotion(
        self,
        relative: str,
        entry: dict[str, Any],
        staged: Path,
        target: Path,
        *,
        provider: Any | None,
        affected_directories: set[Path],
    ) -> None:
        expected_update = (self._manifest.get("metadata_updates") or {}).get(relative)
        if not isinstance(expected_update, dict):
            raise DownloadStageManifestError(f"metadata recovery plan is missing: {relative}")
        replacement_sha = str(expected_update.get("replacement_sha256") or "")
        previous_sha = str(expected_update.get("previous_sha256") or "")
        retained = self._retained_path(relative, entry)
        source = staged if staged.exists() or staged.is_symlink() else retained
        if source.exists() or source.is_symlink():
            self._validate_regular_file(source, label="staged recovery")
            if _sha256(source) != replacement_sha:
                self._runtime_conflict(relative, source, target)

        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                self._runtime_conflict(relative, source, target)
            target_identity = _file_identity(target)
            target_sha = _sha256(target)
            if target_sha == replacement_sha:
                return
            if target_sha != previous_sha or not source.exists():
                self._runtime_conflict(relative, source, target)
            current_update = _safe_metadata_update(
                relative,
                source,
                target,
                provider=provider,
                expected_source=self.source,
            )
            if (
                current_update is None
                or current_update.get("previous_sha256") != previous_sha
                or current_update.get("replacement_sha256") != replacement_sha
            ):
                self._runtime_conflict(relative, source, target)
            self._ensure_retained_copy(source, retained, affected_directories)
            if source != staged:
                self._ensure_parent(staged.parent, affected_directories)
                try:
                    os.link(source, staged, follow_symlinks=False)
                except FileExistsError:
                    if staged.is_symlink() or not staged.is_file() or _sha256(staged) != replacement_sha:
                        self._runtime_conflict(relative, staged, target)
                affected_directories.add(staged.parent)
            # All application promotion/conflict writers share the outer
            # mutex. Recheck after parsing and recovery setup as well, so a
            # target changed at that boundary cannot be silently overwritten.
            if target.is_symlink() or not target.is_file() or not _same_inode(target_identity, _file_identity(target)):
                self._runtime_conflict(relative, source, target)
            os.replace(staged, target)
            affected_directories.update({staged.parent, target.parent})
            return

        if not source.exists():
            raise DownloadStageError(
                f"metadata replacement is missing from both trees: {relative}"
            )
        self._ensure_retained_copy(source, retained, affected_directories)
        self._ensure_parent(target.parent, affected_directories)
        try:
            os.link(source, target, follow_symlinks=False)
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or _sha256(target) != replacement_sha:
                self._runtime_conflict(relative, source, target)
        affected_directories.add(target.parent)

    def _ensure_retained_copy(
        self,
        source: Path,
        retained: Path,
        affected_directories: set[Path],
    ) -> None:
        self._ensure_parent(retained.parent, affected_directories)
        if retained.exists() or retained.is_symlink():
            self._validate_regular_file(retained, label="retained staged")
            if not os.path.samefile(source, retained) and not _files_equal(source, retained):
                raise DownloadStageManifestError("retained metadata recovery copy changed")
            return
        try:
            os.link(source, retained, follow_symlinks=False)
        except FileExistsError:
            self._validate_regular_file(retained, label="retained staged")
            if not _files_equal(source, retained):
                raise DownloadStageManifestError("retained metadata recovery copy changed")
        affected_directories.add(retained.parent)

    def _finish_promoted_batch(self, batch: dict[str, Any]) -> StagePromotion:
        entries = self._validate_promoted_batch(batch)
        self._cleanup_promoted_sources(entries)
        return self._promotion_result(
            [self._canonical_target(relative, create_parent=False) for relative in sorted(entries)],
            dict(self._manifest.get("metadata_updates") or {}),
        )

    def _validate_promoted_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        entries = batch.get("entries")
        if not isinstance(entries, dict):
            raise DownloadStageManifestError("invalid promoted promotion batch")
        promoted = self._manifest.get("promoted") or {}
        for relative in sorted(entries):
            target = self._canonical_target(relative, create_parent=False)
            if (
                not target.exists()
                or target.is_symlink()
                or not target.is_file()
                or not _same_inode(promoted.get(relative), _file_identity(target))
            ):
                staged = self.root / Path(PurePosixPath(relative))
                self._runtime_conflict(relative, staged, target)
        # A previous fsync can fail after the manifest rename. Re-establish
        # that final checkpoint's directory durability before source cleanup.
        _fsync_directory(self.root)
        return entries

    def _new_paths_after_promoted_batch(
        self,
        batch: dict[str, Any],
        staged_files: dict[str, Path],
    ) -> set[str]:
        entries = batch.get("entries")
        if not isinstance(entries, dict):
            raise DownloadStageManifestError("invalid promoted promotion batch")
        new_paths: set[str] = set()
        for relative, staged in staged_files.items():
            entry = entries.get(relative)
            if (
                not isinstance(entry, dict)
                or not _same_inode(entry.get("planned_identity"), _file_identity(staged))
            ):
                new_paths.add(relative)
        return new_paths

    def _cleanup_promoted_sources(
        self,
        entries: dict[str, Any],
        *,
        preserve: set[str] | None = None,
    ) -> None:
        preserve = preserve or set()
        for relative in sorted(entries):
            if relative in preserve:
                continue
            entry = entries[relative]
            staged = self.root / Path(PurePosixPath(relative))
            retained = (
                self._retained_path(relative, entry)
                if isinstance(entry, dict) and entry.get("retained_path")
                else None
            )
            if staged.exists() or staged.is_symlink():
                self._validate_regular_file(staged, label="staged cleanup")
                staged.unlink()
            if retained is not None and (retained.exists() or retained.is_symlink()):
                self._validate_regular_file(retained, label="retained staged cleanup")
                retained.unlink()
        self._remove_empty_directories()

    def _promotion_result(
        self,
        canonical_paths: list[Path],
        metadata_updates: dict[str, Any],
    ) -> StagePromotion:
        applied_updates = tuple(
            dict(update)
            for _, update in sorted(metadata_updates.items())
            if isinstance(update, dict) and update.get("state") == "applied"
        )
        return StagePromotion(tuple(canonical_paths), metadata_updates=applied_updates)

    def _target_has_recorded_identity(
        self,
        relative: str,
        entry: dict[str, Any],
        target: Path,
    ) -> bool:
        if not target.exists() or target.is_symlink() or not target.is_file():
            return False
        identity = _file_identity(target)
        return any(
            _same_inode(expected, identity)
            for expected in (
                entry.get("target_identity"),
                entry.get("planned_identity"),
                (self._manifest.get("planned") or {}).get(relative),
                (self._manifest.get("promoted") or {}).get(relative),
            )
        )

    def _runtime_conflict(self, relative: str, staged: Path, target: Path) -> None:
        detail = _conflict_detail(relative, staged, target)
        self._record_runtime_conflict(relative, detail=detail)
        raise DownloadStageConflict([relative], [detail])

    def _retained_relative(self, relative: str) -> str:
        return (Path(RETAINED_PROMOTION_DIR) / Path(PurePosixPath(relative))).as_posix()

    def _retained_path(self, relative: str, entry: dict[str, Any]) -> Path:
        expected = self._retained_relative(relative)
        recorded = str(entry.get("retained_path") or expected)
        if recorded != expected:
            raise DownloadStageManifestError(
                f"invalid retained promotion path: {recorded!r}"
            )
        return self.root / Path(PurePosixPath(recorded))

    def _ensure_parent(
        self,
        directory: Path,
        affected_directories: set[Path],
    ) -> None:
        missing: list[Path] = []
        current = directory
        while not current.exists():
            missing.append(current)
            current = current.parent
        directory.mkdir(parents=True, exist_ok=True)
        for created in missing:
            affected_directories.add(created.parent)

    @staticmethod
    def _add_directory_barriers(
        directory: Path,
        *,
        boundary: Path,
        affected_directories: set[Path],
    ) -> None:
        try:
            directory.relative_to(boundary)
        except ValueError as exc:
            raise DownloadStageError("promotion directory escapes managed root") from exc
        current = directory
        while True:
            if current.exists():
                affected_directories.add(current)
            if current == boundary:
                return
            current = current.parent

    @_serialize_canonical_writes
    def resolve_conflicts(
        self,
        decisions: dict[str, str],
        *,
        resolution_id: str,
        retention_days: int = 30,
    ) -> StageConflictResolution:
        """Apply one decision for every conflict and preserve every loser.

        This is deliberately separate from :meth:`promote`: ordinary retries
        remain fail-closed until an explicit or evidence-backed resolution has
        made the canonical identity durable in the stage manifest.
        """

        if not resolution_id or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for char in resolution_id
        ):
            raise DownloadStageManifestError("invalid conflict resolution id")
        if any(value not in {"canonical", "staged"} for value in decisions.values()):
            raise DownloadStageManifestError("conflict winner must be canonical or staged")

        existing = self._manifest.get("resolution")
        if isinstance(existing, dict) and existing.get("state") in {"prepared", "applied"}:
            prepared = [dict(entry) for entry in existing.get("entries") or ()]
            recorded_decisions = {
                str(entry.get("relative_path")): str(entry.get("winner"))
                for entry in prepared
            }
            if decisions != recorded_decisions:
                raise DownloadStageManifestError(
                    "conflict resolution decisions do not match the prepared batch"
                )
            resolution_id = str(existing["resolution_id"])
            expires_at = str(existing["expires_at"])
            if existing.get("state") == "applied":
                return StageConflictResolution(
                    resolution_id=resolution_id,
                    entries=tuple(prepared),
                    expires_at=expires_at,
                )
        else:
            conflicts = tuple(
                str(value) for value in self._manifest.get("conflicts") or []
            )
            if not conflicts:
                raise DownloadStageManifestError("stage has no unresolved conflicts")
            if set(decisions) != set(conflicts):
                raise DownloadStageManifestError(
                    "conflict resolution requires exactly one decision per conflict"
                )

            details = {
                str(item.get("relative_path")): item
                for item in self._manifest.get("conflict_details") or []
                if isinstance(item, dict) and item.get("relative_path")
            }
            prepared = []
            for relative in conflicts:
                staged = self.root / Path(PurePosixPath(relative))
                target = self._canonical_target(relative)
                staged_sha = self._conflict_file_hash(
                    staged,
                    label="staged conflict",
                )
                canonical_sha = self._conflict_file_hash(
                    target,
                    label="canonical conflict",
                )
                detail = details.get(relative) or {}
                if detail.get("staged_sha256") not in {None, staged_sha}:
                    raise DownloadStageManifestError(
                        f"staged conflict changed before resolution: {relative}"
                    )
                if detail.get("canonical_sha256") not in {None, canonical_sha}:
                    raise DownloadStageManifestError(
                        f"canonical conflict changed before resolution: {relative}"
                    )
                loser = "canonical" if decisions[relative] == "staged" else "staged"
                quarantine_namespace = hashlib.sha256(
                    resolution_id.encode("utf-8", "surrogatepass")
                ).hexdigest()[:32]
                quarantine_relative = (
                    Path(".conflict-quarantine")
                    / quarantine_namespace
                    / loser
                    / Path(PurePosixPath(relative))
                )
                self._canonical_target(quarantine_relative.as_posix())
                prepared.append({
                    "relative_path": relative,
                    "winner": decisions[relative],
                    "staged_sha256": staged_sha,
                    "canonical_sha256": canonical_sha,
                    "winner_sha256": (
                        staged_sha
                        if decisions[relative] == "staged"
                        else canonical_sha
                    ),
                    "loser_sha256": (
                        canonical_sha
                        if decisions[relative] == "staged"
                        else staged_sha
                    ),
                    "quarantine_path": quarantine_relative.as_posix(),
                    "state": "pending",
                })

            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=max(1, int(retention_days)))
            ).isoformat()
            self._manifest["resolution"] = {
                "resolution_id": resolution_id,
                "state": "prepared",
                "entries": prepared,
                "expires_at": expires_at,
            }
            self._write_manifest()

        for entry in prepared:
            self._resume_conflict_resolution_entry(entry)
            entry["state"] = "applied"
            target = self._canonical_target(str(entry["relative_path"]))
            target_identity = _file_identity(target)
            self._manifest.setdefault("planned", {})[
                str(entry["relative_path"])
            ] = target_identity
            self._manifest.setdefault("promoted", {})[
                str(entry["relative_path"])
            ] = target_identity
            self._manifest["resolution"]["entries"] = prepared
            self._write_manifest()

        self._manifest["state"] = "resolved"
        self._manifest["conflicts"] = []
        self._manifest["conflict_details"] = []
        self._manifest["resolution"]["state"] = "applied"
        self._write_manifest()
        return StageConflictResolution(
            resolution_id=resolution_id,
            entries=tuple(dict(entry) for entry in prepared),
            expires_at=expires_at,
        )

    @staticmethod
    def _conflict_file_hash(
        path: Path,
        *,
        label: str,
        allow_missing: bool = False,
    ) -> str | None:
        if path.is_symlink():
            raise DownloadStageManifestError(f"{label} path cannot be a symlink")
        try:
            info = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return None
            raise DownloadStageManifestError(f"{label} file is missing") from None
        if not stat.S_ISREG(info.st_mode):
            raise DownloadStageManifestError(f"{label} path is not a regular file")
        return _sha256(path)

    def _resume_conflict_resolution_entry(self, entry: dict[str, Any]) -> None:
        """Reach the prepared file state without discarding either version."""

        relative = str(entry["relative_path"])
        staged = self.root / Path(PurePosixPath(relative))
        target = self._canonical_target(relative)
        quarantine = self._canonical_target(str(entry["quarantine_path"]))
        staged_sha = self._conflict_file_hash(
            staged,
            label="staged conflict",
            allow_missing=True,
        )
        target_sha = self._conflict_file_hash(
            target,
            label="canonical conflict",
        )
        quarantine_sha = self._conflict_file_hash(
            quarantine,
            label="conflict quarantine",
            allow_missing=True,
        )
        winner_sha = str(entry["winner_sha256"])
        loser_sha = str(entry["loser_sha256"])

        if (
            target_sha == winner_sha
            and quarantine_sha == loser_sha
            and staged_sha is None
        ):
            _fsync_directory(target.parent)
            _fsync_directory(quarantine.parent)
            return

        if entry["winner"] == "staged":
            if target_sha != loser_sha or staged_sha != winner_sha:
                raise DownloadStageManifestError(
                    f"conflict files changed while resuming resolution: {relative}"
                )
            if quarantine_sha is None:
                try:
                    os.link(target, quarantine, follow_symlinks=False)
                except FileExistsError:
                    quarantine_sha = self._conflict_file_hash(
                        quarantine,
                        label="conflict quarantine",
                    )
                else:
                    quarantine_sha = loser_sha
                    _fsync_directory(quarantine.parent)
            if quarantine_sha != loser_sha:
                raise DownloadStageManifestError(
                    f"conflict quarantine changed: {relative}"
                )
            os.replace(staged, target)
            _fsync_directory(target.parent)
        else:
            if (
                target_sha != winner_sha
                or staged_sha != loser_sha
                or quarantine_sha is not None
            ):
                raise DownloadStageManifestError(
                    f"conflict files changed while resuming resolution: {relative}"
                )
            os.replace(staged, quarantine)
            _fsync_directory(quarantine.parent)

        if (
            self._conflict_file_hash(target, label="resolved canonical conflict")
            != winner_sha
            or self._conflict_file_hash(
                quarantine,
                label="resolved conflict quarantine",
            )
            != loser_sha
            or self._conflict_file_hash(
                staged,
                label="resolved staged conflict",
                allow_missing=True,
            )
            is not None
        ):
            raise DownloadStageManifestError(
                f"conflict resolution did not reach a durable state: {relative}"
            )

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
            _fsync_directory(self.root)
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
                if current == self.root and directory_name == RETAINED_PROMOTION_DIR:
                    directory_names.remove(directory_name)
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

    def _canonical_target(self, relative: str, *, create_parent: bool = True) -> Path:
        _validate_relative(relative)
        target = self.download_root.joinpath(*PurePosixPath(relative).parts)
        if create_parent:
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

    def _record_runtime_conflict(self, relative: str, *, detail: dict[str, Any] | None = None) -> None:
        conflicts = list(self._manifest.get("conflicts") or [])
        if relative not in conflicts:
            conflicts.append(relative)
        self._manifest["conflicts"] = conflicts
        if detail is not None:
            details = list(self._manifest.get("conflict_details") or [])
            details.append(detail)
            self._manifest["conflict_details"] = details
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_metadata_update(
    relative: str,
    staged: Path,
    target: Path,
    *,
    provider: Any | None,
    expected_source: str | None,
) -> dict[str, Any] | None:
    if provider is None or staged.suffix.lower() != ".json" or target.suffix.lower() != ".json":
        return None
    if expected_source and getattr(provider, "source_name", None) != expected_source:
        return None
    if staged.stat().st_size > MAX_SAFE_METADATA_BYTES or target.stat().st_size > MAX_SAFE_METADATA_BYTES:
        return None
    try:
        previous = json.loads(target.read_text(encoding="utf-8"))
        replacement = json.loads(staged.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or not isinstance(replacement, dict):
            return None
        previous_work = provider.parse_work_source(previous)
        replacement_work = provider.parse_work_source(replacement)
        previous_work_id = str(previous_work.get("source_work_id") or "").strip()
        replacement_work_id = str(replacement_work.get("source_work_id") or "").strip()
        previous_creator_id = str(previous_work.get("source_creator_id") or "").strip()
        replacement_creator_id = str(replacement_work.get("source_creator_id") or "").strip()
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if (
        not previous_work_id
        or previous_work_id != replacement_work_id
        or not previous_creator_id
        or previous_creator_id != replacement_creator_id
    ):
        return None
    changed_fields = sorted(
        key
        for key in set(previous) | set(replacement)
        if previous.get(key) != replacement.get(key)
    )
    return {
        "relative_path": relative,
        "classification": "same_work_metadata_update",
        "source": expected_source or getattr(provider, "source_name", None),
        "source_work_id": previous_work_id,
        "source_creator_id": previous_creator_id,
        "previous_sha256": _sha256(target),
        "replacement_sha256": _sha256(staged),
        "previous_metadata": previous,
        "changed_fields": changed_fields,
        "staged_identity": _file_identity(staged),
        "target_identity": _file_identity(target),
        "state": "prepared",
    }


def _conflict_detail(relative: str, staged: Path, target: Path) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "relative_path": relative,
        "file_type": "metadata" if Path(relative).suffix.lower() == ".json" else "media",
        "classification": "unsafe_existing_target",
    }
    if staged.exists() and staged.is_file() and not staged.is_symlink():
        detail["staged_sha256"] = _sha256(staged)
    if target.exists() and target.is_file() and not target.is_symlink():
        detail["canonical_sha256"] = _sha256(target)
    return detail


def _fsync_directory(path: Path) -> None:
    """Require a durable directory barrier before consuming recovery names."""

    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
