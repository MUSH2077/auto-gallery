"""Registry for provider-specific remote discovery adapters."""

from app.remote_discovery.contract import RemoteDiscoveryAdapter


class DiscoveryAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, RemoteDiscoveryAdapter] = {}

    def register(self, adapter: RemoteDiscoveryAdapter) -> None:
        self._adapters[adapter.source] = adapter

    def get(self, source: str) -> RemoteDiscoveryAdapter:
        try:
            return self._adapters[source]
        except KeyError as exc:
            raise KeyError(f"Unknown remote discovery source: {source}") from exc

    def list_sources(self) -> tuple[str, ...]:
        return tuple(self._adapters)


registry = DiscoveryAdapterRegistry()
