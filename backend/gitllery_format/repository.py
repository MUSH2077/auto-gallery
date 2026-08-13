"""Bounded, append-only Gitllery v1 segment repositories.

This module deliberately has no application or database imports.  It is used
by the projection worker and by the standalone read-only CLI.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import zlib

PRODUCT_VERSION = "v1"
FORMAT_ID = "gitllery-segment"
FORMAT_REVISION = 1
MAX_COMMITS_PER_SEGMENT = 100
MAX_SEGMENT_BYTES = 4 * 1024 * 1024
MAX_CHANGES_PER_FRAME = 25
MAX_FRAME_BYTES = 1024 * 1024


class GitlleryFormatError(ValueError):
    """The repository is missing, malformed, or fails integrity checks."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    segments: int
    commits: int
    changes: int
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "segments": self.segments,
            "commits": self.commits,
            "changes": self.changes,
            "errors": list(self.errors),
        }


class SegmentRepository:
    """Read and publish one self-contained Gitllery v1 repository."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        self.manifest_path = self.root / "manifest.json"
        self.config_path = self.root / "config.json"
        self.segments_dir = self.root / "segments"

    @classmethod
    def discover(cls, start: str | os.PathLike[str]) -> "SegmentRepository":
        current = Path(start).resolve()
        if current.is_file():
            current = current.parent
        for directory in (current, *current.parents):
            candidate = directory if directory.name == ".gitllery" else directory / ".gitllery"
            if (candidate / "manifest.json").is_file():
                return cls(candidate)
        raise GitlleryFormatError("no Gitllery v1 repository found")

    def exists(self) -> bool:
        return self.manifest_path.is_file()

    @contextmanager
    def projection_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "projection.lock"
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def initialise(
        self,
        *,
        repository_id: str,
        source: str | None = None,
        creator_dir: str | None = None,
        generation: str | None = None,
    ) -> None:
        if self.exists():
            self.read_manifest()
            return
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "product": "gitllery",
            "product_version": PRODUCT_VERSION,
            "format_id": FORMAT_ID,
            "format_revision": FORMAT_REVISION,
            "repository_id": str(repository_id),
            "source": source,
            "creator_dir": creator_dir,
        }
        manifest = {
            "product": "gitllery",
            "product_version": PRODUCT_VERSION,
            "format_id": FORMAT_ID,
            "format_revision": FORMAT_REVISION,
            "repository_id": str(repository_id),
            "generation": generation or _now(),
            "head_segment": None,
            "last_complete_commit_id": None,
            "last_complete_position": None,
            "segment_count": 0,
            "commit_count": 0,
            "change_count": 0,
            "updated_at": _now(),
        }
        _atomic_json(self.config_path, config)
        _atomic_json(self.manifest_path, manifest)

    @staticmethod
    def _validate_header(value: dict[str, Any], *, kind: str) -> None:
        if value.get("product_version") != PRODUCT_VERSION:
            raise GitlleryFormatError(f"unsupported {kind} product version")
        if value.get("format_id") != FORMAT_ID:
            raise GitlleryFormatError(f"unsupported {kind} format")
        if value.get("format_revision") != FORMAT_REVISION:
            raise GitlleryFormatError(f"unsupported {kind} format revision")

    def read_manifest(self) -> dict[str, Any]:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GitlleryFormatError("cannot read Gitllery manifest") from exc
        self._validate_header(manifest, kind="manifest")
        return manifest

    def _segment_path(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise GitlleryFormatError("invalid segment digest")
        return self.segments_dir / digest[:2] / f"{digest}.z"

    def read_segment(self, digest: str) -> dict[str, Any]:
        try:
            compressed = self._segment_path(digest).read_bytes()
            raw = zlib.decompress(compressed)
            segment = json.loads(raw)
        except (OSError, zlib.error, json.JSONDecodeError) as exc:
            raise GitlleryFormatError(f"cannot read segment {digest}") from exc
        if digest_bytes(raw) != digest:
            raise GitlleryFormatError(f"segment hash mismatch: {digest}")
        self._validate_header(segment, kind="segment")
        return segment

    def _write_segment(self, segment: dict[str, Any]) -> str:
        raw = canonical_bytes(segment)
        if len(raw) > MAX_SEGMENT_BYTES:
            raise GitlleryFormatError("segment exceeds 4 MiB uncompressed limit")
        digest = digest_bytes(raw)
        target = self._segment_path(digest)
        if target.exists():
            self.read_segment(digest)
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{digest}.", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(zlib.compress(raw))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            _fsync_dir(target.parent)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return digest

    @staticmethod
    def _frames_for_commit(commit: dict[str, Any]) -> list[dict[str, Any]]:
        changes = list(commit.get("changes") or [])
        base = {key: value for key, value in commit.items() if key != "changes"}
        if not changes:
            return [{**base, "frame": 0, "frame_count": 1, "changes": []}]
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for change in changes:
            candidate = [*current, change]
            encoded = canonical_bytes({**base, "changes": candidate})
            if current and (
                len(current) >= MAX_CHANGES_PER_FRAME or len(encoded) > MAX_FRAME_BYTES
            ):
                chunks.append(current)
                current = [change]
            else:
                current = candidate
            if len(canonical_bytes({**base, "changes": current})) > MAX_FRAME_BYTES:
                raise GitlleryFormatError("one curation change exceeds 1 MiB frame limit")
        if current:
            chunks.append(current)
        return [
            {**base, "frame": index, "frame_count": len(chunks), "changes": chunk}
            for index, chunk in enumerate(chunks)
        ]

    def append(self, commits: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Publish bounded segments, then atomically advance the manifest."""

        manifest = self.read_manifest()
        pending = list(commits)
        if not pending:
            return manifest
        if (
            len(pending) == 1
            and str(pending[0].get("commit_id"))
            == str(manifest.get("last_complete_commit_id"))
        ):
            return manifest
        previous_commit = manifest.get("last_complete_commit_id")
        previous_segment = manifest.get("head_segment")
        segment_count = int(manifest.get("segment_count") or 0)
        commit_count = int(manifest.get("commit_count") or 0)
        change_count = int(manifest.get("change_count") or 0)
        all_frames: list[dict[str, Any]] = []
        for commit in pending:
            commit = dict(commit)
            commit_id = str(commit.get("commit_id") or "")
            if not commit_id:
                raise GitlleryFormatError("commit_id is required")
            parent = commit.get("parent_commit_id")
            if previous_commit is not None and str(parent) != str(previous_commit):
                raise GitlleryFormatError(
                    f"commit {commit_id} does not follow {previous_commit}"
                )
            all_frames.extend(self._frames_for_commit(commit))
            previous_commit = commit_id
            commit_count += 1
            change_count += len(commit.get("changes") or [])

        frames: list[dict[str, Any]] = []
        segment_commit_ids: set[str] = set()

        def publish_segment() -> None:
            nonlocal frames, segment_commit_ids, previous_segment, segment_count
            if not frames:
                return
            segment = {
                "product": "gitllery",
                "product_version": PRODUCT_VERSION,
                "format_id": FORMAT_ID,
                "format_revision": FORMAT_REVISION,
                "repository_id": manifest["repository_id"],
                "sequence": segment_count + 1,
                "previous_segment": previous_segment,
                "frames": frames,
            }
            previous_segment = self._write_segment(segment)
            segment_count += 1
            frames = []
            segment_commit_ids = set()

        for frame in all_frames:
            commit_id = str(frame["commit_id"])
            candidate_ids = segment_commit_ids | {commit_id}
            candidate = {
                "product": "gitllery",
                "product_version": PRODUCT_VERSION,
                "format_id": FORMAT_ID,
                "format_revision": FORMAT_REVISION,
                "repository_id": manifest["repository_id"],
                "sequence": segment_count + 1,
                "previous_segment": previous_segment,
                "frames": [*frames, frame],
            }
            if frames and (
                len(candidate_ids) > MAX_COMMITS_PER_SEGMENT
                or len(canonical_bytes(candidate)) > MAX_SEGMENT_BYTES
            ):
                publish_segment()
                candidate = {**candidate, "sequence": segment_count + 1, "previous_segment": previous_segment, "frames": [frame]}
            if len(canonical_bytes(candidate)) > MAX_SEGMENT_BYTES:
                raise GitlleryFormatError("one continuation frame exceeds segment limit")
            frames.append(frame)
            segment_commit_ids.add(commit_id)
        publish_segment()
        updated = {
            **manifest,
            "head_segment": previous_segment,
            "last_complete_commit_id": previous_commit,
            "last_complete_position": {
                "segment": previous_segment,
                "sequence": segment_count,
            },
            "segment_count": segment_count,
            "commit_count": commit_count,
            "change_count": change_count,
            "updated_at": _now(),
        }
        _atomic_json(self.manifest_path, updated)
        return updated

    def iter_segments(self, *, newest_first: bool = False) -> Iterator[tuple[str, dict[str, Any]]]:
        digest = self.read_manifest().get("head_segment")
        chain: list[str] = []
        seen: set[str] = set()
        while digest:
            if digest in seen:
                raise GitlleryFormatError("segment chain contains a cycle")
            seen.add(digest)
            segment = self.read_segment(digest)
            if newest_first:
                yield digest, segment
            else:
                chain.append(digest)
            digest = segment.get("previous_segment")
        if not newest_first:
            for digest in reversed(chain):
                yield digest, self.read_segment(digest)

    def iter_commits(self, *, newest_first: bool = False) -> Iterator[dict[str, Any]]:
        current_id: str | None = None
        frames: list[dict[str, Any]] = []

        def assemble(items: list[dict[str, Any]]) -> dict[str, Any]:
            ordered = sorted(items, key=lambda item: int(item.get("frame") or 0))
            count = int(ordered[0].get("frame_count") or 1)
            if len(ordered) != count or [int(item.get("frame") or 0) for item in ordered] != list(range(count)):
                raise GitlleryFormatError(
                    f"incomplete continuation frames for {ordered[0].get('commit_id')}"
                )
            header = {
                key: value
                for key, value in ordered[0].items()
                if key not in {"changes", "frame", "frame_count"}
            }
            return {
                **header,
                "changes": [
                    change
                    for frame in ordered
                    for change in (frame.get("changes") or [])
                ],
            }

        for _, segment in self.iter_segments(newest_first=newest_first):
            segment_frames = list(segment.get("frames") or [])
            if newest_first:
                segment_frames.reverse()
            for frame in segment_frames:
                commit_id = str(frame.get("commit_id") or "")
                if not commit_id:
                    raise GitlleryFormatError("segment frame has no commit_id")
                if current_id is not None and commit_id != current_id:
                    yield assemble(frames)
                    frames = []
                current_id = commit_id
                frames.append(frame)
        if frames:
            yield assemble(frames)

    def find_commit(self, commit_id: str) -> dict[str, Any] | None:
        for commit in self.iter_commits(newest_first=True):
            if str(commit.get("commit_id")) == str(commit_id):
                return commit
        return None

    def verify(self, *, deep: bool = True) -> VerifyResult:
        errors: list[str] = []
        segments = commits = changes = 0
        previous_segment: str | None = None
        previous_commit: str | None = None
        try:
            manifest = self.read_manifest()
            try:
                config = json.loads(self.config_path.read_text(encoding="utf-8"))
                self._validate_header(config, kind="config")
                if config.get("repository_id") != manifest.get("repository_id"):
                    errors.append("config repository does not match manifest")
            except (OSError, json.JSONDecodeError, GitlleryFormatError) as exc:
                errors.append(f"cannot verify Gitllery config: {exc}")
            for digest, segment in self.iter_segments():
                segments += 1
                if segment.get("previous_segment") != previous_segment:
                    errors.append(f"segment {digest} has a broken parent")
                if int(segment.get("sequence") or 0) != segments:
                    errors.append(f"segment {digest} has an invalid sequence")
                if segment.get("repository_id") != manifest.get("repository_id"):
                    errors.append(f"segment {digest} belongs to another repository")
                previous_segment = digest
            if deep:
                for commit in self.iter_commits():
                    commits += 1
                    if previous_commit is not None and str(commit.get("parent_commit_id")) != previous_commit:
                        errors.append(f"commit {commit.get('commit_id')} has a broken parent")
                    previous_commit = str(commit.get("commit_id"))
                    changes += len(commit.get("changes") or [])
            if previous_segment != manifest.get("head_segment"):
                errors.append("manifest head does not match segment chain")
            if deep and previous_commit != manifest.get("last_complete_commit_id"):
                errors.append("manifest commit watermark does not match history")
            if int(manifest.get("segment_count") or 0) != segments:
                errors.append("manifest segment count is incorrect")
            if deep and int(manifest.get("commit_count") or 0) != commits:
                errors.append("manifest commit count is incorrect")
            if deep and int(manifest.get("change_count") or 0) != changes:
                errors.append("manifest change count is incorrect")
        except GitlleryFormatError as exc:
            errors.append(str(exc))
        return VerifyResult(not errors, segments, commits, changes, tuple(errors))

    def export(self) -> dict[str, Any]:
        return {
            "manifest": self.read_manifest(),
            "commits": list(self.iter_commits()),
        }
