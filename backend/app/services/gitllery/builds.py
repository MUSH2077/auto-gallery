"""Resumable, side-by-side Gitllery build and promotion primitives.

The build worker is intentionally separate from ordinary outbox projection.
Nothing in this module changes the active ``.gitllery`` directory unless the
explicit promotion helper is called after every gate has passed.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Mapping
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    CurationChange,
    CurationCommit,
    GitlleryBuild,
    GitlleryProjectionOutbox,
    GitlleryRepositoryState,
)
from app.services.gitllery.slicing import RepoDescriptor, RepoResolver
from gitllery_format import SegmentRepository
from gitllery_format.repository import canonical_bytes, digest_bytes


GITLLERY_MAX_WORKER_MEMORY_MB = 512.0
GITLLERY_MAX_API_REGRESSION_PERCENT = 10.0
GITLLERY_MAX_BUILD_HOURS = 24.0
GITLLERY_MAX_STORAGE_BYTES = 2 * 1024 * 1024 * 1024
_SAFE_GENERATION = re.compile(r"^[A-Za-z0-9_-]+$")


class GitlleryPromotionError(RuntimeError):
    """A verified generation cannot be safely promoted."""


def assert_active_repository_ready(repository: SegmentRepository) -> None:
    """Refuse active writes unless the promoted generation is explicit."""

    verified = settings.gitllery_active_verified_generation.strip()
    if not verified:
        raise GitlleryPromotionError("active Gitllery projection has no verified generation assertion")
    if not repository.exists():
        raise GitlleryPromotionError("active Gitllery repository has not been atomically promoted")
    manifest = repository.read_manifest()
    if str(manifest.get("generation") or "") != verified:
        raise GitlleryPromotionError("active Gitllery repository generation is not verified")


def select_canary_repository_keys(counts: Mapping[str, int]) -> list[str]:
    """Return stable smallest, median, and largest repository samples."""

    ranked = sorted(
        ((max(0, int(count)), str(key)) for key, count in counts.items()),
        key=lambda item: (item[0], item[1]),
    )
    if not ranked:
        return []
    positions = (0, (len(ranked) - 1) // 2, len(ranked) - 1)
    return list(dict.fromkeys(ranked[position][1] for position in positions))


def evaluate_activation_gates(
    evidence: Mapping[str, Any],
    *,
    require_incremental_soak: bool,
) -> dict[str, Any]:
    """Evaluate fail-closed resource, recovery, and performance evidence."""

    checks = {
        "integrity_exact": evidence.get("integrity_exact") is True,
        "duplicate_free": evidence.get("duplicate_free") is True,
        "resume_verified": evidence.get("resume_verified") is True,
        "restore_dry_run_verified": evidence.get("restore_dry_run_verified") is True,
        "worker_memory_mb": (
            isinstance(evidence.get("worker_memory_mb"), (int, float)) and float(evidence["worker_memory_mb"]) <= GITLLERY_MAX_WORKER_MEMORY_MB
        ),
        "api_p95_regression_percent": (
            isinstance(evidence.get("api_p95_regression_percent"), (int, float))
            and float(evidence["api_p95_regression_percent"]) <= GITLLERY_MAX_API_REGRESSION_PERCENT
        ),
        "projected_duration_hours": (
            isinstance(evidence.get("projected_duration_hours"), (int, float)) and float(evidence["projected_duration_hours"]) < GITLLERY_MAX_BUILD_HOURS
        ),
        "projected_storage_bytes": (
            isinstance(evidence.get("projected_storage_bytes"), (int, float)) and int(evidence["projected_storage_bytes"]) < GITLLERY_MAX_STORAGE_BYTES
        ),
    }
    if require_incremental_soak:
        checks["incremental_soak_hours"] = (
            isinstance(evidence.get("incremental_soak_hours"), (int, float)) and float(evidence["incremental_soak_hours"]) >= 24.0
        )
    failed = [name for name, passed in checks.items() if not passed]
    return {"passed": not failed, "checks": checks, "failed": failed}


def _safe_generation(generation: str) -> str:
    value = str(generation or "").strip()
    if not value or not _SAFE_GENERATION.fullmatch(value):
        raise GitlleryPromotionError("invalid Gitllery generation")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def promote_verified_generation(
    creator_root: str | os.PathLike[str],
    *,
    generation: str,
    gates: Mapping[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Atomically promote one verified generation, preserving legacy v0.

    The two renames are deliberately resumable: if a process stops after the
    legacy directory moves, a retry observes the retained directory and only
    performs the second rename.
    """

    if gates.get("passed") is not True:
        raise GitlleryPromotionError("verification gates have not passed")
    if float(gates.get("incremental_soak_hours") or 0) < 24:
        raise GitlleryPromotionError("24-hour incremental soak has not passed")

    generation = _safe_generation(generation)
    root = Path(creator_root).resolve()
    staged = (root / f".gitllery.build-{generation}").resolve()
    active = (root / ".gitllery").resolve()
    legacy = (root / ".gitllery.legacy-v0").resolve()
    for path in (staged, active, legacy):
        path.relative_to(root)

    if not staged.exists():
        # A completed retry is idempotent when the active directory is already
        # the verified segment format.
        active_repo = SegmentRepository(active)
        if active_repo.exists() and active_repo.verify(deep=True).ok:
            manifest = active_repo.read_manifest()
            if str(manifest.get("generation") or "") == generation:
                return {"promoted": False, "already_active": True}
        raise GitlleryPromotionError("verified build directory is missing")
    verification = SegmentRepository(staged).verify(deep=True)
    if not verification.ok:
        raise GitlleryPromotionError("staged generation failed deep verification")
    if active.exists() and legacy.exists():
        raise GitlleryPromotionError("legacy preservation target already exists")

    result = {
        "promoted": not dry_run,
        "generation": generation,
        "legacy_preserved": active.exists() or legacy.exists(),
        "active": str(active),
        "legacy": str(legacy),
    }
    if dry_run:
        return {**result, "promoted": False, "dry_run": True}

    root.mkdir(parents=True, exist_ok=True)
    if active.exists():
        os.replace(active, legacy)
        _fsync_directory(root)
    os.replace(staged, active)
    _fsync_directory(root)
    return result


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _invalidate_gitllery_status() -> None:
    from app.services.gitllery.service import _invalidate_status_cache

    _invalidate_status_cache()


