from fastapi import APIRouter

from app.auth import RequirePermission
from app.providers import registry
from app.schemas.source_registry import SourceListResponse
from app.services.remote_discovery_rollout import provider_rollout

router = APIRouter(dependencies=[RequirePermission("subscriptions")])


@router.get("", response_model=SourceListResponse)
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
                "remote_discovery_rollout": (
                    provider_rollout(p.source_name)
                    if p.capabilities.supports_remote_discovery
                    else None
                ),
            },
        })
    return {"sources": sources}
