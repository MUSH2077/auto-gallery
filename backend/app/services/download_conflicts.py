"""Evidence-gated resolution and rollback for download staging conflicts."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Asset,
    AssetSource,
    DownloadJob,
    MaintenanceAuditEvent,
    SubscriptionSource,
    TaskRun,
    WorkSource,
)
from app.providers import registry
from app.services.download_staging import (
    DownloadStage,
    DownloadStageError,
    _fsync_directory,
)
from app.services.job_manifest import append_manifest_event
from app.services.media_derivatives import request_media_derivatives
from app.services.search_projection_outbox import request_search_projection
from app.services.tasks import TaskService


ACTIVE_RESOLUTION_CONFLICTS = {
    "enqueued", "pending", "running", "downloading", "downloaded",
    "importing", "paused", "recovering",
}


class DownloadConflictError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _managed_relative_parts(relative: str) -> tuple[str, ...]:
    """Return a portable, relative component list for an openat traversal."""

    if not relative or "\\" in relative or "\x00" in relative:
        raise DownloadConflictError("Conflict path is unsafe", status_code=422)
    parsed = PurePosixPath(relative)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise DownloadConflictError("Conflict path is unsafe", status_code=422)
    return tuple(parsed.parts)


def _open_managed_regular(root: Path, relative: str) -> BinaryIO:
    """Open a managed file without following a replaceable path component."""

    parts = _managed_relative_parts(relative)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    file_fd = -1
    try:
        resolved_root = Path(root).resolve(strict=True)
        current_fd = os.open(
            resolved_root,
            os.O_RDONLY | directory_flag | nofollow_flag,
        )
        descriptors.append(current_fd)
        for component in parts[:-1]:
            current_fd = os.open(
                component,
                os.O_RDONLY | directory_flag | nofollow_flag,
                dir_fd=current_fd,
            )
            descriptors.append(current_fd)
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | nofollow_flag,
            dir_fd=current_fd,
        )
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise DownloadConflictError("Conflict file is unsafe", status_code=404)
        handle = os.fdopen(file_fd, "rb")
        file_fd = -1
        return handle
    except DownloadConflictError:
        raise
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise DownloadConflictError(
            "Conflict file is missing or unsafe",
            status_code=404,
        ) from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _equal_nonempty(*values: Any) -> bool:
    normalized = [str(value) for value in values if value not in {None, ""}]
    return len(normalized) == len(values) and len(set(normalized)) == 1


def upstream_conflict_evidence(
    *,
    job_source: str,
    repository_identity: dict | None,
    database_identity: dict | None,
    canonical_metadata_identity: dict | None,
    staged_metadata_identity: dict | None,
    filename_ordinal: int | None,
) -> dict[str, Any]:
    """Require independent agreement on every identity dimension."""

    repository_identity = repository_identity or {}
    database_identity = database_identity or {}
    canonical_metadata_identity = canonical_metadata_identity or {}
    staged_metadata_identity = staged_metadata_identity or {}
    source = _equal_nonempty(
        job_source,
        repository_identity.get("source"),
        database_identity.get("source"),
        canonical_metadata_identity.get("source"),
        staged_metadata_identity.get("source"),
    )
    repository = source and _equal_nonempty(
        repository_identity.get("source_creator_id"),
        database_identity.get("source_creator_id"),
    )
    work = _equal_nonempty(
        database_identity.get("source_work_id"),
        canonical_metadata_identity.get("source_work_id"),
        staged_metadata_identity.get("source_work_id"),
    )
    creator = _equal_nonempty(
        database_identity.get("source_creator_id"),
        canonical_metadata_identity.get("source_creator_id"),
        staged_metadata_identity.get("source_creator_id"),
    )
    page = _equal_nonempty(
        database_identity.get("ordinal"),
        canonical_metadata_identity.get("ordinal"),
        staged_metadata_identity.get("ordinal"),
        filename_ordinal,
    )
    source_asset = _equal_nonempty(
        database_identity.get("source_asset_id"),
        canonical_metadata_identity.get("source_asset_id"),
        staged_metadata_identity.get("source_asset_id"),
    )
    checks = {
        "source": source,
        "repository": repository,
        "work": work,
        "creator": creator,
        "page": page,
        "source_asset": source_asset,
    }
    eligible = all(checks.values())
    return {
        "auto_eligible": eligible,
        "recommended_winner": "staged" if eligible else "canonical",
        "checks": checks,
        "database_identity": database_identity or None,
        "canonical_identity": canonical_metadata_identity or None,
        "staged_identity": staged_metadata_identity or None,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _managed_regular_hash(
    path: Path,
    *,
    label: str,
    allow_missing: bool = False,
) -> str | None:
    if path.is_symlink():
        raise DownloadConflictError(f"{label} cannot be a symlink")
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise DownloadConflictError(f"{label} is missing") from None
    if not stat.S_ISREG(info.st_mode):
        raise DownloadConflictError(f"{label} is not a regular file")
    return _sha256(path)


def _filename_ordinal(path: Path) -> int | None:
    stem = path.stem
    for pattern in (r"_p(\d+)(?:_|$)", r"_(\d+)$"):
        match = re.search(pattern, stem, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _metadata_identity(
    provider,
    directory: Path,
    media_path: Path,
    expected: dict | None,
) -> dict | None:
    expected = expected or {}
    candidates: list[tuple[dict, dict]] = []
    for metadata_path in sorted(directory.glob("*.json")):
        if metadata_path.is_symlink() or not metadata_path.is_file():
            continue
        try:
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            work = provider.parse_work_source(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if not work.get("source_work_id") or not work.get("source_creator_id"):
            continue
        candidates.append((raw, work))
    if not candidates:
        return None
    matching = [
        pair for pair in candidates
        if (
            not expected.get("source_work_id")
            or str(pair[1].get("source_work_id")) == str(expected["source_work_id"])
        )
    ]
    if len(matching) != 1:
        return None
    raw, work = matching[0]
    ordinal = _filename_ordinal(media_path)
    if ordinal is None:
        raw_ordinal = raw.get("num") if isinstance(raw, dict) else None
        if raw_ordinal is not None:
            try:
                ordinal = int(raw_ordinal)
            except (TypeError, ValueError):
                ordinal = None
        elif expected.get("ordinal") == 0:
            # Providers with exactly one page omit a suffix. The database
            # ordinal plus a unique work metadata file is independent proof.
            ordinal = 0
    return {
        "source": provider.source_name,
        "source_work_id": str(work.get("source_work_id")),
        "source_creator_id": str(work.get("source_creator_id")),
        "source_asset_id": media_path.stem,
        "ordinal": ordinal,
        "metadata_file": next(
            (
                item.name for item in directory.glob("*.json")
                if item.is_file() and not item.is_symlink()
            ),
            None,
        ),
    }


class DownloadConflictService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.download_root = Path(settings.download_root).resolve()

    async def _load(
        self,
        task_id: UUID,
        *,
        lock: bool = False,
    ) -> tuple[TaskRun, DownloadJob, DownloadStage]:
        statement = select(TaskRun).where(TaskRun.id == task_id)
        if lock:
            statement = statement.with_for_update()
        task = (await self.db.execute(statement)).scalar_one_or_none()
        if task is None:
            raise DownloadConflictError("Task not found", status_code=404)
        if task.subject_type != "download_job" or not task.subject_id:
            raise DownloadConflictError("Task is not a download job", status_code=422)
        job_statement = select(DownloadJob).where(DownloadJob.id == task.subject_id)
        if lock:
            job_statement = job_statement.with_for_update()
        job = (await self.db.execute(job_statement)).scalar_one_or_none()
        if job is None:
            raise DownloadConflictError("Download job not found", status_code=404)
        try:
            stage = DownloadStage.open(self.download_root, str(job.id), job.source)
        except DownloadStageError as exc:
            raise DownloadConflictError(str(exc)) from exc
        return task, job, stage

    async def _database_identity(self, relative_path: str, source: str) -> dict | None:
        rows = list((await self.db.execute(
            select(Asset, AssetSource, WorkSource)
            .join(AssetSource, AssetSource.asset_id == Asset.id)
            .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
            .where(Asset.file_path == relative_path, WorkSource.source == source)
        )).all())
        if len(rows) != 1:
            return None
        asset, asset_source, work_source = rows[0]
        return {
            "asset_id": str(asset.id),
            "work_id": str(work_source.work_id),
            "source": work_source.source,
            "source_work_id": work_source.source_work_id,
            "source_creator_id": work_source.source_creator_id,
            "source_asset_id": asset_source.source_asset_id,
            "ordinal": asset_source.ordinal,
        }

    async def _repository_identity(self, job: DownloadJob) -> dict | None:
        if not job.subscription_source_id:
            return None
        source = await self.db.get(SubscriptionSource, job.subscription_source_id)
        if source is None:
            return None
        return {
            "id": str(source.id),
            "source": source.source,
            "source_creator_id": source.source_creator_id,
            "source_url": source.source_url,
        }

    async def inspect(self, task_id: UUID) -> dict[str, Any]:
        task, job, stage = await self._load(task_id)
        manifest = dict(stage._manifest)
        conflicts = list(manifest.get("conflicts") or [])
        details_by_path = {
            str(item.get("relative_path")): dict(item)
            for item in manifest.get("conflict_details") or []
            if isinstance(item, dict) and item.get("relative_path")
        }
        repository = await self._repository_identity(job)
        try:
            provider = registry.get(job.source)
        except KeyError:
            provider = None
        items = []
        for relative in conflicts:
            staged = stage.root / relative
            canonical = self.download_root / relative
            database = await self._database_identity(relative, job.source)
            ordinal = _filename_ordinal(Path(relative))
            if ordinal is None and database and database.get("ordinal") == 0:
                ordinal = 0
            canonical_identity = (
                _metadata_identity(provider, canonical.parent, canonical, database)
                if provider is not None and canonical.exists()
                else None
            )
            staged_identity = (
                _metadata_identity(provider, staged.parent, staged, database)
                if provider is not None and staged.exists()
                else None
            )
            evidence = upstream_conflict_evidence(
                job_source=job.source,
                repository_identity=repository,
                database_identity=database,
                canonical_metadata_identity=canonical_identity,
                staged_metadata_identity=staged_identity,
                filename_ordinal=ordinal,
            )
            detail = details_by_path.get(relative, {})
            items.append({
                **detail,
                "relative_path": relative,
                "file_type": detail.get("file_type") or (
                    "metadata" if Path(relative).suffix.lower() == ".json" else "media"
                ),
                "mime_type": mimetypes.guess_type(relative)[0] or "application/octet-stream",
                "canonical_size": canonical.stat().st_size if canonical.is_file() else None,
                "staged_size": staged.stat().st_size if staged.is_file() else None,
                "canonical_sha256": detail.get("canonical_sha256") or (
                    _sha256(canonical) if canonical.is_file() else None
                ),
                "staged_sha256": detail.get("staged_sha256") or (
                    _sha256(staged) if staged.is_file() else None
                ),
                "evidence": evidence,
            })
        return {
            "task_id": str(task.id),
            "download_job_id": str(job.id),
            "source": job.source,
            "source_url": job.source_url,
            "status": task.status,
            "reason_code": task.reason_code,
            "resolution": manifest.get("resolution"),
            "all_auto_eligible": bool(items) and all(
                item["evidence"]["auto_eligible"] for item in items
            ),
            "items": items,
        }

    async def open_media(
        self,
        task_id: UUID,
        relative_path: str,
        side: str,
    ) -> tuple[BinaryIO, str]:
        _task, _job, stage = await self._load(task_id)
        stored_relative = next(
            (
                str(candidate)
                for candidate in stage._manifest.get("conflicts") or []
                if str(candidate) == relative_path
            ),
            None,
        )
        if stored_relative is None:
            raise DownloadConflictError("Conflict file not found", status_code=404)
        if side == "staged":
            root = stage.root.resolve()
        elif side == "canonical":
            root = self.download_root
        else:
            raise DownloadConflictError("side must be canonical or staged", status_code=422)
        return _open_managed_regular(root, stored_relative), stored_relative

    async def resolve(
        self,
        task_id: UUID,
        decisions: dict[str, str],
        *,
        operator: str,
        automatic: bool = False,
        resolution_id: str | None = None,
    ) -> dict[str, Any]:
        task, job, stage = await self._load(task_id, lock=True)
        case = await self.inspect(task_id)
        if automatic:
            if not case["all_auto_eligible"]:
                raise DownloadConflictError("Conflict does not have complete upstream identity evidence")
            expected = {
                item["relative_path"]: item["evidence"]["recommended_winner"]
                for item in case["items"]
            }
            if decisions != expected:
                raise DownloadConflictError("Automatic resolution must follow the evidence-backed decision")
        resolution_id = resolution_id or str(uuid4())
        try:
            resolution = stage.resolve_conflicts(
                decisions,
                resolution_id=resolution_id,
                retention_days=30,
            )
        except DownloadStageError as exc:
            raise DownloadConflictError(str(exc)) from exc

        audit_key = f"download-conflict-resolution:{resolution.resolution_id}"
        existing = (await self.db.execute(
            select(MaintenanceAuditEvent).where(
                MaintenanceAuditEvent.idempotency_key == audit_key
            )
        )).scalar_one_or_none()
        if existing is not None:
            existing_summary = dict(existing.summary or {})
            if str(existing_summary.get("task_id")) != str(task_id):
                raise DownloadConflictError("Conflict resolution audit identity mismatch")
            if existing_summary.get("state") != "applied":
                raise DownloadConflictError(
                    "Conflict resolution has already been rolled back"
                )
            return {**existing_summary, "idempotent_replay": True}
        entries: list[dict[str, Any]] = []
        affected_work_ids: set[UUID] = set()
        derivative_requests: list[dict[str, Any]] = []
        for raw in resolution.entries:
            entry = dict(raw)
            asset_rows = list((await self.db.execute(
                select(Asset, AssetSource, WorkSource)
                .join(AssetSource, AssetSource.asset_id == Asset.id)
                .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
                .where(Asset.file_path == entry["relative_path"])
                .with_for_update(of=Asset)
            )).all())
            entry["assets"] = []
            target = self.download_root / entry["relative_path"]
            stat = target.stat()
            for asset, _asset_source, work_source in asset_rows:
                before = {
                    "file_size": asset.file_size,
                    "sha256": asset.sha256,
                    "width": asset.width,
                    "height": asset.height,
                    "duration": asset.duration,
                    "thumb_sm_path": asset.thumb_sm_path,
                    "thumb_md_path": asset.thumb_md_path,
                    "thumb_lg_path": asset.thumb_lg_path,
                    "derivative_version": asset.derivative_version,
                    "derivative_source_size": asset.derivative_source_size,
                    "derivative_source_mtime_ns": asset.derivative_source_mtime_ns,
                    "phash": asset.phash,
                    "phash_version": asset.phash_version,
                }
                entry["assets"].append({"asset_id": str(asset.id), "before": before})
                affected_work_ids.add(work_source.work_id)
                if entry["winner"] == "staged":
                    asset.file_size = stat.st_size
                    asset.sha256 = entry["winner_sha256"]
                    asset.width = asset.height = None
                    asset.duration = None
                    asset.thumb_sm_path = asset.thumb_md_path = asset.thumb_lg_path = None
                    asset.derivative_version = None
                    asset.derivative_source_size = None
                    asset.derivative_source_mtime_ns = None
                    asset.phash = asset.phash_version = None
                    derivative_requests.append({
                        "asset_id": asset.id,
                        "requested": {"thumbnail": True, "dimensions": True, "video": True},
                        "source_size": stat.st_size,
                        "source_mtime_ns": stat.st_mtime_ns,
                    })
            entries.append(entry)
        await request_media_derivatives(self.db, derivative_requests)
        if affected_work_ids:
            await request_search_projection(self.db, affected_work_ids)
        summary = {
            "resolution_id": resolution.resolution_id,
            "task_id": str(task.id),
            "download_job_id": str(job.id),
            "source": job.source,
            "operator": operator,
            "automatic": automatic,
            "state": "applied",
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": resolution.expires_at,
            "entries": entries,
        }
        if existing is None:
            self.db.add(MaintenanceAuditEvent(
                event_type="download_conflict_resolution",
                idempotency_key=audit_key,
                summary=summary,
            ))
        append_manifest_event(
            job,
            "staging_conflict_resolved",
            resolution_id=resolution.resolution_id,
            automatic=automatic,
            decisions=decisions,
        )
        task_meta = dict(task.meta or {})
        task_meta["staging_conflict_resolution"] = {
            "resolution_id": resolution.resolution_id,
            "automatic": automatic,
            "expires_at": resolution.expires_at,
            "state": "applied",
        }
        await TaskService(self.db).update_task(
            task,
            meta=task_meta,
            attention_state="resolved",
            reason_code=None,
        )
        await TaskService(self.db).add_event(
            task,
            "conflict_resolved",
            to_status=task.status,
            message="Download staging conflict resolved",
            payload={
                "resolution_id": resolution.resolution_id,
                "automatic": automatic,
                "operator": operator,
            },
        )
        await self.db.flush()
        return summary

    def _rollback_files(
        self,
        entry: dict[str, Any],
        *,
        resolution_id: str,
    ) -> os.stat_result:
        """Swap winner and loser with a resumable hard-link recovery point."""

        relative = str(entry["relative_path"])
        target = (self.download_root / relative).resolve()
        quarantine = (self.download_root / str(entry["quarantine_path"])).resolve()
        rollback_token = hashlib.sha256(
            str(resolution_id).encode("utf-8", "surrogatepass")
        ).hexdigest()[:32]
        temporary = quarantine.with_name(f".{quarantine.name}.rollback-{rollback_token}")
        for path in (target, quarantine, temporary):
            try:
                path.relative_to(self.download_root)
            except ValueError as exc:
                raise DownloadConflictError(
                    "Conflict rollback path escapes managed storage"
                ) from exc

        winner_sha = str(entry["winner_sha256"])
        loser_sha = str(entry["loser_sha256"])
        target_sha = _managed_regular_hash(
            target,
            label="Conflict rollback canonical file",
            allow_missing=True,
        )
        quarantine_sha = _managed_regular_hash(
            quarantine,
            label="Conflict rollback quarantine file",
            allow_missing=True,
        )
        temporary_sha = _managed_regular_hash(
            temporary,
            label="Conflict rollback recovery file",
            allow_missing=True,
        )

        if (
            target_sha == loser_sha
            and quarantine_sha == winner_sha
            and temporary_sha is None
        ):
            return target.stat()

        if temporary_sha is None:
            if target_sha != winner_sha or quarantine_sha != loser_sha:
                raise DownloadConflictError("Conflict file changed after resolution")
            try:
                os.link(target, temporary, follow_symlinks=False)
            except FileExistsError:
                temporary_sha = _managed_regular_hash(
                    temporary,
                    label="Conflict rollback recovery file",
                )
            else:
                temporary_sha = winner_sha
                _fsync_directory(temporary.parent)
        if temporary_sha != winner_sha:
            raise DownloadConflictError("Conflict rollback recovery file changed")

        if target_sha in {winner_sha, None} and quarantine_sha == loser_sha:
            os.replace(quarantine, target)
            _fsync_directory(target.parent)
            target_sha = loser_sha
            quarantine_sha = None
        if target_sha != loser_sha or quarantine_sha is not None:
            raise DownloadConflictError(
                "Conflict rollback could not recover file state"
            )

        os.replace(temporary, quarantine)
        _fsync_directory(quarantine.parent)
        if (
            _managed_regular_hash(target, label="Rolled back canonical file")
            != loser_sha
            or _managed_regular_hash(
                quarantine,
                label="Rolled back quarantine file",
            )
            != winner_sha
        ):
            raise DownloadConflictError(
                "Conflict rollback did not reach a durable state"
            )
        return target.stat()

    async def rollback(
        self,
        task_id: UUID,
        resolution_id: str,
        *,
        operator: str,
    ) -> dict[str, Any]:
        task, job, _stage = await self._load(task_id, lock=True)
        if task.status in ACTIVE_RESOLUTION_CONFLICTS:
            raise DownloadConflictError("Conflict resolution cannot be rolled back while the task is active")
        audit = (await self.db.execute(
            select(MaintenanceAuditEvent)
            .where(
                MaintenanceAuditEvent.idempotency_key
                == f"download-conflict-resolution:{resolution_id}"
            )
            .with_for_update()
        )).scalar_one_or_none()
        if audit is None or str((audit.summary or {}).get("task_id")) != str(task_id):
            raise DownloadConflictError("Conflict resolution not found", status_code=404)
        summary = dict(audit.summary or {})
        stored_resolution_id = str(summary.get("resolution_id") or "")
        if stored_resolution_id != resolution_id:
            raise DownloadConflictError("Conflict resolution identity mismatch")
        if summary.get("state") != "applied":
            raise DownloadConflictError("Conflict resolution has already been rolled back")
        expires_at = datetime.fromisoformat(str(summary["expires_at"]))
        if expires_at <= datetime.now(timezone.utc):
            raise DownloadConflictError("Conflict resolution rollback window has expired")

        affected_work_ids: set[UUID] = set()
        derivative_requests: list[dict[str, Any]] = []
        for entry in summary.get("entries") or []:
            target_stat = self._rollback_files(
                entry,
                resolution_id=stored_resolution_id,
            )

            for asset_info in entry.get("assets") or []:
                asset = await self.db.get(Asset, UUID(str(asset_info["asset_id"])), with_for_update=True)
                if asset is None:
                    continue
                work_id = (await self.db.execute(
                    select(WorkSource.work_id)
                    .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
                    .where(AssetSource.asset_id == asset.id)
                    .limit(1)
                )).scalar_one_or_none()
                if work_id:
                    affected_work_ids.add(work_id)
                if entry["winner"] == "staged":
                    for key, value in (asset_info.get("before") or {}).items():
                        setattr(asset, key, value)
                else:
                    asset.file_size = target_stat.st_size
                    asset.sha256 = entry["loser_sha256"]
                    asset.width = asset.height = None
                    asset.duration = None
                    asset.thumb_sm_path = asset.thumb_md_path = asset.thumb_lg_path = None
                    asset.derivative_version = None
                    asset.derivative_source_size = None
                    asset.derivative_source_mtime_ns = None
                    asset.phash = asset.phash_version = None
                derivative_requests.append({
                    "asset_id": asset.id,
                    "requested": {"thumbnail": True, "dimensions": True, "video": True},
                    "source_size": target_stat.st_size,
                    "source_mtime_ns": target_stat.st_mtime_ns,
                })
            entry["winner"], entry["winner_sha256"], entry["loser_sha256"] = (
                "canonical" if entry["winner"] == "staged" else "staged",
                entry["loser_sha256"],
                entry["winner_sha256"],
            )
        await request_media_derivatives(self.db, derivative_requests)
        if affected_work_ids:
            await request_search_projection(self.db, affected_work_ids)
        summary["state"] = "rolled_back"
        summary["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        summary["rolled_back_by"] = operator
        audit.summary = summary
        task_meta = dict(task.meta or {})
        task_meta.pop("staging_conflict_resolution", None)
        task_meta["staging_conflict_rollback"] = {
            "resolution_id": resolution_id,
            "rolled_back_at": summary["rolled_back_at"],
            "operator": operator,
        }
        await TaskService(self.db).update_task(task, meta=task_meta)
        append_manifest_event(
            job,
            "staging_conflict_resolution_rolled_back",
            resolution_id=resolution_id,
            operator=operator,
        )
        await TaskService(self.db).add_event(
            task,
            "conflict_resolution_rolled_back",
            to_status=task.status,
            message="Download conflict resolution rolled back",
            payload={"resolution_id": resolution_id, "operator": operator},
        )
        await self.db.flush()
        return summary
