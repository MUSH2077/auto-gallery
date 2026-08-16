"""Project imported works to curation history and library metadata files."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import (
    Asset,
    AssetSource,
    ImportCurationOutbox,
    SourceCreator,
    Work,
    WorkSource,
)
from app.services.artifact_ledger import ArtifactLedger, managed_artifact_row
from app.services.heavy_io import LocalHeavyIOLock
from app.services.work_import import WorkImportService

logger = logging.getLogger(__name__)


class ImportProjectionClaimLost(RuntimeError):
    """A newer curation-outbox attempt owns at least one claimed row."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def request_import_projection(
    db: AsyncSession,
    records: list[dict[str, Any]],
) -> int:
    if not records:
        return 0
    work_ids = [UUID(str(record["work_id"])) for record in records]
    existing_rows = list(
        (
            await db.execute(
                select(ImportCurationOutbox).where(
                    ImportCurationOutbox.work_id.in_(work_ids)
                )
            )
        ).scalars()
    )
    existing = {row.work_id: row for row in existing_rows}
    now = _now()
    for record, work_id in zip(records, work_ids, strict=True):
        row = existing.get(work_id)
        if row is None:
            db.add(
                ImportCurationOutbox(
                    work_id=work_id,
                    creator_id=record.get("creator_id"),
                    repository_id=record.get("repository_id"),
                    source=record.get("source"),
                    source_work_id=record.get("source_work_id"),
                    state="pending",
                    available_at=now,
                    metadata_path=record.get("metadata_path"),
                    metadata_state=(
                        "pending"
                        if record.get("source") and record.get("source_work_id")
                        else "complete"
                    ),
                    metadata_available_at=now,
                )
            )
            continue

        # Existing works can legitimately receive upstream metadata changes or
        # additional pages.  Keep the durable projection identity current and,
        # when explicitly requested by that update path, invalidate an older
        # metadata writer by clearing its lease token before requeueing it.
        row.creator_id = record.get("creator_id")
        row.repository_id = record.get("repository_id")
        row.source = record.get("source")
        row.source_work_id = record.get("source_work_id")
        if record.get("metadata_path"):
            row.metadata_path = record["metadata_path"]
        if row.state == "failed":
            row.state = "pending"
            row.available_at = now
            row.last_error = None
        if (
            (row.metadata_state == "failed" or record.get("force_metadata_refresh"))
            and row.source
            and row.source_work_id
        ):
            row.metadata_state = "pending"
            row.metadata_available_at = now
            row.metadata_lease_expires_at = None
            row.metadata_lease_token = None
            row.metadata_completed_at = None
            row.metadata_last_error = None
    return len(work_ids)


def _eligible(now: datetime):
    return or_(
        (
            ImportCurationOutbox.state.in_(("pending", "failed"))
            & (ImportCurationOutbox.available_at <= now)
        ),
        (
            (ImportCurationOutbox.state == "processing")
            & (ImportCurationOutbox.lease_expires_at < now)
        ),
    )


def _metadata_eligible(now: datetime):
    return or_(
        and_(
            ImportCurationOutbox.metadata_state.in_(("pending", "failed")),
            ImportCurationOutbox.metadata_available_at <= now,
        ),
        and_(
            ImportCurationOutbox.metadata_state == "processing",
            ImportCurationOutbox.metadata_lease_expires_at.is_not(None),
            ImportCurationOutbox.metadata_lease_expires_at < now,
        ),
    )


