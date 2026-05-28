"""Meilisearch indexing and search service."""

import logging

from meilisearch_python_sdk import Client as MeiliClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Work, Creator, Tag, WorkTag, WorkSource, SourceCreator

logger = logging.getLogger(__name__)

WORKS_INDEX = "works"
CREATORS_INDEX = "creators"
TAGS_INDEX = "tags"

INDEX_SETTINGS = {
    WORKS_INDEX: {
        "searchableAttributes": ["title", "description", "creator_name"],
        "filterableAttributes": ["is_nsfw", "is_ai_generated", "source", "tags"],
        "sortableAttributes": ["posted_at", "created_at"],
    },
    CREATORS_INDEX: {
        "searchableAttributes": ["name", "display_name", "description"],
        "filterableAttributes": ["is_active"],
        "sortableAttributes": ["name", "created_at"],
    },
    TAGS_INDEX: {
        "searchableAttributes": ["normalized_name", "category"],
        "filterableAttributes": ["category"],
        "sortableAttributes": ["normalized_name"],
    },
}


def _client() -> MeiliClient:
    return MeiliClient(settings.meili_url, settings.meili_master_key)


def _ensure_indexes(client: MeiliClient):
    for uid, config in INDEX_SETTINGS.items():
        try:
            client.create_index(uid, {"primaryKey": "id"})
        except Exception:
            pass
        try:
            client.index(uid).update_settings(config)
        except Exception:
            pass


class SearchService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def search(self, query: str, offset: int = 0, limit: int = 20) -> dict:
        try:
            client = _client()
            result = client.index(WORKS_INDEX).search(query, offset=offset, limit=limit)
            # meilisearch_python_sdk returns a typed SearchResults object
            hits = getattr(result, 'hits', [])
            total = getattr(result, 'estimated_total_hits', 0) or getattr(result, 'estimatedTotalHits', 0)
            return {
                "results": list(hits) if hits else [],
                "total": total,
                "query": query,
            }
        except Exception as e:
            logger.warning("Meilisearch search failed: %s", e)
            return {"results": [], "total": 0, "query": query}

    async def index_work(self, work_id: str, title: str | None, description: str | None,
                          creator_name: str | None, is_nsfw: bool, source: str,
                          tags: list[str], posted_at: str | None, created_at: str,
                          thumbnail_asset_id: str | None = None, asset_count: int = 1,
                          is_ai_generated: bool = False):
        try:
            client = _client()
            _ensure_indexes(client)
            client.index(WORKS_INDEX).add_documents([{
                "id": work_id, "title": title or "",
                "description": (description or "")[:500],
                "creator_name": creator_name or "", "is_nsfw": is_nsfw,
                "is_ai_generated": is_ai_generated,
                "source": source, "tags": tags,
                "thumbnail_asset_id": thumbnail_asset_id,
                "asset_count": asset_count,
                "posted_at": posted_at, "created_at": created_at,
            }])
        except Exception as e:
            logger.warning("Failed to index work %s: %s", work_id, e)

    async def delete_work(self, work_id: str):
        """Remove a work from the Meilisearch index (e.g., when deleted from DB)."""
        try:
            client = _client()
            client.index(WORKS_INDEX).delete_document(work_id)
        except Exception as e:
            logger.debug("Failed to delete work %s from index: %s", work_id, e)

    async def delete_all_works(self):
        """Clear all works from the Meilisearch index."""
        try:
            client = _client()
            client.index(WORKS_INDEX).delete_all_documents()
            logger.info("Cleared all works from Meilisearch index")
        except Exception as e:
            logger.warning("Failed to clear works index: %s", e)

    async def _batch_index_works(self, docs: list[dict]):
        """Index multiple works in a single Meilisearch call."""
        if not docs:
            return
        try:
            client = _client()
            _ensure_indexes(client)
            client.index(WORKS_INDEX).add_documents(docs)
            logger.info("Batch-indexed %d works", len(docs))
        except Exception as e:
            logger.warning("Batch index failed: %s", e)

    async def reindex(self) -> dict:
        stats = {"works": 0, "creators": 0, "tags": 0}
        try:
            client = _client()
            _ensure_indexes(client)
        except Exception as e:
            return {"status": "error", "message": str(e)}

        # ── Bulk pre-fetch: avoid N+1 ──────────────────────────────────────────

        # work_id → [tag_names]  (single JOIN query)
        tags_rows = await self.db.execute(
            select(WorkTag.work_id, Tag.normalized_name).join(Tag, Tag.id == WorkTag.tag_id)
        )
        work_tags: dict[str, list[str]] = {}
        for wid, tname in tags_rows.all():
            work_tags.setdefault(str(wid), []).append(tname)

        # work_id → (source, creator_display_name)  (single outer-join query)
        sc_rows = await self.db.execute(
            select(WorkSource.work_id, WorkSource.source, SourceCreator.display_name).outerjoin(
                SourceCreator,
                (SourceCreator.source_creator_id == WorkSource.source_creator_id)
                & (SourceCreator.source == WorkSource.source),
            )
        )
        work_source: dict[str, str] = {}
        work_creator: dict[str, str] = {}
        for wid, src, cname in sc_rows.all():
            key = str(wid)
            if key not in work_source:
                work_source[key] = src
                if cname:
                    work_creator[key] = cname

        # ── Index works ────────────────────────────────────────────────────────
        works = await self.db.execute(
            select(Work).order_by(Work.created_at.desc()).limit(10000)
        )
        work_docs = []
        for w in works.scalars().all():
            wid = str(w.id)
            work_docs.append({
                "id": wid,
                "title": w.title or "",
                "description": (w.description or "")[:500],
                "creator_name": work_creator.get(wid) or "",
                "is_nsfw": w.is_nsfw,
                "is_ai_generated": w.is_ai_generated,
                "source": work_source.get(wid) or "unknown",
                "tags": work_tags.get(wid) or [],
                "thumbnail_asset_id": w.thumbnail_asset_id,
                "asset_count": getattr(w, "asset_count", 1),
                "posted_at": w.posted_at,
                "created_at": w.created_at.isoformat() if w.created_at else None,
            })
            stats["works"] += 1
        if work_docs:
            client.index(WORKS_INDEX).add_documents(work_docs)

        # Index creators
        creators = await self.db.execute(select(Creator).limit(10000))
        creator_docs = []
        for c in creators.scalars().all():
            creator_docs.append({
                "id": str(c.id), "name": c.name,
                "display_name": c.display_name or c.name,
                "description": (c.description or "")[:500],
                "is_active": c.is_active, "created_at": c.created_at.isoformat(),
            })
            stats["creators"] += 1
        if creator_docs:
            client.index(CREATORS_INDEX).add_documents(creator_docs)

        # Index tags
        tags = await self.db.execute(select(Tag).limit(10000))
        tag_docs = []
        for t in tags.scalars().all():
            tag_docs.append({
                "id": str(t.id), "normalized_name": t.normalized_name,
                "category": t.category or "general", "created_at": t.created_at.isoformat(),
            })
            stats["tags"] += 1
        if tag_docs:
            client.index(TAGS_INDEX).add_documents(tag_docs)

        logger.info("Reindexed: %d works, %d creators, %d tags",
                     stats["works"], stats["creators"], stats["tags"])
        return {
            "status": "ok",
            "message": f"Indexed {stats['works']} works, {stats['creators']} creators, {stats['tags']} tags",
            **stats,
        }
