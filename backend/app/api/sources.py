from fastapi import APIRouter

from app.auth import RequirePermission
from app.providers import registry

router = APIRouter(dependencies=[RequirePermission("subscriptions")])


@router.get("")
async def list_sources():
    sources = []
    for name in registry.list_sources():
        p = registry.get(name)
        sources.append({
            "source_name": p.source_name,
            "display_name": p.display_name,
            "capabilities": {
                "can_download": p.capabilities.can_download,
                "can_import_local": p.capabilities.can_import_local,
                "supports_gallerydl": p.capabilities.supports_gallerydl,
                "supports_tags": p.capabilities.supports_tags,
                "is_reference_only": p.capabilities.is_reference_only,
                "supports_remote_discovery": p.capabilities.supports_remote_discovery,
                "discovery_auth_methods": p.capabilities.discovery_auth_methods,
                "supports_collection_selectors": p.capabilities.supports_collection_selectors,
            },
        })
    return {"sources": sources}
