"""Reusable data-management operations used by sync and background APIs."""

from __future__ import annotations

import json
import logging
import os
import shutil
import inspect
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.system_setting import SystemSetting
from app.services.cache import invalidate_api_caches, invalidate_creator_subscription_caches
from app.services.library_sync import (
    resolve_creator_directory,
    library_version,
    metadata_version,
    sync_thumbnails,
    write_metadata_json,
)
from app.services.media_assets import is_browser_playable_video, media_kind
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

ENTITIES = {
    "works": [
        "work_source_tags", "work_tags", "asset_sources",
        "asset_dedup_decisions", "asset_dedup_cases", "visual_asset_members",
        "visual_asset_groups", "asset_dedup_evidence", "asset_dedup_outbox",
        "asset_dedup_scans",
        "work_curation_states", "asset_storage_states",
        "assets", "work_sources", "works",
    ],
    "creators": [
        "subscription_sources", "subscriptions",
        "source_creators", "creator_links", "creator_curation_states", "creators",
    ],
    "jobs": ["task_runs", "import_jobs", "download_jobs"],
    "tags": ["work_source_tags", "work_tags", "tags"],
    "library": [],
    "settings": [],
}


async def clear_entity_data(entity: str, db: AsyncSession) -> dict:
    """Clear one data area and return the legacy response shape."""
    if entity == "all":
        # Clear self-referencing FKs on curation_commits before delete
        await db.execute(text(
            "UPDATE curation_commits SET parent_commit_id = NULL, reverts_commit_id = NULL"
        ))
        await db.flush()
        order = [
            "work_source_tags", "work_tags", "asset_sources",
            "asset_dedup_decisions", "asset_dedup_cases", "visual_asset_members",
            "visual_asset_groups", "asset_dedup_evidence", "asset_dedup_outbox",
            "asset_dedup_scans",
            "curation_changes", "work_curation_states", "creator_curation_states",
            "asset_storage_states", "curation_commits", "assets", "work_sources",
            "works", "storage_artifacts", "task_runs", "import_jobs", "download_jobs",
            "subscription_sources", "subscriptions", "source_creators",
            "creator_links", "creators", "tags",
        ]
        results = await _delete_tables(order, db)
        await db.commit()
        _clear_files([str(settings.download_root), str(settings.library_root)])
        results["files"] = "downloads + library cleared"
        await _clear_search_index(db, "all")
        results["failed_jobs"] = clear_failed_rq_jobs()
        invalidate_creator_subscription_caches(include_works=True)
        invalidate_api_caches("tags")
        return {"status": "ok", "message": "All data cleared", "deleted": results}

    tables = ENTITIES.get(entity)
    if tables is None:
        raise ValueError(f"Unknown entity: {entity}")

    if entity == "library":
        _clear_files([str(settings.library_root)])
        invalidate_api_caches("works", "creators")
        return {
            "status": "ok",
            "message": "Library metadata and thumbnails cleared (media files untouched)",
            "deleted": {"library_files": "cleared"},
        }

    if entity == "settings":
        deleted = await _reset_settings(db)
        invalidate_api_caches("admin")
        return {
            "status": "ok",
            "message": "All settings reset to defaults",
            "deleted": {"settings": deleted},
        }

    results = await _delete_tables(tables, db)
    await db.commit()

    if entity == "works":
        _clear_files([str(settings.download_root), str(settings.library_root)])
        results["files"] = "downloads + library cleared"
        await _clear_search_index(db, "works")
        # Also clear storage_artifacts
        await db.execute(text("DELETE FROM storage_artifacts"))
        invalidate_api_caches("works", "creators", "subscriptions")
    elif entity == "creators":
        invalidate_creator_subscription_caches(include_works=True)
    elif entity == "jobs":
        invalidate_api_caches("subscriptions", "creators")
    elif entity == "tags":
        invalidate_api_caches("tags", "works")

    return {"status": "ok", "message": f"Cleared {entity}", "deleted": results}