async def _claim_batch(limit: int = 25) -> dict[str, Any] | None:
    async with async_session() as db:
        now = _now()
        first = (
            await db.execute(
                select(ImportCurationOutbox)
                .where(_eligible(now))
                .order_by(
                    ImportCurationOutbox.available_at,
                    ImportCurationOutbox.created_at,
                    ImportCurationOutbox.id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if first is None:
            return None
        if first.batch_key:
            rows = list(
                (
                    await db.execute(
                        select(ImportCurationOutbox)
                        .where(
                            ImportCurationOutbox.batch_key == first.batch_key,
                            _eligible(now),
                        )
                        .order_by(ImportCurationOutbox.created_at, ImportCurationOutbox.id)
                        .with_for_update(skip_locked=True)
                        .limit(min(25, max(1, limit)))
                    )
                ).scalars()
            )
            # An enforce-mode budget can split the importer's original
            # 25-work batch.  Give each exact subset a stable curation key;
            # reusing the original would make later slices look complete and
            # silently omit their individual CurationChanges.
            identity_digest = hashlib.sha256(
                "|".join(str(row.id) for row in rows).encode("ascii")
            ).hexdigest()[:56]
            # ImportCurationOutbox.batch_key is VARCHAR(64).  Derive solely
            # from the claimed identities so a failed/reclaimed slice does not
            # recursively append to its previous key and eventually overflow.
            batch_key = f"slice:{identity_digest}"
        else:
            rows = list(
                (
                    await db.execute(
                        select(ImportCurationOutbox)
                        .where(_eligible(now), ImportCurationOutbox.batch_key.is_(None))
                        .order_by(
                            ImportCurationOutbox.available_at,
                            ImportCurationOutbox.created_at,
                            ImportCurationOutbox.id,
                        )
                        .with_for_update(skip_locked=True)
                        .limit(min(25, max(1, limit)))
                    )
                ).scalars()
            )
            batch_key = uuid4().hex
        for row in rows:
            row.batch_key = batch_key
            row.state = "processing"
            row.attempts += 1
            row.lease_expires_at = now + timedelta(minutes=15)
        payload = {
            "batch_key": batch_key,
            "ids": [row.id for row in rows],
            "attempts": {row.id: int(row.attempts) for row in rows},
        }
        await db.commit()
        return payload


async def _claim_metadata_batch(
    limit: int = 25,
    *,
    preferred_ids: list[UUID] | None = None,
) -> dict[str, Any] | None:
    """Claim metadata intents without holding a transaction during file I/O."""

    async with async_session() as db:
        now = _now()
        lease_token = uuid4()
        statement = (
            select(ImportCurationOutbox)
            .where(_metadata_eligible(now))
            .order_by(
                ImportCurationOutbox.metadata_available_at,
                ImportCurationOutbox.created_at,
                ImportCurationOutbox.id,
            )
            .with_for_update(skip_locked=True)
            .limit(min(25, max(1, limit)))
        )
        if preferred_ids is not None:
            if not preferred_ids:
                return None
            statement = statement.where(
                ImportCurationOutbox.id.in_(preferred_ids)
            )
        rows = list((await db.execute(statement)).scalars())
        if not rows:
            return None
        for row in rows:
            row.metadata_state = "processing"
            row.metadata_attempts += 1
            row.metadata_lease_expires_at = now + timedelta(minutes=15)
            row.metadata_lease_token = lease_token
        payload = {
            "ids": [row.id for row in rows],
            "lease_token": lease_token,
            "attempts": {
                row.id: row.metadata_attempts
                for row in rows
            },
        }
        await db.commit()
        return payload


def _path_inside(root: Path, relative: str) -> Path:
    """Resolve an application-generated relative path without traversal."""

    candidate_path = Path(relative)
    if candidate_path.is_absolute():
        raise ValueError("metadata projection path must be relative")
    resolved_root = root.resolve()
    candidate = (resolved_root / candidate_path).resolve()
    candidate.relative_to(resolved_root)
    return candidate


async def _metadata_records(
    ids: list[UUID],
) -> tuple[dict[UUID, dict[str, Any]], dict[UUID, str]]:
    """Load a bounded projection from authoritative rows in two SQL queries."""

    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(
                        ImportCurationOutbox,
                        Work,
                        WorkSource,
                        SourceCreator.display_name,
                    )
                    .join(Work, Work.id == ImportCurationOutbox.work_id)
                    .join(
                        WorkSource,
                        and_(
                            WorkSource.work_id == Work.id,
                            WorkSource.source == ImportCurationOutbox.source,
                            WorkSource.source_work_id
                            == ImportCurationOutbox.source_work_id,
                        ),
                    )
                    .outerjoin(
                        SourceCreator,
                        and_(
                            SourceCreator.source == WorkSource.source,
                            SourceCreator.source_creator_id
                            == WorkSource.source_creator_id,
                        ),
                    )
                    .where(ImportCurationOutbox.id.in_(ids))
                )
            ).all()
        )
        work_source_ids = [work_source.id for _, _, work_source, _ in rows]
        asset_rows = list(
            (
                await db.execute(
                    select(
                        AssetSource.work_source_id,
                        Asset.file_path,
                        AssetSource.ordinal,
                        AssetSource.id,
                    )
                    .join(Asset, Asset.id == AssetSource.asset_id)
                    .where(AssetSource.work_source_id.in_(work_source_ids))
                    .order_by(
                        AssetSource.work_source_id,
                        AssetSource.ordinal.asc().nullslast(),
                        AssetSource.id,
                    )
                )
            ).all()
            if work_source_ids
            else []
        )

    assets_by_source: dict[UUID, list[Path]] = {}
    invalid_asset_sources: set[UUID] = set()
    download_root = Path(settings.download_root)
    for work_source_id, file_path, _ordinal, _asset_source_id in asset_rows:
        try:
            path = _path_inside(download_root, file_path)
        except (OSError, ValueError) as exc:
            invalid_asset_sources.add(work_source_id)
            logger.warning(
                "Rejected asset path while rebuilding metadata for %s: %s",
                work_source_id,
                exc,
            )
            continue
        assets_by_source.setdefault(work_source_id, []).append(path)

    records: dict[UUID, dict[str, Any]] = {}
    errors: dict[UUID, str] = {}
    library_root = Path(settings.library_root)
    for row, work, work_source, display_name in rows:
        try:
            if work_source.id in invalid_asset_sources:
                raise ValueError("metadata projection contains an unsafe asset path")
            if row.metadata_path:
                target = _path_inside(library_root, row.metadata_path)
            else:
                _creator_dir, lib_dir = WorkImportService.library_directory(
                    work_source.source,
                    work_source.raw_metadata or {},
                    work_source.source_work_id,
                )
                target = (lib_dir / "metadata.json").resolve()
                target.relative_to(library_root.resolve())
            if target.name != "metadata.json":
                raise ValueError("metadata projection target has an invalid filename")
            asset_files = assets_by_source.get(work_source.id, [])
            if not asset_files:
                raise ValueError("metadata projection has no committed assets")
            records[row.id] = {
                "work": SimpleNamespace(
                    id=work.id,
                    title=work.title,
                    posted_at=work.posted_at,
                ),
                "work_source": SimpleNamespace(
                    source=work_source.source,
                    source_work_id=work_source.source_work_id,
                ),
                "creator_name": (
                    display_name
                    or work_source.source_creator_id
                    or work_source.source
                ),
                "asset_files": asset_files,
                "target": target,
                "metadata_path": str(target.relative_to(library_root.resolve())),
                "creator_dir": target.parent.parent.name,
                "source": work_source.source,
                "source_work_id": work_source.source_work_id,
            }
        except Exception as exc:
            errors[row.id] = str(exc)[:4000]

    missing_ids = set(ids) - set(records) - set(errors)
    for row_id in missing_ids:
        errors[row_id] = "metadata projection source rows are missing"
    return records, errors


