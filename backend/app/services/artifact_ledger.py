"""PostgreSQL-backed artifact inventory and import coordination."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import case, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.storage_artifact import StorageArtifact
from app.models.import_job import ImportJob
from app.models.work_source import WorkSource


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".webm"}


@dataclass(frozen=True, slots=True)
class WorkClaimBatch:
    """Classification produced by one bounded, non-blocking claim attempt."""

    claimed: tuple[str, ...] = ()
    contended: tuple[str, ...] = ()
    existing: tuple[str, ...] = ()


def artifact_type(path: Path) -> str:
    if path.suffix.lower() == ".json":
        return "metadata_json"
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return "image"
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return "video"
    if path.suffix.lower() == ".zip":
        return "archive"
    return "unknown"


def artifact_row(
    path: Path,
    root: Path,
    download_job_id: UUID | None = None,
    *,
    source: str | None = None,
    creator_dir: str | None = None,
    source_work_id: str | None = None,
) -> dict | None:
    rel = path.relative_to(root)
    parts = rel.parts
    # Legacy callers infer identity from a source/creator/work directory and
    # therefore still require four path components. Provider-aware callers
    # pass the parsed work ID explicitly, which also supports gallery-dl's
    # flatter ``twitter/{creator}/{filename}`` layout.
    resolved_source = source or (parts[0] if parts else None)
    resolved_creator = creator_dir or (parts[1] if len(parts) > 1 else None)
    resolved_work_id = source_work_id or (parts[2] if len(parts) > 2 else None)
    if not resolved_source or not resolved_creator or not resolved_work_id:
        return None
    stat = path.stat()
    return {
        "id": uuid4(),
        "storage_root": "downloads",
        "file_path": str(rel),
        "source": resolved_source,
        "creator_dir": resolved_creator,
        "source_work_id": resolved_work_id,
        "file_name": path.name,
        "artifact_type": artifact_type(path),
        "file_size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "download_job_id": download_job_id,
        "state": "new",
    }


def managed_artifact_row(path: Path, root: Path, storage_root: str, source: str,
                         creator_dir: str, source_work_id: str, kind: str,
                         state: str = "done") -> dict:
    stat = path.stat()
    return {
        "id": uuid4(), "storage_root": storage_root,
        "file_path": str(path.relative_to(root)), "source": source,
        "creator_dir": creator_dir, "source_work_id": source_work_id,
        "file_name": path.name, "artifact_type": kind,
        "file_size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        "state": state,
    }


class ArtifactLedger:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def upsert_many(self, rows: list[dict], chunk_size: int = 1000) -> int:
        rows = [row for row in rows if row and row["artifact_type"] != "unknown"]
        for offset in range(0, len(rows), chunk_size):
            batch = rows[offset:offset + chunk_size]
            stmt = insert(StorageArtifact).values(batch)
            should_refresh = (
                StorageArtifact.mtime_ns.is_distinct_from(stmt.excluded.mtime_ns)
                | (StorageArtifact.state == "failed")
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_storage_artifacts_root_path",
                set_={
                    "source": stmt.excluded.source,
                    "creator_dir": stmt.excluded.creator_dir,
                    "source_work_id": stmt.excluded.source_work_id,
                    "file_name": stmt.excluded.file_name,
                    "artifact_type": stmt.excluded.artifact_type,
                    "file_size": stmt.excluded.file_size,
                    "mtime_ns": stmt.excluded.mtime_ns,
                    # Only reassign download_job_id when the file has genuinely
                    # changed (mtime differs).  Otherwise keep the original owner
                    # to prevent cross-contamination between jobs of different
                    # creators that share the same source root.
                    "download_job_id": case(
                        (should_refresh, stmt.excluded.download_job_id),
                        else_=StorageArtifact.download_job_id,
                    ),
                    "state": case(
                        (should_refresh, "new"),
                        else_=StorageArtifact.state,
                    ),
                    "import_job_id": case(
                        (should_refresh, None),
                        else_=StorageArtifact.import_job_id,
                    ),
                    "lease_token": case(
                        (should_refresh, None),
                        else_=StorageArtifact.lease_token,
                    ),
                    "lease_expires_at": case(
                        (should_refresh, None),
                        else_=StorageArtifact.lease_expires_at,
                    ),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            await self.db.execute(stmt)
        return len(rows)

    async def new_metadata_paths(self, download_job_id: UUID) -> list[str]:
        result = await self.db.execute(
            select(StorageArtifact.file_path)
            .where(
                StorageArtifact.download_job_id == download_job_id,
                StorageArtifact.artifact_type == "metadata_json",
                StorageArtifact.state.in_(("new", "importing")),
            )
            .order_by(StorageArtifact.created_at, StorageArtifact.id)
        )
        return list(result.scalars())

    async def counts(self, download_job_id: UUID) -> tuple[int, int, list[str]]:
        result = await self.db.execute(
            select(StorageArtifact.artifact_type, func.count(StorageArtifact.id))
            .where(StorageArtifact.download_job_id == download_job_id,
                   StorageArtifact.state.in_(("new", "importing")))
            .group_by(StorageArtifact.artifact_type)
        )
        counts = dict(result.all())
        paths = await self.new_metadata_paths(download_job_id)
        return len(paths), int(counts.get("image", 0)), paths

    async def claim_work_batch(
        self,
        download_job_id: UUID,
        import_job_id: UUID,
        *,
        lease_token: UUID,
        source: str | None = None,
        source_work_ids: list[str] | tuple[str, ...] | set[str] | None = None,
        limit: int = 25,
        lease_seconds: int = 900,
    ) -> WorkClaimBatch:
        """Atomically lease up to ``limit`` works without blocking peers.

        A deterministic metadata row is the lock representative for each work.
        Locking only representatives avoids claiming the same multi-page work
        twice while ``SKIP LOCKED`` lets another worker move on immediately.
        Once representatives are locked, every artifact belonging to those
        works moves to ``importing`` in one UPDATE.  The caller owns the short
        transaction and must commit before doing filesystem or media work.
        """
        now = datetime.now(timezone.utc)
        limit = max(1, min(int(limit), 25))
        candidates = (
            list(dict.fromkeys(str(value) for value in source_work_ids))
            if source_work_ids is not None
            else None
        )
        if candidates is not None:
            candidates = candidates[:limit]
            if not candidates:
                return WorkClaimBatch()

        claim_candidates = candidates

        eligible = (
            (StorageArtifact.state == "new")
            | (
                (StorageArtifact.state == "importing")
                & (
                    StorageArtifact.lease_expires_at.is_(None)
                    | (StorageArtifact.lease_expires_at < now)
                )
            )
        )
        identity_scope = (
            (
                StorageArtifact.source == source,
                *(
                    (StorageArtifact.source_work_id.in_(claim_candidates),)
                    if claim_candidates is not None
                    else ()
                ),
            )
            if source is not None
            else (
                StorageArtifact.download_job_id == download_job_id,
                *(
                    (StorageArtifact.source_work_id.in_(claim_candidates),)
                    if claim_candidates is not None
                    else ()
                ),
            )
        )
        active_artifact = aliased(StorageArtifact)
        active_identity_lease = exists().where(
            active_artifact.source == StorageArtifact.source,
            active_artifact.source_work_id == StorageArtifact.source_work_id,
            active_artifact.artifact_type == "metadata_json",
            active_artifact.state == "importing",
            active_artifact.lease_expires_at.is_not(None),
            active_artifact.lease_expires_at > now,
        ).correlate(StorageArtifact)
        execution_is_active = exists().where(
            ImportJob.id == import_job_id,
            ImportJob.execution_token == lease_token,
            ImportJob.status == "running",
        )
        ranked = (
            select(
                StorageArtifact.id.label("artifact_id"),
                StorageArtifact.source_work_id.label("source_work_id"),
                func.row_number()
                .over(
                    partition_by=(
                        StorageArtifact.source,
                        StorageArtifact.source_work_id,
                    ),
                    order_by=(StorageArtifact.created_at, StorageArtifact.id),
                )
                .label("work_rank"),
            )
            .where(
                *identity_scope,
                StorageArtifact.artifact_type == "metadata_json",
                eligible,
                execution_is_active,
                ~active_identity_lease,
                *(
                    [
                        ~exists().where(
                            WorkSource.source == source,
                            WorkSource.source_work_id
                            == StorageArtifact.source_work_id,
                        ).correlate(StorageArtifact)
                    ]
                    if source is not None
                    else []
                ),
            )
            .subquery("ranked_metadata_artifacts")
        )
        claimed_result = await self.db.execute(
            select(StorageArtifact.source_work_id)
            .join(ranked, ranked.c.artifact_id == StorageArtifact.id)
            .where(ranked.c.work_rank == 1)
            .order_by(StorageArtifact.created_at, StorageArtifact.id)
            .limit(limit)
            .with_for_update(of=StorageArtifact, skip_locked=True)
        )
        claimed = list(dict.fromkeys(claimed_result.scalars()))
        if claimed:
            update_scope = (
                (StorageArtifact.source == source,)
                if source is not None
                else (StorageArtifact.download_job_id == download_job_id,)
            )
            await self.db.execute(
                update(StorageArtifact)
                .where(
                    *update_scope,
                    StorageArtifact.source_work_id.in_(claimed),
                    eligible,
                )
                .values(
                    state="importing",
                    import_job_id=import_job_id,
                    lease_token=lease_token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    attempts=StorageArtifact.attempts
                    + case(
                        (StorageArtifact.artifact_type == "metadata_json", 1),
                        else_=0,
                    ),
                    last_error=None,
                )
            )
        claimed_set = set(claimed)
        ordered_claimed = tuple(
            claimed
            if claim_candidates is None
            else (value for value in claim_candidates if value in claimed_set)
        )
        contended = tuple(
            value
            for value in (claim_candidates or ())
            if value not in claimed_set
        )
        existing: tuple[str, ...] = ()
        if source is not None and contended:
            existing_set = set(
                (
                    await self.db.execute(
                        select(WorkSource.source_work_id).where(
                            WorkSource.source == source,
                            WorkSource.source_work_id.in_(contended),
                        )
                    )
                ).scalars()
            )
            existing = tuple(
                value for value in contended if value in existing_set
            )
            contended = tuple(
                value for value in contended if value not in existing_set
            )
            if existing:
                await self.mark_works(
                    download_job_id,
                    set(existing),
                    "done",
                    source=source,
                    preserve_active_leases=True,
                )
        return WorkClaimBatch(
            claimed=ordered_claimed,
            contended=contended,
            existing=existing,
        )

    async def claim_work(
        self,
        download_job_id: UUID,
        source_work_id: str,
        import_job_id: UUID,
        lease_seconds: int = 900,
        *,
        lease_token: UUID | None = None,
        source: str | None = None,
    ) -> bool:
        claimed = await self.claim_work_batch(
            download_job_id,
            import_job_id,
            lease_token=lease_token or uuid4(),
            source=source,
            source_work_ids=[source_work_id],
            limit=1,
            lease_seconds=lease_seconds,
        )
        return bool(claimed.claimed)

    async def renew_work_leases(
        self,
        download_job_id: UUID,
        source_work_ids: list[str] | tuple[str, ...] | set[str],
        *,
        expected_import_job_id: UUID,
        expected_lease_token: UUID,
        lease_seconds: int = 900,
    ) -> set[str]:
        """CAS-renew leases and return identities still owned by this run.

        The UPDATE itself serializes against an expired-lease claimant.  At
        READ COMMITTED PostgreSQL rechecks the token/expiry predicates after a
        concurrent row update, so two executions cannot both obtain a positive
        ownership result.
        """

        values = list(dict.fromkeys(str(value) for value in source_work_ids))
        if not values:
            return set()
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            update(StorageArtifact)
            .where(
                StorageArtifact.source_work_id.in_(values),
                StorageArtifact.artifact_type == "metadata_json",
                StorageArtifact.state == "importing",
                StorageArtifact.import_job_id == expected_import_job_id,
                StorageArtifact.lease_token == expected_lease_token,
                exists().where(
                    ImportJob.id == expected_import_job_id,
                    ImportJob.execution_token == expected_lease_token,
                    ImportJob.status == "running",
                ),
                StorageArtifact.lease_expires_at.is_not(None),
                StorageArtifact.lease_expires_at > now,
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            .returning(StorageArtifact.source_work_id)
        )
        # Claim eligibility is represented by metadata rows, so renewing those
        # rows is sufficient and keeps this ownership check to one statement.
        return set(result.scalars())

    async def mark_work(self, download_job_id: UUID, source_work_id: str, state: str, error: str | None = None) -> None:
        await self.db.execute(
            update(StorageArtifact)
            .where(StorageArtifact.download_job_id == download_job_id,
                   StorageArtifact.source_work_id == source_work_id)
            .values(
                state=state,
                lease_token=None,
                lease_expires_at=None,
                last_error=error,
            )
        )

    async def mark_works(
        self,
        download_job_id: UUID,
        source_work_ids: list[str] | set[str],
        state: str,
        error: str | None = None,
        *,
        chunk_size: int = 1000,
        expected_import_job_id: UUID | None = None,
        expected_lease_token: UUID | None = None,
        source: str | None = None,
        preserve_active_leases: bool = False,
    ) -> int:
        """Bulk terminal/checkpoint update without a transaction per work."""
        values = list(dict.fromkeys(str(item) for item in source_work_ids))
        changed = 0
        for offset in range(0, len(values), chunk_size):
            conditions = [
                StorageArtifact.source_work_id.in_(
                    values[offset : offset + chunk_size]
                ),
            ]
            if expected_lease_token is None:
                conditions.append(
                    StorageArtifact.source == source
                    if source is not None
                    else StorageArtifact.download_job_id == download_job_id
                )
            if expected_import_job_id is not None:
                conditions.append(
                    StorageArtifact.import_job_id == expected_import_job_id
                )
            if expected_lease_token is not None:
                conditions.append(
                    StorageArtifact.lease_token == expected_lease_token
                )
                if expected_import_job_id is not None:
                    conditions.append(
                        exists().where(
                            ImportJob.id == expected_import_job_id,
                            ImportJob.execution_token == expected_lease_token,
                            ImportJob.status == "running",
                        )
                    )
            if preserve_active_leases:
                now = datetime.now(timezone.utc)
                conditions.append(
                    ~(
                        (StorageArtifact.state == "importing")
                        & StorageArtifact.lease_expires_at.is_not(None)
                        & (StorageArtifact.lease_expires_at > now)
                    )
                )
            result = await self.db.execute(
                update(StorageArtifact)
                .where(*conditions)
                .values(
                    state=state,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=error,
                )
            )
            changed += int(result.rowcount or 0)
        return changed

    async def mark_work_results(
        self,
        download_job_id: UUID,
        results: dict[str, tuple[str, str | None]],
        *,
        expected_import_job_id: UUID | None = None,
        expected_lease_token: UUID | None = None,
    ) -> set[str]:
        """Apply per-work terminal results in one bounded CASE update.

        Error text remains work-specific without turning a partially failed
        25-work slice into one transaction per work.
        """

        items = [
            (str(source_work_id), state, error)
            for source_work_id, (state, error) in results.items()
        ]
        if not items:
            return set()
        changed: set[str] = set()
        for offset in range(0, len(items), 500):
            batch = items[offset : offset + 500]
            source_work_ids = [source_work_id for source_work_id, _, _ in batch]
            conditions = [
                StorageArtifact.source_work_id.in_(source_work_ids),
            ]
            if expected_lease_token is None:
                conditions.append(
                    StorageArtifact.download_job_id == download_job_id
                )
            if expected_import_job_id is not None:
                conditions.append(
                    StorageArtifact.import_job_id == expected_import_job_id
                )
            if expected_lease_token is not None:
                conditions.append(
                    StorageArtifact.lease_token == expected_lease_token
                )
                if expected_import_job_id is not None:
                    conditions.append(
                        exists().where(
                            ImportJob.id == expected_import_job_id,
                            ImportJob.execution_token == expected_lease_token,
                            ImportJob.status == "running",
                        )
                    )
            result = await self.db.execute(
                update(StorageArtifact)
                .where(*conditions)
                .values(
                    state=case(
                        *(
                            (StorageArtifact.source_work_id == source_work_id, state)
                            for source_work_id, state, _ in batch
                        ),
                        else_=StorageArtifact.state,
                    ),
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=case(
                        *(
                            (StorageArtifact.source_work_id == source_work_id, error)
                            for source_work_id, _, error in batch
                        ),
                        else_=StorageArtifact.last_error,
                    ),
                )
                .returning(StorageArtifact.source_work_id)
            )
            changed.update(result.scalars())
        return changed

    async def release_owned_leases(
        self,
        import_job_id: UUID,
        lease_token: UUID,
    ) -> set[str]:
        """Return unfinished leases owned by one execution to the ready pool."""

        result = await self.db.execute(
            update(StorageArtifact)
            .where(
                StorageArtifact.import_job_id == import_job_id,
                StorageArtifact.lease_token == lease_token,
                StorageArtifact.state == "importing",
            )
            .values(
                state="new",
                import_job_id=None,
                lease_token=None,
                lease_expires_at=None,
            )
            .returning(StorageArtifact.source_work_id)
        )
        return set(result.scalars())
