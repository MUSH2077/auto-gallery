from fastapi import APIRouter

router = APIRouter()


@router.get("/providers")
async def list_reference_providers():
    return {"providers": [{"source_name": "danbooru_reference", "display_name": "Danbooru", "capabilities": {"is_reference_only": True}}]}


@router.post("/danbooru/artist/preview")
async def preview_danbooru_artist(data: dict):
    tag = data.get("tag", "")
    if not tag:
        return {"status": "error", "message": "Tag is required"}
    return {
        "status": "ok",
        "artist": {
            "tag": tag,
            "name": tag,
            "related_urls": [],
            "notes": "Danbooru reference API not yet connected. This endpoint will query Danbooru artist data when backend integration is implemented.",
        },
    }


@router.post("/danbooru/artist/import")
async def import_danbooru_artist(data: dict):
    return {"status": "not_implemented", "message": "Danbooru artist import is not yet implemented"}
