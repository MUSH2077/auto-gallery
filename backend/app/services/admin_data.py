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
    "creators": ["source_creators", "creator_links", "creator_curation_states", "creators"],
    "subscriptions": ["subscription_sources", "subscriptions"],
    "jobs": ["task_runs", "import_jobs", "download_jobs"],
    "tags": ["work_source_tags", "work_tags", "tags"],
    "settings": [],
}

CONFIRMATION_PHRASES = {
    "works": "DELETE-WORKS",
    "creators": "DELETE-CREATORS",
    "subscriptions": "DELETE-SUBSCRIPTIONS",
    "tags": "DELETE-TAGS",
    "jobs": "DELETE-JOBS",
    "settings": "RESET-SETTINGS",
    "all": "DELETE-ALL-DATA",
}


async def preview_clear_entity_data(entity: str, db: AsyncSession) -> dict:
    """Return a read-only, domain-scoped impact preview."""

    if entity != "all" and entity not in ENTITIES:
        raise ValueError(f"Unknown entity: {entity}")
    preview_tables = {
        "works": ["works", "work_sources", "assets", "asset_sources"],
        "creators": ["creators", "source_creators", "subscriptions", "subscription_sources", "download_jobs", "import_jobs"],
        "subscriptions": ["subscriptions", "subscription_sources", "download_jobs", "import_jobs"],
        "tags": ["tags", "work_tags", "work_source_tags"],
        "jobs": ["task_runs", "task_events", "download_jobs", "import_jobs"],
        "settings": ["system_settings"],
        "all": ["works", "creators", "subscriptions", "subscription_sources", "tags", "task_runs", "download_jobs", "import_jobs", "assets"],
    }[entity]
    counts: dict[str, int] = {}
    for table in preview_tables:
        counts[table] = int(
            (await db.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar_one()
        )
    return {
        "entity": entity,
        "confirmation_phrase": CONFIRMATION_PHRASES[entity],
        "counts": counts,
        "preserves_repository_sync_receipts": entity == "jobs",
        "deletes_media_files": entity in {"works", "all"},
    }


async def _clear_subscriptions(db: AsyncSession) -> dict[str, int]:
    """Delete subscription-owned operational rows before their parents."""

    results: dict[str, int] = {}
    # Scope operational cleanup to jobs that are actually owned by a
    # subscription/repository. Manual imports and unrelated downloads remain.
    task_result = await db.execute(
        text(
            """
            DELETE FROM task_runs
            WHERE (
                subject_type = 'download_job'
                AND subject_id IN (
                    SELECT id FROM download_jobs
                    WHERE subscription_id IS NOT NULL
                       OR subscription_source_id IS NOT NULL
                )
            ) OR (
                subject_type = 'import_job'
                AND subject_id IN (
                    SELECT ij.id
                    FROM import_jobs ij
                    JOIN download_jobs dj ON dj.id = ij.download_job_id
                    WHERE dj.subscription_id IS NOT NULL
                       OR dj.subscription_source_id IS NOT NULL
                )
            )
            """
        )
    )
    results["task_runs"] = task_result.rowcount or 0
    statements = {
        "import_jobs": """
            DELETE FROM import_jobs
            WHERE download_job_id IN (
                SELECT id FROM download_jobs
                WHERE subscription_id IS NOT NULL
                   OR subscription_source_id IS NOT NULL
            )
        """,
        "download_jobs": """
            DELETE FROM download_jobs
            WHERE subscription_id IS NOT NULL
               OR subscription_source_id IS NOT NULL
        """,
        "subscription_sources": "DELETE FROM subscription_sources",
        "subscriptions": "DELETE FROM subscriptions",
    }
    for table, statement in statements.items():
        result = await db.execute(text(statement))
        results[table] = result.rowcount or 0
    return results


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
        results["failed_jobs"] = await clear_failed_rq_jobs(db)
        invalidate_creator_subscription_caches(include_works=True)
        invalidate_api_caches("tags")
        return {"status": "ok", "message": "All data cleared", "deleted": results}

    tables = ENTITIES.get(entity)
    if tables is None:
        raise ValueError(f"Unknown entity: {entity}")

    if entity == "settings":
        deleted = await _reset_settings(db)
        invalidate_api_caches("admin")
        return {
            "status": "ok",
            "message": "All settings reset to defaults",
            "deleted": {"settings": deleted},
        }

    if entity in {"subscriptions", "creators"}:
        results = await _clear_subscriptions(db)
        if entity == "creators":
            results.update(await _delete_tables(tables, db))
    else:
        results = await _delete_tables(tables, db)
    if entity == "works":
        storage_result = await db.execute(text("DELETE FROM storage_artifacts"))
        results["storage_artifacts"] = storage_result.rowcount or 0
    await db.commit()

    if entity == "works":
        _clear_files([str(settings.download_root), str(settings.library_root)])
        results["files"] = "downloads + library cleared"
        await _clear_search_index(db, "works")
        invalidate_api_caches("works", "creators", "subscriptions")
    elif entity == "creators":
        invalidate_creator_subscription_caches(include_works=True)
    elif entity == "jobs":
        results["failed_rq_jobs"] = await clear_failed_rq_jobs(db)
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
        projection_work_ids: set[UUID] = set()
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
                projection_work_ids.add(work.id)
            except Exception:
                errors += 1
                logger.warning("Library rebuild: failed work %s source %s", work.id, ws.source, exc_info=True)

        stats["scanned"] += len(page)
        await ArtifactLedger(db).upsert_many(library_artifacts)
        if projection_work_ids:
            from app.services.search_projection_outbox import request_search_projection

            await request_search_projection(db, projection_work_ids)
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


async def clear_failed_rq_jobs(db: AsyncSession, *, dry_run: bool = False) -> int:
    """Export and remove failed RQ entries not referenced by active tasks.

    Queue names are discovered from Redis rather than assumed. The exported
    manifest makes this maintenance action auditable and repeatable.
    """
    from rq import Queue
    from rq.registry import FailedJobRegistry
    from app.models.task_run import TaskRun

    try:
        redis = get_redis()
        active_rq_ids = set(
            (
                await db.execute(
                    select(TaskRun.rq_job_id).where(
                        TaskRun.status.in_({"enqueued", "running", "paused", "recovering"}),
                        TaskRun.rq_job_id.is_not(None),
                    )
                )
            ).scalars()
        )
        queues = list(Queue.all(connection=redis))
        known_names = {queue.name for queue in queues}
        if "default" not in known_names:
            queues.append(Queue(connection=redis))
        total = 0
        manifest: list[dict[str, str]] = []
        for queue in queues:
            registry = FailedJobRegistry(queue=queue)
            for job_id in registry.get_job_ids():
                if job_id in active_rq_ids:
                    continue
                manifest.append({"queue": queue.name, "job_id": job_id})
        audit_dir = Path(settings.app_config_root) / "audits"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_path = audit_dir / f"rq-failed-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        audit_path.write_text(
            json.dumps({"dry_run": dry_run, "items": manifest}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if dry_run:
            return len(manifest)
        registries = {queue.name: FailedJobRegistry(queue=queue) for queue in queues}
        for item in manifest:
            registries[item["queue"]].remove(item["job_id"], delete_job=True)
            total += 1
        return total
    except Exception:
        logger.warning("Failed to clear Redis failed-job registries", exc_info=True)
        return 0
