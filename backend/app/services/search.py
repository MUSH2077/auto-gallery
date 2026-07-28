"""Meilisearch indexing and search service."""

import asyncio
import logging

from meilisearch_python_sdk import Client as MeiliClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Work, Creator, Tag, WorkTag, WorkSource, SourceCreator, WorkCurationState

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
            client.create_index(uid, primary_key="id")
        except Exception:
            pass
        try:
            # A rebuilt index can exist without a primary key (docs like works
            # carry both id and creator_id, so Meilisearch refuses to guess —
            # index_primary_key_multiple_candidates_found). Pin it explicitly.
            index = client.get_index(uid)
            if index.primary_key is None:
                index.update(primary_key="id")
        except Exception:
            pass
        try:
            client.index(uid).update_settings(config)
        except Exception:
            pass


class SearchService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def search(self, query: str, offset: int = 0, limit: int = 20, kind: str = "all",
                      force_sfw: bool = False) -> dict:
        # The meilisearch SDK client is synchronous — run the HTTP round-trips in
        # a thread so this hot endpoint never blocks the single async event loop.
        def _run() -> dict:
            client = _client()
            result = {"query": query, "total": 0, "results": [], "creators": [], "tags": []}

            # Works — searched for 'all' and 'works'
            if kind in ("all", "works"):
                works_result = client.index(WORKS_INDEX).search(
                    query, offset=offset, limit=limit,
                    filter="is_nsfw = false" if force_sfw else None,
                )
                works_hits = getattr(works_result, 'hits', []) or []
                result["results"] = list(works_hits)
                result["total"] = getattr(works_result, 'estimated_total_hits', 0) or 0

            # Creators — searched for 'all' and 'creators'
            if kind in ("all", "creators"):
                try:
                    cr = client.index(CREATORS_INDEX).search(query, limit=10 if kind == "all" else limit)
                    result["creators"] = list(getattr(cr, 'hits', []) or [])
                    if kind == "creators":
                        result["total"] = getattr(cr, 'estimated_total_hits', 0) or 0
                except Exception:
                    pass

            # Tags — searched for 'all' and 'tags'
            if kind in ("all", "tags"):
                try:
                    tr = client.index(TAGS_INDEX).search(query, limit=10 if kind == "all" else limit)
                    result["tags"] = list(getattr(tr, 'hits', []) or [])
                    if kind == "tags":
                        result["total"] = getattr(tr, 'estimated_total_hits', 0) or 0
                except Exception:
                    pass

            return result

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            logger.warning("Meilisearch search failed: %s", e)
            return {"results": [], "total": 0, "query": query, "creators": [], "tags": []}

    async def index_work(self, work_id: str, title: str | None, description: str | None,
                          creator_name: str | None, is_nsfw: bool, source: str,
                          tags: list[str], posted_at: str | None, created_at: str,
                          thumbnail_asset_id: str | None = None, asset_count: int = 1,
                          is_ai_generated: bool = False, creator_id: str | None = None):
        try:
            def _run():
                client = _client()
                _ensure_indexes(client)
                client.index(WORKS_INDEX).add_documents([{
                    "id": work_id, "title": title or "",
                    "description": (description or "")[:500],
                    "creator_name": creator_name or "", "creator_id": creator_id or "",
                    "is_nsfw": is_nsfw, "is_ai_generated": is_ai_generated,
                    "source": source, "tags": tags,
                    "thumbnail_asset_id": thumbnail_asset_id,
                    "asset_count": asset_count,
                    "posted_at": posted_at, "created_at": created_at,
                }])
            await asyncio.to_thread(_run)
        except Exception as e:
            logger.warning("Failed to index work %s: %s", work_id, e)

    async def delete_work(self, work_id: str):
        """Remove a work from the Meilisearch index (e.g., when deleted from DB)."""
        try:
            await asyncio.to_thread(lambda: _client().index(WORKS_INDEX).delete_document(work_id))
        except Exception as e:
            logger.debug("Failed to delete work %s from index: %s", work_id, e)

    async def delete_all_works(self):
        """Clear all works from the Meilisearch index."""
        try:
            await asyncio.to_thread(lambda: _client().index(WORKS_INDEX).delete_all_documents())
            logger.info("Cleared all works from Meilisearch index")
        except Exception as e:
            logger.warning("Failed to clear works index: %s", e)

    async def index_creator(self, creator_id: str, name: str, display_name: str | None,
                            description: str | None, is_active: bool, created_at: str):
        try:
            def _run():
                client = _client()
                _ensure_indexes(client)
                client.index(CREATORS_INDEX).add_documents([{
                    "id": creator_id, "name": name,
                    "display_name": display_name or name,
                    "description": (description or "")[:500],
                    "is_active": is_active, "created_at": created_at,
                }])
            await asyncio.to_thread(_run)
        except Exception as e:
            logger.warning("Failed to index creator %s: %s", creator_id, e)

    async def index_tag(self, tag_id: str, normalized_name: str, category: str | None, created_at: str):
        try:
            def _run():
                client = _client()
                _ensure_indexes(client)
                client.index(TAGS_INDEX).add_documents([{
                    "id": tag_id, "normalized_name": normalized_name,
                    "category": category or "general", "created_at": created_at,
                }])
            await asyncio.to_thread(_run)
        except Exception as e:
            logger.warning("Failed to index tag %s: %s", tag_id, e)

    async def delete_creator(self, creator_id: str):
        try:
            await asyncio.to_thread(lambda: _client().index(CREATORS_INDEX).delete_document(creator_id))
        except Exception as e:
            logger.debug("Failed to delete creator %s from index: %s", creator_id, e)

    async def delete_tag(self, tag_id: str):
        try:
            await asyncio.to_thread(lambda: _client().index(TAGS_INDEX).delete_document(tag_id))
        except Exception as e:
            logger.debug("Failed to delete tag %s from index: %s", tag_id, e)

    async def _batch_index_works(self, docs: list[dict]):
        """Index multiple works in a single Meilisearch call."""
        if not docs:
            return
        try:
            def _run():
                client = _client()
                _ensure_indexes(client)
                client.index(WORKS_INDEX).add_documents(docs)
            await asyncio.to_thread(_run)
            logger.info("Batch-indexed %d works", len(docs))
        except Exception as e:
            logger.warning("Batch index failed: %s", e)

    async def _reindex_batch_meta(self, work_ids: list):
        """Tags + source/creator for one batch of works. Scoped to work_ids so
        reindex memory is O(batch), not O(library)."""
        work_tags: dict[str, list[str]] = {}
        work_source: dict[str, str] = {}
        work_creator: dict[str, str] = {}
        work_creator_id: dict[str, str] = {}
        if not work_ids:
            return work_tags, work_source, work_creator, work_creator_id

        tags_rows = await self.db.execute(
            select(WorkTag.work_id, Tag.normalized_name)
            .join(Tag, Tag.id == WorkTag.tag_id)
            .where(WorkTag.work_id.in_(work_ids))
        )
        for wid, tname in tags_rows.all():
            work_tags.setdefault(str(wid), []).append(tname)

        sc_rows = await self.db.execute(
            select(WorkSource.work_id, WorkSource.source, SourceCreator.display_name,
                   SourceCreator.creator_id)
            .outerjoin(
                SourceCreator,
                (SourceCreator.source_creator_id == WorkSource.source_creator_id)
                & (SourceCreator.source == WorkSource.source),
            )
            .where(WorkSource.work_id.in_(work_ids))
        )
        for wid, src, cname, cid in sc_rows.all():
            key = str(wid)
            if key not in work_source:
                work_source[key] = src
                if cname:
                    work_creator[key] = cname
                if cid:
                    work_creator_id[key] = str(cid)
        return work_tags, work_source, work_creator, work_creator_id

    async def reindex(self) -> dict:
        BATCH_SIZE = 500
        stats = {"works": 0, "creators": 0, "tags": 0}
        try:
            client = _client()
            _ensure_indexes(client)
        except Exception as e:
            return {"status": "error", "message": str(e)}

        # Tag/source lookups are fetched per batch (see _reindex_batch_meta),
        # not pre-loaded for the whole library — the old full-library dicts were
        # O(works) resident memory and could OOM the worker on a large DB.

        # Clear existing indexes before reindex. reindex is called inline from
        # the admin endpoint, so every synchronous meili call is offloaded to a
        # thread — otherwise a full rebuild freezes the whole event loop.
        def _clear_indexes():
            client.index(WORKS_INDEX).delete_all_documents()
            client.index(CREATORS_INDEX).delete_all_documents()
            client.index(TAGS_INDEX).delete_all_documents()
        try:
            await asyncio.to_thread(_clear_indexes)
        except Exception:
            pass

        # ── Index works (paginated) ──────────────────────────────────────
        work_offset = 0
        while True:
            rows = await self.db.execute(
                select(Work)
                .outerjoin(WorkCurationState, WorkCurationState.work_id == Work.id)
                .where((WorkCurationState.visibility.is_(None)) | (WorkCurationState.visibility == "visible"))
                .order_by(Work.created_at.desc())
                .offset(work_offset).limit(BATCH_SIZE)
            )
            batch = rows.scalars().all()
            if not batch:
                break
            batch_ids = [w.id for w in batch]
            work_tags, work_source, work_creator, work_creator_id = \
                await self._reindex_batch_meta(batch_ids)
            work_docs = []
            for w in batch:
                wid = str(w.id)
                work_docs.append({
                    "id": wid, "title": w.title or "",
                    "description": (w.description or "")[:500],
                    "creator_name": work_creator.get(wid) or "",
                    "creator_id": work_creator_id.get(wid) or "",
                    "is_nsfw": w.is_nsfw, "is_ai_generated": w.is_ai_generated,
                    "source": work_source.get(wid) or "unknown",
                    "tags": work_tags.get(wid) or [],
                    "thumbnail_asset_id": str(w.thumbnail_asset_id) if w.thumbnail_asset_id else None,
                    "asset_count": getattr(w, "asset_count", 1),
                    "posted_at": w.posted_at.isoformat() if w.posted_at else None,
                    "created_at": w.created_at.isoformat() if w.created_at else None,
                })
                stats["works"] += 1
            await asyncio.to_thread(lambda: client.index(WORKS_INDEX).add_documents(work_docs))
            logger.info("Indexed %d works (offset=%d)", len(work_docs), work_offset)
            work_offset += BATCH_SIZE

        # ── Index creators (paginated) ───────────────────────────────────
        creator_offset = 0
        while True:
            rows = await self.db.execute(
                select(Creator).order_by(Creator.created_at.desc())
                .offset(creator_offset).limit(BATCH_SIZE)
            )
            batch = rows.scalars().all()
            if not batch:
                break
            creator_docs = []
            for c in batch:
                creator_docs.append({
                    "id": str(c.id), "name": c.name,
                    "display_name": c.display_name or c.name,
                    "description": (c.description or "")[:500],
                    "is_active": c.is_active, "created_at": c.created_at.isoformat(),
                })
                stats["creators"] += 1
            await asyncio.to_thread(lambda: client.index(CREATORS_INDEX).add_documents(creator_docs))
            creator_offset += BATCH_SIZE

        # ── Index tags (paginated) ───────────────────────────────────────
        tag_offset = 0
        while True:
            rows = await self.db.execute(
                select(Tag).order_by(Tag.created_at.desc())
                .offset(tag_offset).limit(BATCH_SIZE)
            )
            batch = rows.scalars().all()
            if not batch:
                break
            tag_docs = []
            for t in batch:
                tag_docs.append({
                    "id": str(t.id), "normalized_name": t.normalized_name,
                    "category": t.category or "general", "created_at": t.created_at.isoformat(),
                })
                stats["tags"] += 1
            await asyncio.to_thread(lambda: client.index(TAGS_INDEX).add_documents(tag_docs))
            tag_offset += BATCH_SIZE

        logger.info("Reindexed: %d works, %d creators, %d tags",
                     stats["works"], stats["creators"], stats["tags"])
        return {
            "status": "ok",
            "message": f"Indexed {stats['works']} works, {stats['creators']} creators, {stats['tags']} tags",
            **stats,
        }