async def _finish_metadata(
    payload: dict[str, Any],
    results: dict[UUID, tuple[dict[str, Any] | None, str | None]],
) -> tuple[int, int]:
    """Checkpoint file results and ledger rows in one short transaction."""

    now = _now()
    complete = 0
    failed = 0
    artifact_rows: list[dict[str, Any]] = []
    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(ImportCurationOutbox).where(
                        ImportCurationOutbox.id.in_(payload["ids"]),
                        ImportCurationOutbox.metadata_state == "processing",
                        ImportCurationOutbox.metadata_lease_token
                        == payload["lease_token"],
                    ).with_for_update()
                )
            ).scalars()
        )
        for row in rows:
            expected_attempt = int(payload["attempts"].get(row.id, -1))
            if row.metadata_attempts != expected_attempt:
                # A newer lease owns this row.  The stale worker must not
                # publish its ledger row or overwrite the new result.
                continue
            record, error = results.get(
                row.id,
                (None, "metadata projection produced no result"),
            )
            row.metadata_lease_expires_at = None
            row.metadata_lease_token = None
            if error is None and record is not None:
                row.metadata_state = "complete"
                row.metadata_path = record["metadata_path"]
                row.metadata_completed_at = now
                row.metadata_last_error = None
                complete += 1
                artifact_rows.append(record["artifact_row"])
            else:
                attempts = expected_attempt
                row.metadata_state = "failed"
                row.metadata_available_at = now + timedelta(
                    seconds=min(3600, 15 * (2 ** min(attempts, 8)))
                )
                row.metadata_last_error = (error or "unknown metadata error")[:4000]
                failed += 1
        if artifact_rows:
            await ArtifactLedger(db).upsert_many(artifact_rows)
        await db.commit()
    return complete, failed


def _metadata_lock_path(target: Path) -> Path:
    digest = hashlib.sha256(
        str(target).encode("utf-8", "surrogatepass")
    ).hexdigest()
    # A fixed 256-way stripe keeps the shared lock directory O(1) instead of
    # accumulating one inode for every work in a 67k+ library.  Hash collisions
    # only cause a short, safe retry because acquisition is non-blocking.
    return Path(settings.download_root) / ".locks" / f"metadata-{digest[:2]}.lock"


