from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.auth import bearer_scheme, get_admin_key
from app.config import settings
from app.database import async_session
from app.models.asset import Asset
from app.models.curation import AssetStorageState
from app.services.media_signing import verify_media_token

router = APIRouter()

# Resolve roots once at module load for path containment checks
_RESOLVED_DOWNLOAD_ROOT = Path(settings.download_root).resolve()
_RESOLVED_LIBRARY_ROOT = Path(settings.library_root).resolve()


def _safe_path(root: Path, relative: str) -> Path:
    """Resolve root/relative and verify it stays within root.

    Uses ``Path.relative_to`` for proper path-component containment rather
    than string prefix matching, which is vulnerable to sibling-directory
    attacks (e.g. ``/downloads_evil`` passing a check against ``/downloads``).

    Raises HTTPException(404) if the resolved path escapes root,
    preventing path traversal attacks through manipulated file_path values.
    """
    full = (root / relative).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found")
    return full


async def _serve(asset_id: str, size: str):
    async with async_session() as db:
        result = await db.execute(select(Asset).where(Asset.id == asset_id))
        asset = result.scalar_one_or_none()
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        storage_result = await db.execute(select(AssetStorageState).where(AssetStorageState.asset_id == asset.id))
        storage_state = storage_result.scalar_one_or_none()
        if storage_state and storage_state.storage_state == "purged":
            raise HTTPException(status_code=404, detail="File not found")

        if size == "thumb":
            if asset.thumb_sm_path:
                full = _safe_path(_RESOLVED_LIBRARY_ROOT, asset.thumb_sm_path)
                if full.exists():
                    return FileResponse(full, media_type="image/webp")
            raise HTTPException(status_code=404, detail="Thumbnail not found")
        else:
            full = _safe_path(_RESOLVED_DOWNLOAD_ROOT, asset.file_path)
            if not full.exists():
                raise HTTPException(status_code=404, detail="File not found")
            mime = asset.mime_type or "image/jpeg"
            return FileResponse(full, media_type=mime)


@router.get("/media/thumb/{asset_id}")
async def thumb(asset_id: str):
    """Serve thumbnail — no auth needed (embedded in <img> tags on admin-web).

    Thumbnails are content-addressed by asset id and never change, so they are
    safe to cache in the browser. preview/original stay uncached (auth-gated).
    """
    resp = await _serve(asset_id, "thumb")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@router.get("/media/preview/{asset_id}")
async def preview(asset_id: str, expires: str | None = None, token: str | None = None):
    """Serve preview image with a short-lived signed URL."""
    if not verify_media_token(asset_id, "preview", expires, token):
        raise HTTPException(status_code=401, detail="Invalid or expired media token")
    return await _serve(asset_id, "original")


@router.get("/media/original/{asset_id}")
async def original(
    asset_id: str,
    request: Request,
    expires: str | None = None,
    token: str | None = None,
    credentials=Depends(bearer_scheme),
):
    """Serve original image via admin auth or short-lived signed URL."""
    if not verify_media_token(asset_id, "original", expires, token):
        await get_admin_key(request, credentials)
    return await _serve(asset_id, "original")
