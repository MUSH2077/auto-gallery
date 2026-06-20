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


async def rebuild_library_index(db: AsyncSession) -> dict:
    """Regenerate all /library/ metadata.json + thumbnails from DB records."""
    from pathlib import Path
    import json, os, asyncio
    from sqlalchemy import select
    from app.config import settings
    from app.models import Work, Asset, WorkSource, AssetSource, SourceCreator
    from app.services.file_index import FileIndex
    from app.services.thumbnail import generate_thumbnail
    from app.services.settings import load_gallerydl_config, extractor_key_for_source
    from app.services.redis_client import get_redis

    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

    result = await db.execute(select(Work).order_by(Work.created_at.desc()))
    all_works = result.scalars().all()
    total = len(all_works)
    if total == 0:
        return {"status": "ok", "message": "No works to rebuild", "rebuilt": 0, "errors": 0}

    r = get_redis()
    progress_key = "library:rebuild:progress"
    r.setex(progress_key, 3600, json.dumps({"current": 0, "total": total, "status": "running"}))

    file_index = FileIndex(os.path.join(str(settings.download_root), ".file-index.sqlite3"))
    config = load_gallerydl_config()
    rebuilt = 0
    errors = 0

    for idx, work in enumerate(all_works):
        try:
            ws_result = await db.execute(select(WorkSource).where(WorkSource.work_id == work.id))
            ws = ws_result.scalar_one_or_none()
            if not ws:
                continue

            as_result = await db.execute(
                select(Asset, AssetSource).join(AssetSource, AssetSource.asset_id == Asset.id)
                .where(AssetSource.work_source_id == ws.id).order_by(Asset.file_name)
            )
            asset_rows = as_result.all()
            if not asset_rows:
                continue

            _ek = extractor_key_for_source(ws.source)
            _ec = config.get("extractor", {}).get(_ek, {})
            _dt = _ec.get("directory", [ws.source, "{id}"])
            _rparts = []
            for part in _dt:
                rp = part
                rm = ws.raw_metadata or {}
                if isinstance(rm, dict):
                    for k, v in rm.items():
                        if isinstance(v, dict):
                            for sk, sv in v.items():
                                rp = rp.replace(f"{{user[{sk}]}}", str(sv) if sv else "")
                        elif isinstance(v, (str, int, float)):
                            rp = rp.replace(f"{{{k}}}", str(v) if v is not None else "")
                rp = rp.replace("{id}", ws.source_work_id)
                _rparts.append(rp.strip().replace("/", "_"))
            creator_dir = _rparts[1] if len(_rparts) > 1 else ws.source_work_id

            lib_dir = Path(settings.library_root) / ws.source / creator_dir / ws.source_work_id
            lib_dir.mkdir(parents=True, exist_ok=True)

            sc_result = await db.execute(
                select(SourceCreator).where(
                    SourceCreator.source_creator_id == ws.source_creator_id,
                    SourceCreator.source == ws.source,
                )
            ) if ws.source_creator_id else None
            sc = sc_result.scalar_one_or_none() if sc_result else None
            display_name = sc.display_name if sc else creator_dir

            assets_meta = []
            for asset, _as in asset_rows:
                assets_meta.append({"file_name": asset.file_name})
                fp = Path(settings.download_root) / asset.file_path
                if fp.exists() and fp.suffix.lower() in IMAGE_EXTS:
                    tp = await asyncio.to_thread(generate_thumbnail, str(fp), lib_dir, f"{fp.stem}.thumbnail")
                    if tp:
                        rel = str(Path(tp).relative_to(settings.library_root))
                        file_index.upsert(file_path=rel, storage_root="library", source=ws.source,
                                          creator_dir=creator_dir, work_id=ws.source_work_id,
                                          file_name=Path(tp).name, file_type="thumbnail",
                                          file_size=Path(tp).stat().st_size)

            with open(lib_dir / "metadata.json", "w") as mf:
                json.dump({
                    "work_id": str(work.id), "source": ws.source,
                    "source_work_id": ws.source_work_id, "title": work.title,
                    "posted_at": work.posted_at.isoformat() if work.posted_at else None,
                    "creator": display_name, "assets": assets_meta,
                }, mf, indent=2, ensure_ascii=False, default=str)

            file_index.upsert(
                file_path=str(Path(ws.source) / creator_dir / ws.source_work_id / "metadata.json"),
                storage_root="library", source=ws.source, creator_dir=creator_dir,
                work_id=ws.source_work_id, file_name="metadata.json",
                file_type="metadata_json", file_size=(lib_dir / "metadata.json").stat().st_size,
                import_status="done",
            )
            rebuilt += 1
        except Exception:
            errors += 1
            logger.warning("Library rebuild: failed work %s", work.id, exc_info=True)

        if idx % 50 == 0:
            r.setex(progress_key, 3600, json.dumps({"current": idx + 1, "total": total, "status": "running"}))

    r.setex(progress_key, 3600, json.dumps({"current": total, "total": total, "status": "done", "rebuilt": rebuilt, "errors": errors}))
    return {"status": "ok", "message": f"Rebuilt {rebuilt} works ({errors} errors)", "rebuilt": rebuilt, "errors": errors}


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
