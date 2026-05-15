from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.asset import Asset

router = APIRouter()


async def _serve(asset_id: str, size: str):
    async with async_session() as db:
        result = await db.execute(select(Asset).where(Asset.id == asset_id))
        asset = result.scalar_one_or_none()
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")

        if size == "thumb":
            # Thumbnail lives in library/{source}/{creator}/{work_id}/thumbnail.webp
            if asset.thumb_sm_path:
                full = Path(settings.library_root) / asset.thumb_sm_path
                if full.exists():
                    return FileResponse(full, media_type="image/webp")
            raise HTTPException(status_code=404, detail="Thumbnail not found")
        else:
            # Original / preview: serve the downloaded file directly
            full = Path(settings.download_root) / asset.file_path
            if not full.exists():
                raise HTTPException(status_code=404, detail="File not found")
            mime = asset.mime_type or "image/jpeg"
            return FileResponse(full, media_type=mime)


@router.get("/media/thumb/{asset_id}")
async def thumb(asset_id: str):
    return await _serve(asset_id, "thumb")


@router.get("/media/preview/{asset_id}")
async def preview(asset_id: str):
    return await _serve(asset_id, "original")


@router.get("/media/original/{asset_id}")
async def original(asset_id: str):
    return await _serve(asset_id, "original")
