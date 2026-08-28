"""Typed public source registry and effective rollout capabilities."""

from pydantic import BaseModel


class RemoteDiscoveryRolloutRead(BaseModel):
    manual_preview: bool
    auto_import: bool
    unavailable_reason: str | None


class SourceCapabilitiesRead(BaseModel):
    can_download: bool
    can_import_local: bool
    supports_gallerydl: bool
    supports_tags: bool
    is_reference_only: bool
    supports_remote_discovery: bool
    discovery_auth_methods: list[str]
    supports_collection_selectors: bool
    remote_discovery_rollout: RemoteDiscoveryRolloutRead | None = None


class SourceRead(BaseModel):
    source_name: str
    display_name: str
    capabilities: SourceCapabilitiesRead


class SourceListResponse(BaseModel):
    sources: list[SourceRead]
