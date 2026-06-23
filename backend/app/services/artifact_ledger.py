"""PostgreSQL-backed artifact inventory and import coordination."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.storage_artifact import StorageArtifact


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def artifact_type(path: Path) -> str:
    if path.suffix.lower() == ".json":
        return "metadata_json"
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return "image"
    if path.suffix.lower() == ".zip":
        return "archive"
    return "unknown"


def artifact_row(path: Path, root: Path, download_job_id: UUID | None = None) -> dict | None:
    rel = path.relative_to(root)
    parts = rel.parts
    if len(parts) < 4:
        return None
    stat = path.stat()
    return {
        "id": uuid4(),
        "storage_root": "downloads",
        "file_path": str(rel),
        "source": parts[0],
        "creator_dir": parts[1],
        "source_work_id": parts[2],
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
            stmt = stmt.on_conflict_do_update(
                constraint="uq_storage_artifacts_root_path",
                set_={
                    "file_size": stmt.excluded.file_size,
                    "mtime_ns": stmt.excluded.mtime_ns,
                    # Only reassign download_job_id when the file has genuinely
                    # changed (mtime differs).  Otherwise keep the original owner
                    # to prevent cross-contamination between jobs of different
                    # creators that share the same source root.
                    "download_job_id": case(
                        (StorageArtifact.mtime_ns.is_distinct_from(stmt.excluded.mtime_ns), stmt.excluded.download_job_id),
                        else_=StorageArtifact.download_job_id,
                    ),
                    "state": case(
                        (StorageArtifact.mtime_ns.is_distinct_from(stmt.excluded.mtime_ns), "new"),
                        else_=StorageArtifact.state,
                    ),
                    "import_job_id": case(
                        (StorageArtifact.mtime_ns.is_distinct_from(stmt.excluded.mtime_ns), None),
                        else_=StorageArtifact.import_job_id,
                    ),
                    "lease_expires_at": case(
                        (StorageArtifact.mtime_ns.is_distinct_from(stmt.excluded.mtime_ns), None),
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

    async def claim_work(self, download_job_id: UUID, source_work_id: str, import_job_id: UUID, lease_seconds: int = 900) -> bool:
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            update(StorageArtifact)
            .where(
                StorageArtifact.download_job_id == download_job_id,
                StorageArtifact.source_work_id == source_work_id,
                StorageArtifact.artifact_type == "metadata_json",
                (StorageArtifact.state == "new") |
                ((StorageArtifact.state == "importing") & (
                    (StorageArtifact.lease_expires_at < now) |
                    (StorageArtifact.import_job_id == import_job_id)
                )),
            )
            .values(state="importing", import_job_id=import_job_id,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    attempts=StorageArtifact.attempts + 1, last_error=None)
        )
        return bool(result.rowcount)

    async def mark_work(self, download_job_id: UUID, source_work_id: str, state: str, error: str | None = None) -> None:
        await self.db.execute(
            update(StorageArtifact)
            .where(StorageArtifact.download_job_id == download_job_id,
                   StorageArtifact.source_work_id == source_work_id)
            .values(state=state, lease_expires_at=None, last_error=error)
        )
