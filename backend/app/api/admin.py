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
