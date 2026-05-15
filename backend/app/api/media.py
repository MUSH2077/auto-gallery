from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from app.config import settings

router = APIRouter()


@router.get("/media/thumb/{asset_id}")
async def thumb(asset_id: str):
    return {"status": "not_implemented", "message": f"Thumbnail for {asset_id}", "asset_id": asset_id}


@router.get("/media/preview/{asset_id}")
async def preview(asset_id: str):
    return {"status": "not_implemented", "message": f"Preview for {asset_id}", "asset_id": asset_id}


@router.get("/media/original/{asset_id}")
async def original(asset_id: str):
    return {"status": "not_implemented", "message": f"Original for {asset_id}", "asset_id": asset_id}
