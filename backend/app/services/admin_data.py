"""Reusable data-management operations used by sync and background APIs."""

from __future__ import annotations

import logging
import os
import shutil

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.system_setting import SystemSetting
from app.services.cache import invalidate_api_caches, invalidate_creator_subscription_caches
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

ENTITIES = {
    "works": [
        "work_source_tags", "work_tags", "asset_sources",
        "work_curation_states", "asset_storage_states",
        "assets", "work_sources", "works",
    ],
    "creators": [
        "import_jobs", "download_jobs", "subscription_sources", "subscriptions",
        "source_creators", "creator_links", "creator_curation_states", "creators",
    ],
    "downloads": ["import_jobs", "download_jobs"],
    "jobs": ["import_jobs", "download_jobs"],
    "tags": ["work_source_tags", "work_tags", "tags"],
    "library": [],
    "settings": [],
}


async def clear_entity_data(entity: str, db: AsyncSession) -> dict:
    """Clear one data area and return the legacy response shape."""
    if entity == "all":
        order = [
            "work_source_tags", "work_tags", "asset_sources",
            "curation_changes", "work_curation_states", "creator_curation_states",
            "asset_storage_states", "curation_commits", "assets", "work_sources",
            "works", "import_jobs", "download_jobs", "subscription_sources",
            "subscriptions", "source_creators", "creator_links", "creators", "tags",
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
        invalidate_api_caches("works", "creators", "subscriptions")
    elif entity == "creators":
        invalidate_creator_subscription_caches(include_works=True)
    elif entity in {"downloads", "jobs"}:
        invalidate_api_caches("subscriptions", "creators")
    elif entity == "tags":
        invalidate_api_caches("tags", "works")

    return {"status": "ok", "message": f"Cleared {entity}", "deleted": results}


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
