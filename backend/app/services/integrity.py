"""Library integrity diagnostics shared by background operations."""
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings

logger = logging.getLogger(__name__)

async def scan_integrity(db: AsyncSession):
    """Scan for data integrity issues: orphaned files, missing thumbnails, orphaned records."""
    from sqlalchemy import text
    issues = []

    # 1. Orphaned download files (files without work_sources)
    try:
        dl_root = Path(settings.download_root)
        result = await db.execute(text("SELECT source, source_work_id FROM work_sources"))
        db_work_ids = {(row[0], row[1]) for row in result.fetchall()}

        def _scan_orphans() -> list:
            """Blocking full-library walk — offloaded off the event loop."""
            found = []
            for source_dir in dl_root.iterdir():
                if not source_dir.is_dir():
                    continue
                src = source_dir.name
                for creator_dir in source_dir.iterdir():
                    if not creator_dir.is_dir():
                        continue
                    for work_dir in creator_dir.iterdir():
                        if not work_dir.is_dir():
                            continue
                        swid = work_dir.name
                        if (src, swid) not in db_work_ids:
                            file_count = sum(1 for _ in work_dir.rglob("*") if _.is_file())
                            found.append({
                                "path": str(work_dir.relative_to(dl_root)),
                                "source": src,
                                "source_work_id": swid,
                                "file_count": file_count,
                            })
            return found

        orphaned_files = await asyncio.to_thread(_scan_orphans)
        if orphaned_files:
            issues.append({
                "type": "orphaned_download_files",
                "severity": "warning",
                "count": len(orphaned_files),
                "description": "下载目录中存在但数据库无对应 work_source 记录的文件",
                "items": orphaned_files[:50],
            })
    except Exception:
        await db.rollback()
        logger.exception("Integrity check failed while scanning orphaned files")
        raise

    # 2. Missing thumbnails (works with asset but no thumbnail)
    try:
        result = await db.execute(text(
            "SELECT DISTINCT ON (a.id) "
            "a.id, a.file_name, ws.source, ws.source_work_id FROM assets a "
            "JOIN asset_sources ars ON ars.asset_id = a.id "
            "JOIN work_sources ws ON ws.id = ars.work_source_id "
            "WHERE a.thumb_sm_path IS NULL OR a.thumb_sm_path = '' "
            "ORDER BY a.id, ws.source, ws.source_work_id"
        ))
        missing_thumbs = []
        for row in result.fetchall():
            missing_thumbs.append({
                "asset_id": str(row[0]),
                "file_name": row[1],
                "source": row[2],
                "source_work_id": row[3],
            })
        if missing_thumbs:
            issues.append({
                "type": "missing_thumbnails",
                "severity": "warning",
                "count": len(missing_thumbs),
                "description": "有资产记录但缺少缩略图的作品",
                "items": missing_thumbs[:50],
            })
    except Exception:
        await db.rollback()
        logger.exception("Integrity check failed while scanning missing thumbnails")
        raise

    # 3. Orphaned creators (no subscriptions and no source identities). Works
    # are linked to creators through source_creators -> work_sources in the
    # current schema, so a creator without a source identity cannot own one.
    try:
        result = await db.execute(text(
            "SELECT c.id, c.name FROM creators c "
            "LEFT JOIN subscriptions s ON s.creator_id = c.id "
            "LEFT JOIN source_creators sc ON sc.creator_id = c.id "
            "WHERE s.id IS NULL AND sc.id IS NULL"
        ))
        orphaned_creators = [{"id": str(row[0]), "name": row[1]} for row in result.fetchall()]
        if orphaned_creators:
            issues.append({
                "type": "orphaned_creators",
                "severity": "info",
                "count": len(orphaned_creators),
                "description": "无作品、无订阅、无来源账号的孤立创作者",
                "items": orphaned_creators,
            })
    except Exception:
        await db.rollback()
        logger.exception("Integrity check failed while scanning orphaned creators")
        raise

    # 4. Orphaned tags (no work_tags associations)
    try:
        result = await db.execute(text(
            "SELECT t.id, t.normalized_name FROM tags t "
            "LEFT JOIN work_tags wt ON wt.tag_id = t.id "
            "WHERE wt.tag_id IS NULL"
        ))
        orphaned_tags = [{"id": str(row[0]), "name": row[1]} for row in result.fetchall()]
        if orphaned_tags:
            issues.append({
                "type": "orphaned_tags",
                "severity": "info",
                "count": len(orphaned_tags),
                "description": "无关联作品的孤立标签",
                "items": orphaned_tags,
            })
    except Exception:
        await db.rollback()
        logger.exception("Integrity check failed while scanning orphaned tags")
        raise

    # 5. Dead links (asset records where file doesn't exist on disk)
    try:
        # Keyset-paged: fetchall over assets is O(rows) resident memory — an
        # OOM vector on a large library. Batches keep memory O(batch).
        dead_links: list[dict] = []
        _DEAD_LINK_BATCH = 5000
        _DEAD_LINK_MAX_ITEMS = 500  # cap the payload; count keeps going
        dead_total = 0
        last_id = None
        while True:
            q = ("SELECT a.id, a.file_path, a.file_name FROM assets a "
                 "WHERE a.file_path IS NOT NULL ")
            params: dict = {"lim": _DEAD_LINK_BATCH}
            if last_id is not None:
                q += "AND a.id > :last_id "
                params["last_id"] = last_id
            q += "ORDER BY a.id LIMIT :lim"
            batch = (await db.execute(text(q), params)).fetchall()
            if not batch:
                break
            last_id = batch[-1][0]

            def _check_batch(rows=batch) -> list:
                found = []
                for row in rows:
                    fpath = row[1]
                    candidate = Path(fpath) if fpath else None
                    if candidate is not None and not candidate.is_absolute():
                        candidate = Path(settings.download_root) / candidate
                    if candidate is not None and not candidate.exists():
                        found.append({
                            "asset_id": str(row[0]),
                            "file_path": fpath,
                            "file_name": row[2],
                        })
                return found

            batch_dead = await asyncio.to_thread(_check_batch)
            dead_total += len(batch_dead)
            if len(dead_links) < _DEAD_LINK_MAX_ITEMS:
                dead_links.extend(batch_dead[: _DEAD_LINK_MAX_ITEMS - len(dead_links)])
        if dead_total:
            issues.append({
                "type": "dead_links",
                "severity": "error",
                "count": dead_total,
                "description": "数据库记录指向不存在文件的死链",
                "items": dead_links[:50],
            })
    except Exception:
        await db.rollback()
        logger.exception("Integrity check failed while scanning dead links")
        raise

    # 6. DB table stats
    try:
        tables = ["works", "assets", "creators", "subscriptions", "tags",
                   "download_jobs", "import_jobs", "source_creators", "work_sources"]
        db_stats = {}
        for table in tables:
            r = await db.execute(text(f"SELECT COUNT(*) FROM {table}"))
            db_stats[table] = r.scalar() or 0
    except Exception:
        await db.rollback()
        logger.exception("Integrity check failed while collecting database statistics")
        raise

    return {"issues": issues, "db_stats": db_stats, "checked_at": datetime.now(timezone.utc).isoformat()}