async def rebuild_library_index(db: AsyncSession, options: dict | None = None, progress_callback=None) -> dict:
    """Incrementally repair /library/ with bounded keyset-pagination batches."""
    from app.models import Work, WorkSource, Asset, AssetSource, SourceCreator
    from app.services.settings import load_gallerydl_config
    options = options or {}
    mode = options.get("mode", "repair")
    if mode not in {"repair", "full"}:
        raise ValueError("mode must be 'repair' or 'full'")
    batch_size = min(max(int(options.get("batch_size", 500)), 10), 2000)
    source_filter = options.get("source")
    work_filter = UUID(str(options["work_id"])) if options.get("work_id") else None
    creator_filter = UUID(str(options["creator_id"])) if options.get("creator_id") else None

    filters = []
    if work_filter:
        filters.append(Work.id == work_filter)
    if source_filter:
        filters.append(Work.work_sources.any(WorkSource.source == source_filter))
    if creator_filter:
        filters.append(Work.work_sources.any(
            WorkSource.source_creator_id.in_(
                select(SourceCreator.source_creator_id).where(SourceCreator.creator_id == creator_filter)
            )
        ))

    id_query = select(Work.id, Work.created_at).where(*filters)
    total = (await db.execute(select(func.count()).select_from(id_query.subquery()))).scalar_one()
    if total == 0:
        return {"status": "ok", "message": "No works to rebuild", "scanned": 0, "skipped": 0,
                "metadata_written": 0, "thumbnails_generated": 0, "errors": 0, "cursor": None}

    r = get_redis()
    progress_key = "library:rebuild:progress"
    checkpoint_key = "library:rebuild:checkpoint"
    cursor_created_at = None
    cursor_id = None
    resumed_stats = None
    resumed_errors = 0
    if options.get("resume", True):
        raw_checkpoint = r.get(checkpoint_key)
        if raw_checkpoint:
            if isinstance(raw_checkpoint, bytes):
                raw_checkpoint = raw_checkpoint.decode()
            checkpoint = json.loads(raw_checkpoint)
            if checkpoint.get("options") == {k: options.get(k) for k in ("mode", "source", "creator_id", "work_id")}:
                cursor_created_at = datetime.fromisoformat(checkpoint["created_at"])
                cursor_id = UUID(checkpoint["id"])
                resumed_stats = checkpoint.get("stats")
                resumed_errors = int(checkpoint.get("errors", 0))

    from app.services.artifact_ledger import ArtifactLedger, managed_artifact_row
    file_index = None
    config = load_gallerydl_config()
    stats = resumed_stats or {"scanned": 0, "skipped": 0, "metadata_written": 0, "thumbnails_generated": 0}
    errors = resumed_errors
    while True:
        page_query = id_query
        if cursor_created_at is not None:
            page_query = page_query.where(or_(
                Work.created_at > cursor_created_at,
                and_(Work.created_at == cursor_created_at, Work.id > cursor_id),
            ))
        page = (await db.execute(page_query.order_by(Work.created_at, Work.id).limit(batch_size))).all()
        if not page:
            break
        work_ids = [row.id for row in page]
        source_conditions = [Work.id.in_(work_ids)]
        if source_filter:
            source_conditions.append(WorkSource.source == source_filter)
        if creator_filter:
            source_conditions.append(SourceCreator.creator_id == creator_filter)
        source_rows = (await db.execute(
            select(Work, WorkSource, SourceCreator)
            .join(WorkSource, WorkSource.work_id == Work.id)
            .outerjoin(SourceCreator, and_(SourceCreator.source == WorkSource.source,
                                          SourceCreator.source_creator_id == WorkSource.source_creator_id))
            .where(*source_conditions)
            .order_by(Work.created_at, Work.id, WorkSource.id)
        )).all()
        asset_rows = (await db.execute(
            select(WorkSource.id, Asset, AssetSource)
            .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .join(Asset, Asset.id == AssetSource.asset_id)
            .where(WorkSource.work_id.in_(work_ids))
            .order_by(WorkSource.id, Asset.file_name)
        )).all()
        assets_by_source = {}
        library_artifacts = []
        for ws_id, asset, asset_source in asset_rows:
            assets_by_source.setdefault(ws_id, []).append((asset, asset_source))

        for work, ws, creator in source_rows:
            rows = assets_by_source.get(ws.id, [])
            if not rows:
                stats["skipped"] += 1
                continue
            try:
                creator_dir = resolve_creator_directory(ws.source, ws.raw_metadata, ws.source_work_id, config)
                lib_dir = Path(settings.library_root) / ws.source / creator_dir / ws.source_work_id
                version = library_version(work, ws, rows)
                expected_thumbs = []
                media_projection_complete = True
                for asset, _ in rows:
                    asset_kind = media_kind(asset.mime_type, asset.file_name)
                    stem = Path(asset.file_path).stem
                    if asset_kind in {"image", "animated_image"}:
                        expected_thumbs.append(lib_dir / f"{stem}.thumbnail.webp")
                    elif is_browser_playable_video(asset.mime_type, asset.file_name):
                        expected_thumbs.extend(
                            (
                                lib_dir / f"{stem}.thumbnail.webp",
                                lib_dir / f"{stem}.poster.webp",
                            )
                        )
                        media_projection_complete = media_projection_complete and all(
                            value is not None
                            for value in (
                                asset.width,
                                asset.height,
                                asset.duration,
                                asset.thumb_sm_path,
                                asset.thumb_lg_path,
                            )
                        )
                if (
                    mode == "repair"
                    and metadata_version(lib_dir) == version
                    and media_projection_complete
                    and all(path.exists() for path in expected_thumbs)
                ):
                    stats["skipped"] += 1
                    continue
                generated = await sync_thumbnails(
                    rows, lib_dir, file_index, ws.source, creator_dir, ws.source_work_id,
                    force=mode == "full")
                assets_meta = [{"file_name": asset.file_name} for asset, _ in rows]
                write_metadata_json(lib_dir, work, ws,
                                    creator.display_name if creator and creator.display_name else creator_dir,
                                    assets_meta, version=version)
                managed_paths = [
                    lib_dir / "metadata.json",
                    *lib_dir.glob("*.thumbnail.webp"),
                    *lib_dir.glob("*.poster.webp"),
                ]
                library_artifacts.extend(
                    managed_artifact_row(path, Path(settings.library_root), "library",
                                         ws.source, creator_dir, ws.source_work_id,
                                         (
                                             "metadata_json"
                                             if path.name == "metadata.json"
                                             else "video_poster"
                                             if path.name.endswith(".poster.webp")
                                             else "thumbnail"
                                         ))
                    for path in managed_paths if path.exists()
                )
                stats["metadata_written"] += 1
                stats["thumbnails_generated"] += generated
            except Exception:
                errors += 1
                logger.warning("Library rebuild: failed work %s source %s", work.id, ws.source, exc_info=True)

        stats["scanned"] += len(page)
        await ArtifactLedger(db).upsert_many(library_artifacts)
        await db.commit()
        cursor_created_at, cursor_id = page[-1].created_at, page[-1].id
        cursor = {"created_at": cursor_created_at.isoformat(), "id": str(cursor_id)}
        checkpoint = {**cursor, "options": {k: options.get(k) for k in ("mode", "source", "creator_id", "work_id")},
                      "stats": stats, "errors": errors}
        r.setex(checkpoint_key, 86400, json.dumps(checkpoint))
        progress = {**stats, "errors": errors, "total": total, "cursor": cursor, "phase": "running"}
        r.setex(progress_key, 3600, json.dumps(progress))
        if progress_callback:
            callback_result = progress_callback(progress)
            if inspect.isawaitable(callback_result):
                await callback_result
        db.expunge_all()

    r.delete(checkpoint_key)
    result = {"status": "ok", **stats, "errors": errors, "cursor": None,
              "message": f"Scanned {stats['scanned']} sources, wrote {stats['metadata_written']} metadata files ({errors} errors)"}
    r.setex(progress_key, 3600, json.dumps({**result, "status": "done"}))
    return result