def _acquire_metadata_locks(
    records: dict[UUID, dict[str, Any]],
) -> list[LocalHeavyIOLock] | None:
    """Non-blockingly fence every target in a stable order."""

    paths = sorted(
        {_metadata_lock_path(record["target"]) for record in records.values()}
    )
    locks: list[LocalHeavyIOLock] = []
    try:
        for path in paths:
            lock = LocalHeavyIOLock(path)
            if not lock.try_acquire():
                for acquired in reversed(locks):
                    acquired.release()
                return None
            locks.append(lock)
        return locks
    except BaseException:
        for acquired in reversed(locks):
            acquired.release()
        raise


def _release_metadata_locks(locks: list[LocalHeavyIOLock]) -> None:
    for lock in reversed(locks):
        lock.release()


async def _owned_metadata_attempts(payload: dict[str, Any]) -> set[UUID]:
    """Fence cached descriptions against a newer database claim."""

    async with async_session() as db:
        rows = await db.execute(
            select(
                ImportCurationOutbox.id,
                ImportCurationOutbox.metadata_attempts,
            ).where(
                ImportCurationOutbox.id.in_(payload["ids"]),
                ImportCurationOutbox.metadata_state == "processing",
                ImportCurationOutbox.metadata_lease_token
                == payload["lease_token"],
            )
        )
        return {
            row_id
            for row_id, attempt in rows
            if int(attempt) == int(payload["attempts"].get(row_id, -1))
        }


async def _project_metadata(
    payload: dict[str, Any],
) -> tuple[int, int]:
    """Write idempotent metadata files with no SQL transaction held open."""

    records, errors = await _metadata_records(payload["ids"])
    results: dict[UUID, tuple[dict[str, Any] | None, str | None]] = {
        row_id: (None, error)
        for row_id, error in errors.items()
    }
    locks = await asyncio.to_thread(_acquire_metadata_locks, records)
    if locks is None:
        busy_error = "metadata projection target is busy"
        results.update(
            {row_id: (None, busy_error) for row_id in records}
        )
        return await _finish_metadata(payload, results)
    try:
        # Verify the exact database attempt after acquiring all target locks.
        # A stale process therefore cannot write after a newer process has
        # already projected the same file; the non-blocking contender backs off.
        owned_ids = await _owned_metadata_attempts(payload)
        for row_id, record in records.items():
            if row_id not in owned_ids:
                continue
            try:
                await asyncio.to_thread(
                    WorkImportService.write_library_metadata,
                    record["target"].parent,
                    record["work"],
                    record["work_source"],
                    record["creator_name"],
                    record["asset_files"],
                )
            except Exception as exc:
                results[row_id] = (None, str(exc)[:4000])
                logger.warning(
                    "Metadata projection failed for outbox row %s",
                    row_id,
                    exc_info=True,
                )
            else:
                try:
                    record["artifact_row"] = managed_artifact_row(
                        record["target"],
                        Path(settings.library_root).resolve(),
                        "library",
                        record["source"],
                        record["creator_dir"],
                        record["source_work_id"],
                        "metadata_json",
                    )
                except Exception as exc:
                    results[row_id] = (None, str(exc)[:4000])
                else:
                    results[row_id] = (record, None)
    finally:
        await asyncio.to_thread(_release_metadata_locks, locks)
    return await _finish_metadata(payload, results)


async def _fail_metadata(payload: dict[str, Any], exc: Exception) -> None:
    await _finish_metadata(
        payload,
        {
            row_id: (None, str(exc)[:4000])
            for row_id in payload["ids"]
        },
    )


