"""Idempotent repair of metadata and derived posters for stored videos."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.asset import Asset
from app.models.asset_source import AssetSource
from app.models.work_source import WorkSource
from app.services.artifact_ledger import ArtifactLedger, managed_artifact_row
from app.services.media_assets import (
    browser_video_mime_type,
    is_browser_playable_video,
    render_video_derivatives,
)
from app.services.work_import import WorkImportService


class VideoBackfillReport(TypedDict):
    matched: int
    needs_repair: int
    repaired: int
    failed: int
    dry_run: bool


async def backfill_video_assets(
    db: AsyncSession,
    *,
    apply: bool = False,
    force: bool = False,
) -> VideoBackfillReport:
    """Inspect video assets and repair missing metadata/posters."""

    rows = (await db.execute(
        select(Asset, WorkSource)
        .join(AssetSource, AssetSource.asset_id == Asset.id)
        .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
        .where(
            or_(
                Asset.mime_type.like("video/%"),
                Asset.file_name.ilike("%.mp4"),
                Asset.file_name.ilike("%.webm"),
            )
        )
        .order_by(Asset.created_at, WorkSource.created_at)
    )).all()
    unique: dict = {}
    for asset, work_source in rows:
        if is_browser_playable_video(asset.mime_type, asset.file_name):
            unique.setdefault(asset.id, (asset, work_source))

    report: VideoBackfillReport = {
        "matched": len(unique),
        "needs_repair": 0,
        "repaired": 0,
        "failed": 0,
        "dry_run": not apply,
    }
    ledger_rows: list[dict] = []
    changed_work_ids: set = set()
    for asset, work_source in unique.values():
        source_path = Path(settings.download_root) / asset.file_path
        creator_dir, library_dir = WorkImportService.library_directory(
            work_source.source,
            work_source.raw_metadata,
            work_source.source_work_id,
        )
        expected_thumbnail = library_dir / f"{source_path.stem}.thumbnail.webp"
        expected_poster = library_dir / f"{source_path.stem}.poster.webp"
        needs_repair = force or any(
            value is None
            for value in (
                asset.width,
                asset.height,
                asset.duration,
                asset.thumb_sm_path,
                asset.thumb_lg_path,
            )
        ) or not expected_thumbnail.exists() or not expected_poster.exists()
        if not needs_repair:
            continue
        report["needs_repair"] += 1
        if not apply:
            continue
        if not source_path.exists():
            report["failed"] += 1
            continue

        derivatives = render_video_derivatives(
            source_path,
            library_dir,
            source_path.stem,
            force=force,
        )
        if derivatives.inspection:
            asset.width = derivatives.inspection.width
            asset.height = derivatives.inspection.height
            asset.duration = derivatives.inspection.duration
            asset.mime_type = browser_video_mime_type(asset.file_name, asset.mime_type)
        if derivatives.thumbnail_path:
            asset.thumb_sm_path = str(
                derivatives.thumbnail_path.relative_to(settings.library_root)
            )
        if derivatives.poster_path:
            asset.thumb_lg_path = str(
                derivatives.poster_path.relative_to(settings.library_root)
            )
        if not derivatives.inspection or not derivatives.thumbnail_path or not derivatives.poster_path:
            report["failed"] += 1
            continue

        for path, file_type in (
            (derivatives.thumbnail_path, "thumbnail"),
            (derivatives.poster_path, "video_poster"),
        ):
            ledger_rows.append(
                managed_artifact_row(
                    path,
                    Path(settings.library_root),
                    "library",
                    work_source.source,
                    creator_dir,
                    work_source.source_work_id,
                    file_type,
                )
            )
        report["repaired"] += 1
        changed_work_ids.add(work_source.work_id)

    if apply:
        await ArtifactLedger(db).upsert_many(ledger_rows)
        if changed_work_ids:
            from app.services.search_projection_outbox import request_search_projection

            await request_search_projection(db, changed_work_ids)
        await db.commit()
    return report