async def _delete_tables(tables: list[str], db: AsyncSession) -> dict[str, int]:
    results = {}
    for table in tables:
        r = await db.execute(text(f"DELETE FROM {table}"))
        results[table] = r.rowcount or 0
    return results


async def _reset_settings(db: AsyncSession) -> int:
    result = await db.execute(select(SystemSetting))
    rows = result.scalars().all()
    for row in rows:
        await db.delete(row)
    await db.commit()
    return len(rows)


async def _clear_search_index(db: AsyncSession, entity: str) -> None:
    try:
        from app.services.search import SearchService

        svc = SearchService(db)
        await svc.delete_all_works()
        logger.info("Cleared Meilisearch indexes after %r clear", entity)
    except Exception:
        logger.warning("Failed to clear Meilisearch after %r clear", entity, exc_info=True)


def _clear_files(paths: list[str]) -> None:
    """Remove all files in given directories while preserving root dirs."""
    for path in paths:
        try:
            for item in os.listdir(path):
                item_path = os.path.join(path, item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                else:
                    os.unlink(item_path)
        except Exception:
            logger.warning("Failed to clear files in %s", path, exc_info=True)


def clear_failed_rq_jobs() -> int:
    """Remove failed RQ jobs across all queues; best effort for clear-all."""
    from rq import Queue
    from rq.registry import FailedJobRegistry

    try:
        redis = get_redis()
        total = 0
        for qname in ("default", "downloads", "imports", "scheduled"):
            queue = Queue(connection=redis) if qname == "default" else Queue(name=qname, connection=redis)
            registry = FailedJobRegistry(queue=queue)
            for job_id in registry.get_job_ids():
                registry.remove(job_id, delete_job=True)
                total += 1
        return total
    except Exception:
        logger.warning("Failed to clear Redis failed-job registries", exc_info=True)
        return 0