async def _project(payload: dict[str, Any]) -> UUID | None:
    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(ImportCurationOutbox)
                    .where(ImportCurationOutbox.id.in_(payload["ids"]))
                    .order_by(ImportCurationOutbox.created_at, ImportCurationOutbox.id)
                    .with_for_update()
                )
            ).scalars()
        )
        expected_attempts = payload["attempts"]
        owned = {
            row.id
            for row in rows
            if row.state == "processing"
            and row.batch_key == payload["batch_key"]
            and int(row.attempts) == int(expected_attempts.get(row.id, -1))
        }
        if owned != set(payload["ids"]):
            await db.rollback()
            raise ImportProjectionClaimLost(
                "import projection outbox claim was superseded"
            )
        works = {
            work.id: work
            for work in (
                await db.execute(
                    select(Work).where(Work.id.in_([row.work_id for row in rows]))
                )
            ).scalars()
        }
        records = [
            {
                "work": works[row.work_id],
                "creator_id": row.creator_id,
                "repository_id": row.repository_id,
                "source": row.source,
                "source_work_id": row.source_work_id,
            }
            for row in rows
            if row.work_id in works
        ]
        commit_id: UUID | None = None
        if records:
            from app.services.curation import CurationService

            commit, _visibilities = await CurationService(db).record_imported_works(
                records,
                dedupe_key=f"import-batch:{payload['batch_key']}",
            )
            commit_id = commit.id
        for row in rows:
            row.state = "complete"
            row.curation_commit_id = commit_id
            row.completed_at = _now()
            row.lease_expires_at = None
            row.last_error = None
        await db.commit()
        return commit_id


async def _fail(payload: dict[str, Any], exc: Exception) -> None:
    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(ImportCurationOutbox).where(
                        ImportCurationOutbox.id.in_(payload["ids"])
                    ).with_for_update()
                )
            ).scalars()
        )
        expected_attempts = payload["attempts"]
        rows = [
            row
            for row in rows
            if row.state == "processing"
            and row.batch_key == payload["batch_key"]
            and int(row.attempts) == int(expected_attempts.get(row.id, -1))
        ]
        if not rows:
            await db.rollback()
            return
        highest_attempt = max(
            (int(value) for value in expected_attempts.values()),
            default=1,
        )
        available_at = _now() + timedelta(
            seconds=min(3600, 15 * (2 ** min(highest_attempt, 8)))
        )
        for row in rows:
            row.state = "failed"
            row.available_at = available_at
            row.lease_expires_at = None
            row.last_error = str(exc)[:4000]
        await db.commit()


async def process_import_projection_outbox(
    *,
    limit: int = 25,
    max_seconds: float = 20.0,
) -> dict[str, int | bool]:
    batches = 0
    works = 0
    failed = 0
    metadata_batches = 0
    metadata_processed = 0
    metadata_failed = 0
    claimed_units = 0
    found_empty = False
    started = monotonic()
    bounded_limit = max(1, limit)
    bounded_seconds = max(0.001, float(max_seconds))
    while (
        claimed_units < bounded_limit
        and monotonic() - started < bounded_seconds
    ):
        remaining = min(25, bounded_limit - claimed_units)
        payload = await _claim_batch(remaining)
        metadata_payload: dict[str, Any] | None = None
        if payload is not None:
            claimed_units += len(payload["ids"])
            try:
                await _project(payload)
            except ImportProjectionClaimLost:
                logger.info(
                    "Import projection batch %s was superseded",
                    payload["batch_key"],
                )
            except Exception as exc:
                failed += len(payload["ids"])
                logger.warning(
                    "Import projection batch %s failed",
                    payload["batch_key"],
                    exc_info=True,
                )
                await _fail(payload, exc)
            else:
                batches += 1
                works += len(payload["ids"])

            # Prefer the same works so a fresh import normally closes both
            # projections in one bounded coordinator slice.  Curation is
            # already committed here; a metadata failure cannot recreate it.
            metadata_payload = await _claim_metadata_batch(
                len(payload["ids"]),
                preferred_ids=payload["ids"],
            )
        else:
            metadata_payload = await _claim_metadata_batch(remaining)
            if metadata_payload is None:
                found_empty = True
                break
            claimed_units += len(metadata_payload["ids"])

        if metadata_payload is not None:
            try:
                completed, projection_failed = await _project_metadata(
                    metadata_payload
                )
            except Exception as exc:
                projection_failed = len(metadata_payload["ids"])
                logger.warning(
                    "Import metadata projection batch failed",
                    exc_info=True,
                )
                await _fail_metadata(metadata_payload, exc)
                completed = 0
            metadata_batches += 1
            metadata_processed += completed
            metadata_failed += projection_failed

    slice_exhausted = not found_empty and (
        claimed_units >= bounded_limit
        or monotonic() - started >= bounded_seconds
    )
    return {
        "claimed": claimed_units,
        "batches": batches,
        "processed": works,
        "failed": failed,
        "metadata_batches": metadata_batches,
        "metadata_processed": metadata_processed,
        "metadata_failed": metadata_failed,
        "slice_exhausted": slice_exhausted,
        "more_likely": slice_exhausted,
    }