def _current_rss_mb() -> float:
    """Return current resident memory without adding a runtime dependency."""

    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0.0


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _sample_worker_memory(stats: dict[str, Any]) -> None:
    baseline = float(stats.get("rss_attempt_start_mb") or 0)
    stats["worker_memory_mb"] = max(
        float(stats.get("worker_memory_mb") or 0),
        max(0.0, _current_rss_mb() - baseline),
    )


class GitlleryBuildService:
    """Create, checkpoint, resume, and verify side-by-side generations."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        *,
        scope: str = "canary",
        generation: str | None = None,
    ) -> GitlleryBuild:
        if scope not in {"canary", "full"}:
            raise ValueError("Gitllery build scope must be canary or full")
        if scope == "full":
            await self._require_passing_canary()
        active = await self.db.scalar(
            select(GitlleryBuild.id)
            .where(
                GitlleryBuild.kind == "build",
                GitlleryBuild.state.in_(["pending", "running"]),
            )
            .limit(1)
        )
        if active is not None:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=409,
                detail={
                    "code": "gitllery_build_running",
                    "message": "Another Gitllery build is already running.",
                    "build_id": str(active),
                },
            )
        requested = generation or f"{settings.gitllery_build_generation}-{uuid4().hex[:8]}"
        safe_generation = _safe_generation(requested)
        existing = await self.db.scalar(
            select(GitlleryBuild.id)
            .where(
                GitlleryBuild.kind == "build",
                GitlleryBuild.generation == safe_generation,
            )
            .limit(1)
        )
        if existing is not None:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=409,
                detail={
                    "code": "gitllery_generation_exists",
                    "message": "Gitllery build generation already exists.",
                    "build_id": str(existing),
                },
            )
        high_water = (
            await self.db.execute(select(CurationCommit).order_by(CurationCommit.created_at.desc(), CurationCommit.id.desc()).limit(1))
        ).scalar_one_or_none()
        row = GitlleryBuild(
            kind="build",
            state="pending",
            generation=safe_generation,
            high_water_commit_id=high_water.id if high_water else None,
            stats={
                "scope": scope,
                "phase": "planning",
                "repository_counts": {},
                "selected_repositories": [],
                "built_repositories": {},
                "resume_count": 0,
                "worker_memory_mb": 0.0,
                "active_seconds": 0.0,
            },
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        _invalidate_gitllery_status()
        return row

    async def get(self, build_id: UUID | str) -> GitlleryBuild:
        row = await self.db.get(GitlleryBuild, UUID(str(build_id)))
        if row is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Gitllery build not found")
        return row

    async def create_verification(
        self,
        *,
        repository_id: str | None,
        build_id: UUID | None,
        deep: bool,
        evidence: Mapping[str, Any] | None = None,
    ) -> GitlleryBuild:
        target = await self.get(build_id) if build_id else None
        if target is not None and target.kind != "build":
            raise ValueError("Gitllery verification target must be a build")
        if target is not None and target.state not in {"staged", "complete"}:
            raise RuntimeError("Gitllery build is not staged for verification")
        high_water = None
        if target is not None:
            high_water = target.high_water_commit_id
        else:
            high_water = await self.db.scalar(select(CurationCommit.id).order_by(CurationCommit.created_at.desc(), CurationCommit.id.desc()).limit(1))
        verification = GitlleryBuild(
            kind="verify",
            state="pending",
            generation=(target.generation if target else settings.gitllery_build_generation),
            repository_key=repository_id,
            high_water_commit_id=high_water,
            stats={
                "phase": "pending",
                "scope": ("build" if target else "repository" if repository_id else "library"),
                "target_build_id": str(target.id) if target else None,
                "deep": deep,
                "evidence": dict(evidence or {}),
            },
        )
        self.db.add(verification)
        await self.db.commit()
        await self.db.refresh(verification)
        _invalidate_gitllery_status()
        return verification

    async def run_verification(self, verification_id: UUID | str) -> dict[str, Any]:
        verification = await self.get(verification_id)
        if verification.kind != "verify":
            raise ValueError("Gitllery operation is not a verification")
        if verification.state == "complete":
            return dict((verification.stats or {}).get("result") or {})
        stats = dict(verification.stats or {})
        verification.state = "running"
        stats["phase"] = "running"
        verification.stats = stats
        verification.last_error = None
        await self.db.commit()
        _invalidate_gitllery_status()
        try:
            target_build_id = stats.get("target_build_id")
            if target_build_id:
                result = await self.verify(
                    target_build_id,
                    evidence=stats.get("evidence") or {},
                )
            elif verification.repository_key:
                from app.services.gitllery.service import GitlleryService

                result = await GitlleryService(self.db).verify_segment_repository(
                    verification.repository_key,
                    deep=bool(stats.get("deep")),
                )
            else:
                result = await self.verify_library(deep=bool(stats.get("deep")))
            verification = await self.get(verification_id)
            updated = dict(verification.stats or {})
            updated.update({"phase": "complete", "result": result})
            verification.stats = updated
            verification.state = "complete" if result.get("ok") else "failed"
            verification.summary_hash = digest_bytes(canonical_bytes(result)) if result else None
            verification.last_error = None if result.get("ok") else "; ".join(result.get("errors") or ["verification failed"])[:4000]
            await self.db.commit()
            _invalidate_gitllery_status()
            return result
        except Exception as exc:
            await self.db.rollback()
            verification = await self.get(verification_id)
            failed = dict(verification.stats or {})
            failed["phase"] = "failed"
            verification.stats = failed
            verification.state = "failed"
            verification.last_error = str(exc)[:4000]
            await self.db.commit()
            _invalidate_gitllery_status()
            raise

    async def _require_passing_canary(self) -> None:
        rows = (
            await self.db.execute(select(GitlleryBuild).where(GitlleryBuild.kind == "build").order_by(GitlleryBuild.created_at.desc(), GitlleryBuild.id.desc()))
        ).scalars()
        for row in rows:
            stats = row.stats or {}
            if stats.get("scope") != "canary":
                continue
            if row.state == "complete" and (stats.get("gates") or {}).get("passed"):
                return
            break
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409,
            detail={
                "code": "gitllery_canary_required",
                "message": "A verified canary with passing gates is required first.",
            },
        )

    async def run(
        self,
        build_id: UUID | str,
        *,
        batch_size: int = 100,
        stop_after_batches: int | None = None,
    ) -> GitlleryBuild:
        """Run bounded checkpoints until staged (or an explicit test stop)."""

        build_id = UUID(str(build_id))
        batch_size = max(1, min(int(batch_size), 100))
        row = await self.get(build_id)
        if row.state in {"staged", "complete"}:
            return row
        if row.state not in {"pending", "running"}:
            raise RuntimeError(f"Gitllery build cannot resume from {row.state}")
        stats = dict(row.stats or {})
        if row.state == "running":
            stats["resume_count"] = int(stats.get("resume_count") or 0) + 1
        stats["rss_attempt_start_mb"] = _current_rss_mb()
        row.state = "running"
        row.stats = stats
        row.last_error = None
        await self.db.commit()
        _invalidate_gitllery_status()

        started = monotonic()
        batches = 0
        try:
            while True:
                row = await self.get(build_id)
                phase = (row.stats or {}).get("phase") or "planning"
                if phase == "planning":
                    had_rows = await self._plan_batch(row, batch_size)
                elif phase == "building":
                    had_rows = await self._build_batch(row, batch_size)
                elif phase == "awaiting_verification":
                    return row
                else:
                    raise RuntimeError(f"Unknown Gitllery build phase: {phase}")
                batches += 1
                if not had_rows and phase == "building":
                    return await self.get(build_id)
                if stop_after_batches is not None and batches >= stop_after_batches:
                    return await self.get(build_id)
        except Exception as exc:
            await self.db.rollback()
            row = await self.get(build_id)
            stats = dict(row.stats or {})
            stats["phase"] = "failed"
            stats["failure_count"] = int(stats.get("failure_count") or 0) + 1
            row.stats = stats
            row.state = "failed"
            row.last_error = str(exc)[:4000]
            # The last committed cursor and the staged generation remain
            # intact for diagnosis or an explicitly-created recovery build.
            await self.db.commit()
            _invalidate_gitllery_status()
            raise
        finally:
            await self._record_runtime(build_id, monotonic() - started)

    async def _record_runtime(self, build_id: UUID, elapsed: float) -> None:
        try:
            row = await self.get(build_id)
            stats = dict(row.stats or {})
            stats["active_seconds"] = float(stats.get("active_seconds") or 0) + elapsed
            _sample_worker_memory(stats)
            row.stats = stats
            await self.db.commit()
        except Exception:
            await self.db.rollback()

    async def _high_water_position(self, row: GitlleryBuild) -> tuple[datetime, UUID] | None:
        if row.high_water_commit_id is None:
            return None
        position = (
            await self.db.execute(select(CurationCommit.created_at, CurationCommit.id).where(CurationCommit.id == row.high_water_commit_id))
        ).one_or_none()
        return (position[0], position[1]) if position else None

    async def _commit_batch(self, row: GitlleryBuild, limit: int) -> list[CurationCommit]:
        high_water = await self._high_water_position(row)
        if high_water is None:
            return []
        stmt = select(CurationCommit).where(tuple_(CurationCommit.created_at, CurationCommit.id) <= tuple_(*high_water))
        if row.cursor_created_at is not None and row.cursor_commit_id is not None:
            stmt = stmt.where(tuple_(CurationCommit.created_at, CurationCommit.id) > tuple_(row.cursor_created_at, row.cursor_commit_id))
        return list((await self.db.execute(stmt.order_by(CurationCommit.created_at, CurationCommit.id).limit(limit))).scalars())

    async def _slices_for_commits(self, commits: list[CurationCommit]) -> dict[UUID, dict[str, tuple[RepoDescriptor, list[CurationChange]]]]:
        if not commits:
            return {}
        changes = list(
            (
                await self.db.execute(
                    select(CurationChange)
                    .where(CurationChange.commit_id.in_([commit.id for commit in commits]))
                    .order_by(
                        CurationChange.commit_id,
                        CurationChange.sequence.asc().nulls_last(),
                        CurationChange.created_at,
                        CurationChange.id,
                    )
                )
            ).scalars()
        )
        grouped: dict[UUID, list[CurationChange]] = defaultdict(list)
        for change in changes:
            grouped[change.commit_id].append(change)
        resolver = RepoResolver(self.db)
        await resolver.preload_work_sources(sorted({change.subject_id for change in changes if change.subject_type == "work"}))
        result = {}
        for commit in commits:
            result[commit.id] = await resolver.slice_changes(grouped.get(commit.id, []))
        return result

    async def _initial_repository_counts(self) -> dict[str, dict[str, Any]]:
        return {
            descriptor.key(): {
                "commits": 0,
                "changes": 0,
                "source": descriptor.source,
                "creator_dir": descriptor.creator_dir,
            }
            for descriptor in await RepoResolver(self.db).all_repositories()
        }

    async def _plan_batch(self, row: GitlleryBuild, limit: int) -> bool:
        commits = await self._commit_batch(row, limit)
        stats = dict(row.stats or {})
        counts = dict(stats.get("repository_counts") or {})
        if not counts:
            counts = await self._initial_repository_counts()
        if not commits:
            simple_counts = {key: int(value.get("commits") or 0) for key, value in counts.items()}
            selected = select_canary_repository_keys(simple_counts) if stats.get("scope") == "canary" else sorted(simple_counts)
            stats.update(
                {
                    "phase": "building",
                    "repository_counts": counts,
                    "selected_repositories": selected,
                }
            )
            _sample_worker_memory(stats)
            row.stats = stats
            row.cursor_created_at = None
            row.cursor_commit_id = None
            await self.db.commit()
            _invalidate_gitllery_status()
            return False

        slices = await self._slices_for_commits(commits)
        for commit in commits:
            for key, (descriptor, changes) in slices.get(commit.id, {}).items():
                entry = dict(
                    counts.get(key)
                    or {
                        "commits": 0,
                        "changes": 0,
                        "source": descriptor.source,
                        "creator_dir": descriptor.creator_dir,
                    }
                )
                entry["commits"] = int(entry.get("commits") or 0) + 1
                entry["changes"] = int(entry.get("changes") or 0) + len(changes)
                counts[key] = entry
        stats["repository_counts"] = counts
        _sample_worker_memory(stats)
        row.stats = stats
        row.cursor_created_at = commits[-1].created_at
        row.cursor_commit_id = commits[-1].id
        await self.db.commit()
        return True

    @staticmethod
    def _descriptor_from_stats(key: str, entry: Mapping[str, Any]) -> RepoDescriptor:
        return RepoDescriptor(
            repository_id=key,
            source=str(entry["source"]),
            source_creator_id=None,
            creator_id=None,
            creator_dir=str(entry["creator_dir"]),
        )

    @staticmethod
    def _repository_for(descriptor: RepoDescriptor, generation: str) -> SegmentRepository:
        root = Path(settings.library_root).resolve()
        creator_root = (root / descriptor.source / descriptor.creator_dir).resolve()
        creator_root.relative_to(root)
        repository = SegmentRepository(creator_root / f".gitllery.build-{_safe_generation(generation)}")
        repository.root.relative_to(root)
        return repository

    @staticmethod
    def _repository_for_state(
        descriptor: RepoDescriptor,
        state: GitlleryRepositoryState,
    ) -> SegmentRepository:
        root = Path(settings.library_root).resolve()
        creator_root = (root / descriptor.source / descriptor.creator_dir).resolve()
        creator_root.relative_to(root)
        dirname = ".gitllery" if state.mode == "active" else f".gitllery.build-{_safe_generation(state.generation)}"
        repository = SegmentRepository(creator_root / dirname)
        repository.root.relative_to(root)
        return repository

    async def _build_batch(self, row: GitlleryBuild, limit: int) -> bool:
        commits = await self._commit_batch(row, limit)
        stats = dict(row.stats or {})
        selected = set(stats.get("selected_repositories") or [])
        counts = dict(stats.get("repository_counts") or {})
        if not commits:
            summaries: dict[str, dict[str, Any]] = {}
            storage_bytes = 0
            for key in sorted(selected):
                descriptor = self._descriptor_from_stats(key, counts[key])
                repository = self._repository_for(descriptor, row.generation)
                with repository.projection_lock():
                    repository.initialise(
                        repository_id=key,
                        source=descriptor.source,
                        creator_dir=descriptor.creator_dir,
                        generation=row.generation,
                    )
                    manifest = repository.read_manifest()
                summaries[key] = {
                    "head_segment": manifest.get("head_segment"),
                    "commit_count": int(manifest.get("commit_count") or 0),
                    "change_count": int(manifest.get("change_count") or 0),
                }
                storage_bytes += _directory_size(repository.root)
            stats["phase"] = "awaiting_verification"
            stats["built_repositories"] = summaries
            stats["storage_bytes"] = storage_bytes
            _sample_worker_memory(stats)
            row.stats = stats
            row.summary_hash = digest_bytes(canonical_bytes(summaries))
            row.state = "staged"
            row.cursor_created_at = None
            row.cursor_commit_id = None
            await self.db.commit()
            _invalidate_gitllery_status()
            return False

        slices = await self._slices_for_commits(commits)
        by_repository: dict[str, tuple[RepoDescriptor, list[tuple[CurationCommit, list[CurationChange]]]]] = {}
        for commit in commits:
            for key, (descriptor, changes) in slices.get(commit.id, {}).items():
                if key not in selected:
                    continue
                by_repository.setdefault(key, (descriptor, []))[1].append((commit, changes))

        from app.services.gitllery.service import GitlleryService

        for key in sorted(by_repository):
            descriptor, items = by_repository[key]
            repository = self._repository_for(descriptor, row.generation)
            with repository.projection_lock():
                repository.initialise(
                    repository_id=key,
                    source=descriptor.source,
                    creator_dir=descriptor.creator_dir,
                    generation=row.generation,
                )
                manifest = repository.read_manifest()
                if str(manifest.get("last_complete_commit_id")) == str(items[-1][0].id):
                    continue
                parent = manifest.get("last_complete_commit_id")
                payloads = []
                for commit, changes in items:
                    payload = GitlleryService._segment_commit_payload(
                        commit,
                        changes,
                        repository_parent_commit_id=parent,
                    )
                    payloads.append(payload)
                    parent = str(commit.id)
                repository.append(payloads)

        row.cursor_created_at = commits[-1].created_at
        row.cursor_commit_id = commits[-1].id
        _sample_worker_memory(stats)
        row.stats = stats
        await self.db.commit()
        return True

    async def verify(
        self,
        build_id: UUID | str,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await self.get(build_id)
        if row.state not in {"staged", "complete"}:
            raise RuntimeError("Gitllery build is not staged for verification")
        stats = dict(row.stats or {})
        counts = dict(stats.get("repository_counts") or {})
        selected = list(stats.get("selected_repositories") or [])
        verified: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        duplicate_free = True
        verified_summaries: dict[str, dict[str, Any]] = {}
        now = _utcnow()
        for key in selected:
            descriptor = self._descriptor_from_stats(key, counts[key])
            repository = self._repository_for(descriptor, row.generation)
            result = repository.verify(deep=True)
            try:
                manifest = repository.read_manifest()
            except Exception as exc:
                message = f"{key}: cannot read verified manifest: {exc}"
                errors.append(message)
                duplicate_free = False
                verified[key] = {
                    **result.as_dict(),
                    "ok": False,
                    "exact": False,
                    "errors": [*result.errors, message],
                }
                await self._record_failed_repository_state(
                    key,
                    descriptor,
                    row.generation,
                    message,
                    verified_at=now,
                )
                continue
            commit_ids = [str(commit.get("commit_id")) for commit in repository.iter_commits()] if result.ok else []
            duplicate_free = duplicate_free and len(commit_ids) == len(set(commit_ids))
            expected = counts[key]
            exact = result.ok and result.commits == int(expected.get("commits") or 0) and result.changes == int(expected.get("changes") or 0)
            if not exact:
                errors.append(f"{key}: Gitllery summary does not match PostgreSQL")
            verified[key] = {**result.as_dict(), "exact": exact}
            verified_summaries[key] = {
                "head_segment": manifest.get("head_segment"),
                "commit_count": int(manifest.get("commit_count") or 0),
                "change_count": int(manifest.get("change_count") or 0),
            }
            last_id = manifest.get("last_complete_commit_id")
            last_created_at = None
            if last_id:
                try:
                    last_created_at = await self.db.scalar(select(CurationCommit.created_at).where(CurationCommit.id == UUID(str(last_id))))
                except ValueError:
                    errors.append(f"{key}: invalid commit watermark")
            await self.db.execute(
                pg_insert(GitlleryRepositoryState)
                .values(
                    id=uuid4(),
                    repository_key=key,
                    source=descriptor.source,
                    creator_dir=descriptor.creator_dir,
                    product_version="v1",
                    format_id="gitllery-segment",
                    format_revision=1,
                    mode="shadow",
                    generation=row.generation,
                    head_segment=manifest.get("head_segment"),
                    last_complete_commit_id=UUID(str(last_id)) if last_id else None,
                    last_complete_created_at=last_created_at,
                    segment_count=int(manifest.get("segment_count") or 0),
                    commit_count=int(manifest.get("commit_count") or 0),
                    change_count=int(manifest.get("change_count") or 0),
                    last_verified_at=now if exact else None,
                    last_error=None if exact else errors[-1],
                )
                .on_conflict_do_update(
                    index_elements=[GitlleryRepositoryState.repository_key],
                    set_={
                        "mode": "shadow",
                        "generation": row.generation,
                        "head_segment": manifest.get("head_segment"),
                        "last_complete_commit_id": UUID(str(last_id)) if last_id else None,
                        "last_complete_created_at": last_created_at,
                        "segment_count": int(manifest.get("segment_count") or 0),
                        "commit_count": int(manifest.get("commit_count") or 0),
                        "change_count": int(manifest.get("change_count") or 0),
                        "last_verified_at": now if exact else None,
                        "last_error": None if exact else errors[-1],
                        "updated_at": now,
                    },
                )
            )

        actual_summary_hash = digest_bytes(canonical_bytes(verified_summaries))
        summary_hash_exact = actual_summary_hash == row.summary_hash
        if not summary_hash_exact:
            errors.append("Gitllery build summary changed after staging")

        total_commits = sum(int(value.get("commits") or 0) for value in counts.values())
        selected_commits = sum(int(counts[key].get("commits") or 0) for key in selected)
        multiplier = max(1.0, total_commits / max(1, selected_commits))
        active_hours = float(stats.get("active_seconds") or 0) / 3600
        auto_evidence = {
            "integrity_exact": not errors and summary_hash_exact,
            "duplicate_free": duplicate_free,
            "resume_verified": int(stats.get("resume_count") or 0) > 0,
            "worker_memory_mb": float(stats.get("worker_memory_mb") or 0),
            "projected_duration_hours": active_hours * multiplier,
            "projected_storage_bytes": int(int(stats.get("storage_bytes") or 0) * multiplier),
        }
        combined = {**auto_evidence, **dict(evidence or {})}
        gates = evaluate_activation_gates(
            combined,
            require_incremental_soak=False,
        )
        stats.update(
            {
                "phase": "verified" if not errors else "verification_failed",
                "verified_repositories": verified,
                "verified_summary_hash": actual_summary_hash,
                "evidence": combined,
                "gates": gates,
            }
        )
        row.stats = stats
        row.state = "complete" if not errors else "failed"
        row.last_error = "; ".join(errors)[:4000] if errors else None
        if not errors and stats.get("scope") == "full" and gates["passed"]:
            await self._settle_full_build(row)
            stats["settled_through_high_water"] = True
            row.stats = stats
        await self.db.commit()
        _invalidate_gitllery_status()
        return {
            "build_id": str(row.id),
            "ok": not errors,
            "errors": errors,
            "gates": gates,
            "repositories": verified,
        }

    async def _record_failed_repository_state(
        self,
        key: str,
        descriptor: RepoDescriptor,
        generation: str,
        message: str,
        *,
        verified_at: datetime,
    ) -> None:
        await self.db.execute(
            pg_insert(GitlleryRepositoryState)
            .values(
                id=uuid4(),
                repository_key=key,
                source=descriptor.source,
                creator_dir=descriptor.creator_dir,
                product_version="v1",
                format_id="gitllery-segment",
                format_revision=1,
                mode="shadow",
                generation=generation,
                segment_count=0,
                commit_count=0,
                change_count=0,
                last_verified_at=None,
                last_error=message[:4000],
            )
            .on_conflict_do_update(
                index_elements=[GitlleryRepositoryState.repository_key],
                set_={
                    "last_verified_at": None,
                    "last_error": message[:4000],
                    "updated_at": verified_at,
                },
            )
        )

    async def verify_library(self, *, deep: bool) -> dict[str, Any]:
        """Background-only verification of every DB-recorded segment repo."""

        descriptors = {descriptor.key(): descriptor for descriptor in await RepoResolver(self.db).all_repositories()}
        states = list(
            (
                await self.db.execute(
                    select(GitlleryRepositoryState).where(
                        GitlleryRepositoryState.format_id == "gitllery-segment",
                        GitlleryRepositoryState.format_revision == 1,
                    )
                )
            ).scalars()
        )
        errors: list[str] = []
        repositories: dict[str, dict[str, Any]] = {}
        verified_at = _utcnow()
        for state in states:
            descriptor = descriptors.get(state.repository_key)
            if descriptor is None:
                message = "repository is no longer present in the database catalog"
                errors.append(f"{state.repository_key}: {message}")
                state.last_error = message
                continue
            repository = self._repository_for_state(descriptor, state)
            if not repository.exists():
                message = "segment repository is missing from disk"
                errors.append(f"{state.repository_key}: {message}")
                state.last_error = message
                repositories[state.repository_key] = {
                    "ok": False,
                    "errors": [message],
                }
                continue
            result = repository.verify(deep=deep)
            repositories[state.repository_key] = result.as_dict()
            if result.ok:
                state.last_verified_at = verified_at
                state.last_error = None
            else:
                message = "; ".join(result.errors)
                state.last_error = message[:4000]
                errors.append(f"{state.repository_key}: {message}")
        await self.db.commit()
        _invalidate_gitllery_status()
        return {
            "ok": not errors,
            "deep": deep,
            "repository_count": len(states),
            "errors": errors,
            "repositories": repositories,
        }

    async def _settle_full_build(self, row: GitlleryBuild) -> None:
        high_water = await self._high_water_position(row)
        if high_water is None:
            return
        await self.db.execute(
            update(GitlleryProjectionOutbox)
            .where(
                GitlleryProjectionOutbox.commit_id.in_(
                    select(CurationCommit.id).where(tuple_(CurationCommit.created_at, CurationCommit.id) <= tuple_(*high_water))
                )
            )
            .values(
                state="complete",
                completed_at=_utcnow(),
                lease_expires_at=None,
                last_error=None,
                projection_stats={
                    "format": "gitllery-segment",
                    "revision": 1,
                    "generation": row.generation,
                    "settled_by_build": str(row.id),
                },
            )
        )
