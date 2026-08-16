"""Idempotently project one already-downloaded upstream work into PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select

from app.config import settings
from app.database import async_session
from app.jobs.import_runner import (
    _prepare_import_metadata,
    _update_existing_work_groups,
)
from app.models import AssetSource, DownloadJob, StorageArtifact, WorkSource
from app.providers import registry
from app.services.artifact_discovery import group_metadata_by_work


async def run(download_job_id: UUID, source_work_id: str, *, dry_run: bool) -> dict:
    async with async_session() as db:
        download_job = await db.get(DownloadJob, download_job_id)
        if download_job is None:
            raise RuntimeError(f"download job {download_job_id} was not found")
        metadata_paths = list(
            (
                await db.execute(
                    select(StorageArtifact.file_path)
                    .where(
                        StorageArtifact.source == download_job.source,
                        StorageArtifact.source_work_id == source_work_id,
                        StorageArtifact.artifact_type == "metadata_json",
                    )
                    .order_by(StorageArtifact.file_path)
                )
            ).scalars()
        )

    root = Path(settings.download_root)
    files = [root / value for value in metadata_paths if (root / value).is_file()]
    provider = registry.get(download_job.source)
    groups, invalid = group_metadata_by_work(provider, files)
    selected = groups.get(source_work_id)
    if not selected:
        raise RuntimeError(
            f"no valid metadata remains for {download_job.source}/{source_work_id}"
        )
    preview = {
        "download_job_id": str(download_job_id),
        "source": download_job.source,
        "source_work_id": source_work_id,
        "metadata_files": [str(path) for path, _raw in selected],
        "invalid_metadata": [str(path) for path in invalid],
    }
    if dry_run:
        return {"dry_run": True, **preview}

    prepared, failures = await _prepare_import_metadata(
        provider,
        download_job,
        {source_work_id: selected},
    )
    if failures:
        raise RuntimeError(json.dumps(failures, ensure_ascii=False))
    result = await _update_existing_work_groups(
        provider,
        download_job,
        prepared,
    )
    if result["failures"]:
        raise RuntimeError(json.dumps(result["failures"], ensure_ascii=False))

    async with async_session() as db:
        work_source = (
            await db.execute(
                select(WorkSource).where(
                    WorkSource.source == download_job.source,
                    WorkSource.source_work_id == source_work_id,
                )
            )
        ).scalar_one()
        asset_count = int(
            await db.scalar(
                select(func.count(AssetSource.id)).where(
                    AssetSource.work_source_id == work_source.id
                )
            )
            or 0
        )
    return {
        "dry_run": False,
        **preview,
        "result": result,
        "work_id": str(work_source.work_id),
        "page_count": (work_source.raw_metadata or {}).get("page_count"),
        "asset_count": asset_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("download_job_id", type=UUID)
    parser.add_argument("source_work_id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(
        run(
            args.download_job_id,
            args.source_work_id,
            dry_run=not args.apply,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
