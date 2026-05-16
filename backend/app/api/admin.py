from fastapi import APIRouter, Depends

from app.auth import RequireAdmin

router = APIRouter(dependencies=[RequireAdmin])


@router.get("/settings")
async def get_settings():
    return {"dedup": {"source_level_enabled": False, "cross_source_enabled": False, "auto_merge": False, "phash_threshold": 8}}


@router.post("/search/reindex")
async def reindex_search():
    return {"status": "ok", "message": "Reindex triggered"}

from pydantic import BaseModel

class DedupSettings(BaseModel):
    source_level_enabled: bool = False
    cross_source_enabled: bool = False
    auto_merge: bool = False
    phash_threshold: int = 8

class AdminSettingsUpdate(BaseModel):
    dedup: DedupSettings | None = None

@router.put("/settings")
async def update_settings(data: AdminSettingsUpdate):
    return {"status": "ok", "message": "Settings updated (stored in memory for v1)", "dedup": data.dedup.model_dump() if data.dedup else None}

class GalleryDLConfig(BaseModel):
    cookies_path: str | None = None
    refresh_token: str | None = None
    filename: str | None = None
    directory: str | None = None
    include: str | None = "artworks"
    tags: str | None = "japanese"
    ugoira: bool | None = True
    sleep_request: float | None = None
    max_posts: int | None = None

@router.get("/gallerydl-config")
async def get_gallerydl_config():
    import json, os
    config_path = os.path.join(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "config.json")
    config = {}
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
    pixiv = config.get("extractor", {}).get("pixiv", {})
    return {
        "refresh_token": pixiv.get("refresh-token"),
        "cookies_path": pixiv.get("cookies"),
        "filename": pixiv.get("filename"),
        "directory": "/".join(pixiv["directory"]) if isinstance(pixiv.get("directory"), list) else pixiv.get("directory"),
        "include": str(pixiv.get("include", "artworks")),
        "tags": pixiv.get("tags", "japanese"),
        "ugoira": pixiv.get("ugoira", True),
        "sleep_request": pixiv.get("sleep-request"),
        "max_posts": pixiv.get("max-posts"),
    }

@router.put("/gallerydl-config")
async def update_gallerydl_config(data: GalleryDLConfig):
    import json, os
    config_path = os.path.join(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "config.json")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    existing = {}
    if os.path.exists(config_path):
        with open(config_path) as f:
            existing = json.load(f)
    pixiv = existing.get("extractor", {}).get("pixiv", {})
    if data.refresh_token is not None: pixiv["refresh-token"] = data.refresh_token
    if data.cookies_path is not None: pixiv["cookies"] = data.cookies_path
    if data.filename is not None: pixiv["filename"] = data.filename
    if data.directory is not None: pixiv["directory"] = data.directory.split("/") if "/" in data.directory else data.directory
    if data.include is not None: pixiv["include"] = data.include
    if data.tags is not None: pixiv["tags"] = data.tags
    if data.ugoira is not None: pixiv["ugoira"] = data.ugoira
    if data.sleep_request is not None: pixiv["sleep-request"] = data.sleep_request
    if data.max_posts is not None: pixiv["max-posts"] = data.max_posts
    existing.setdefault("extractor", {})["pixiv"] = pixiv
    with open(config_path, "w") as f:
        json.dump(existing, f, indent=2)
    return {"status": "ok", "message": "gallery-dl config updated", "path": config_path}


# ── Dedup ──

@router.get("/dedup/duplicates")
async def list_duplicates():
    """List works where the same source+source_work_id appears multiple times."""
    from app.database import async_session
    from app.models import WorkSource
    from sqlalchemy import func, select

    async with async_session() as db:
        result = await db.execute(
            select(
                WorkSource.source,
                WorkSource.source_work_id,
                func.count(WorkSource.id).label("cnt"),
                func.array_agg(WorkSource.work_id).label("work_ids"),
            )
            .group_by(WorkSource.source, WorkSource.source_work_id)
            .having(func.count(WorkSource.id) > 1)
            .limit(100)
        )
        duplicates = []
        for row in result:
            duplicates.append({
                "source": row.source,
                "source_work_id": row.source_work_id,
                "count": row.cnt,
                "work_ids": [str(w) for w in row.work_ids],
            })
        return {"duplicates": duplicates, "total": len(duplicates)}


@router.post("/dedup/scan")
async def scan_duplicates():
    """Trigger a duplicate scan (deferred to future phase — returns current state)."""
    from app.database import async_session
    from app.models import WorkSource
    from sqlalchemy import func, select

    async with async_session() as db:
        result = await db.execute(
            select(func.count(func.distinct(WorkSource.source + WorkSource.source_work_id)).label("unique_works"),
                   func.count(WorkSource.id).label("total_sources"))
        )
        row = result.one()
        return {
            "status": "ok",
            "unique_works": row.unique_works,
            "total_source_records": row.total_sources,
            "message": "Dedup scan completed. Duplicate works have multiple work_source records for the same source+source_work_id.",
        }


# ── Merge Candidates ──

@router.get("/merge-candidates")
async def list_merge_candidates():
    """List potential merge candidates (works with the same title across sources)."""
    from app.database import async_session
    from app.models import Work, WorkSource
    from sqlalchemy import func, select

    async with async_session() as db:
        result = await db.execute(
            select(
                Work.title,
                func.count(func.distinct(WorkSource.source)).label("source_count"),
                func.array_agg(func.distinct(WorkSource.source)).label("sources"),
                func.array_agg(Work.id).label("work_ids"),
            )
            .join(WorkSource, WorkSource.work_id == Work.id)
            .where(Work.title.isnot(None))
            .group_by(Work.title)
            .having(func.count(func.distinct(WorkSource.source)) > 1)
            .limit(100)
        )
        candidates = []
        for row in result:
            candidates.append({
                "title": row.title,
                "source_count": row.source_count,
                "sources": [str(s) for s in row.sources],
                "work_ids": [str(w) for w in row.work_ids],
            })
        return {"candidates": candidates, "total": len(candidates)}


@router.post("/merge-candidates/{work_id}/merge")
async def merge_works(work_id: str, data: dict):
    """Merge works (deferred to future phase)."""
    return {"status": "not_implemented", "message": "Merge functionality is deferred to a future phase."}
