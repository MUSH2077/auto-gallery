"""Reference data service — Danbooru artist lookup and import helpers."""

import asyncio
import logging
import re
import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.creator_link import CreatorLink
from app.models.source_creator import SourceCreator
from app.services import danbooru as danbooru_svc
from app.services.redis_client import get_redis
from app.services.queue_admission import checked_enqueue, ensure_redis_enqueue_capacity
from app.services.operations import set_operation_status

logger = logging.getLogger(__name__)


class ReferenceService:
    def __init__(self, db: AsyncSession = None):
        self.db = db

    async def get_danbooru_artist(self, artist_id: int) -> dict:
        artist = await asyncio.to_thread(danbooru_svc.get_artist, artist_id)
        if not artist:
            raise ValueError("Artist not found on Danbooru")
        pixiv_display_name = None
        from app.database import async_session
        pixiv_ids = []
        for u in artist.get("urls", []):
            raw = u.get("normalized_url") or u.get("url", "")
            m = re.search(r'pixiv\.net/(?:en/)?users/(\d+)', raw)
            if m:
                pixiv_ids.append(m.group(1))
        if pixiv_ids:
            async with async_session() as db:
                result = await db.execute(
                    select(SourceCreator).where(
                        SourceCreator.source == "pixiv",
                        SourceCreator.source_creator_id.in_(pixiv_ids),
                    ).limit(1))
                sc = result.scalar_one_or_none()
                if sc and sc.display_name:
                    pixiv_display_name = sc.display_name
        return {
            "artist": {
                "id": artist["id"],
                "name": artist["name"],
                "other_names": artist.get("other_names", []),
                "pixiv_display_name": pixiv_display_name,
            }
        }

    async def preview_danbooru(self, data: dict) -> dict:
        source_url = data.get("url") or data.get("source_url")
        pixiv_id = data.get("pixiv_id")
        artist_name = data.get("name") or data.get("tag")
        if not source_url and not pixiv_id and not artist_name:
            return {"status": "error", "message": "At least one of url, pixiv_id, or name is required"}

        artist, links = await asyncio.to_thread(
            danbooru_svc.search_and_extract,
            source_url=source_url, pixiv_id=pixiv_id, artist_name=artist_name,
        )
        if not artist:
            return {"status": "ok", "found": False, "message": "No matching Danbooru artist found"}

        pixiv_user_id = None
        if pixiv_id:
            pixiv_user_id = str(pixiv_id).strip()
        elif source_url:
            m = re.search(r'pixiv\.net/(?:en/)?users/(\d+)', source_url)
            if m:
                pixiv_user_id = m.group(1)
        if not pixiv_user_id:
            for u in artist.get("urls", []):
                raw = u.get("normalized_url") or u.get("url", "")
                m = re.search(r'pixiv\.net/(?:en/)?users/(\d+)', raw)
                if m:
                    pixiv_user_id = m.group(1)
                    break

        pixiv_display_name = None
        if pixiv_user_id:
            result = await self.db.execute(
                select(SourceCreator).where(
                    SourceCreator.source == "pixiv",
                    SourceCreator.source_creator_id == pixiv_user_id,
                ).limit(1))
            sc = result.scalar_one_or_none()
            if sc and sc.display_name:
                pixiv_display_name = sc.display_name

        return {
            "status": "ok", "found": True,
            "artist": {
                "id": artist["id"], "name": artist["name"],
                "other_names": artist.get("other_names", []),
                "post_count": artist.get("post_count"),
                "notes": artist.get("notes"),
                "is_active": artist.get("is_active"),
                "created_at": artist.get("created_at"),
                "urls": [{"url": u["url"], "normalized_url": u.get("normalized_url", u["url"]),
                          "is_active": u.get("is_active", True)} for u in artist.get("urls", [])],
                "pixiv_display_name": pixiv_display_name,
            },
            "suggested_links": links,
        }

    async def import_danbooru_links(self, creator_id: UUID, data: dict) -> dict:
        source_url = data.get("url") or data.get("source_url")
        pixiv_id = data.get("pixiv_id")
        artist_name = data.get("name") or data.get("tag")
        if not source_url and not pixiv_id and not artist_name:
            raise ValueError("At least one of url, pixiv_id, or name is required")

        artist, links = await asyncio.to_thread(
            danbooru_svc.search_and_extract,
            source_url=source_url, pixiv_id=pixiv_id, artist_name=artist_name,
        )
        if not artist or not links:
            return {"status": "ok", "imported": 0, "message": "No matching Danbooru artist found"}

        created = 0
        for link_data in links:
            self.db.add(CreatorLink(
                creator_id=creator_id, url=link_data["url"],
                link_type=link_data["link_type"], source=link_data["source"],
                confidence=link_data["confidence"], is_verified=link_data["is_verified"],
                notes=link_data.get("notes"),
            ))
            created += 1
        await self.db.commit()
        return {"status": "ok", "imported": created, "artist_name": artist["name"]}

    @staticmethod
    def enqueue_async_import(data: dict) -> dict:
        from rq import Queue
        source_url = data.get("url") or data.get("source_url")
        pixiv_id = data.get("pixiv_id")
        artist_name = data.get("name") or data.get("tag")
        if not source_url and not pixiv_id and not artist_name:
            raise ValueError("At least one of url, pixiv_id, or name is required")

        redis = get_redis()
        ensure_redis_enqueue_capacity(redis)
        job_id = str(uuid.uuid4())
        queue = Queue(name="imports", connection=redis)
        try:
            set_operation_status(job_id, "queued", "danbooru-import-all",
                                 progress={"phase": "queued", "label": "Queued Danbooru import"},
                                 meta={"name": artist_name, "pixiv_id": pixiv_id})
            rq_job = checked_enqueue(
                queue,
                "app.jobs.danbooru_import.run_import_all_danbooru",
                data,
                job_id,
                job_timeout=3600,
                result_ttl=7200,
            )
        except Exception as exc:
            try:
                set_operation_status(
                    job_id,
                    "failed",
                    "danbooru-import-all",
                    progress={"phase": "failed", "label": "Queue publication failed"},
                    error=str(exc),
                )
            except Exception:
                logger.warning(
                    "Unable to compensate Danbooru import queue failure job=%s",
                    job_id,
                    exc_info=True,
                )
            raise
        logger.info("Enqueued Danbooru import-all operation job_id=%s rq_job=%s", job_id, rq_job.id)
        return {"job_id": job_id, "status": "queued"}

    @staticmethod
    async def precheck_pixiv_ids(pixiv_ids: list[str]) -> dict:
        from app.database import async_session
        from app.models.source_creator import SourceCreator
        from app.models.creator import Creator
        from sqlalchemy import select as sa_select

        total = len(pixiv_ids)
        seen: set[str] = set()
        unique: list[str] = []
        duplicate_ids: list[str] = []
        for pid in pixiv_ids:
            if pid not in seen:
                seen.add(pid)
                unique.append(pid)
            else:
                duplicate_ids.append(pid)

        async with async_session() as db:
            result = await db.execute(
                sa_select(SourceCreator.source_creator_id, Creator.name, Creator.id)
                .join(Creator, Creator.id == SourceCreator.creator_id)
                .where(
                    SourceCreator.source == "pixiv",
                    SourceCreator.source_creator_id.in_(unique),
                )
            )
            existing_map = {row[0]: {"name": row[1], "creator_id": str(row[2])} for row in result}
            existing_ids = [pid for pid in unique if pid in existing_map]
            new_ids = [pid for pid in unique if pid not in existing_map]

        return {
            "total": total,
            "unique_count": len(unique),
            "duplicates_removed": len(duplicate_ids),
            "duplicate_ids": duplicate_ids,
            "unique_ids": unique,
            "already_exists": [
                {"pixiv_id": pid, "creator_name": existing_map[pid]["name"],
                 "creator_id": existing_map[pid]["creator_id"]}
                for pid in existing_ids
            ],
            "new_count": len(unique) - len(existing_ids),
            "existing_ids": existing_ids,
        }
