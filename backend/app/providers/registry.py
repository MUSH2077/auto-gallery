from app.providers.base import BaseProvider


class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, BaseProvider] = {}

    def register(self, provider: BaseProvider) -> None:
        self._providers[provider.source_name] = provider

    def get(self, source_name: str) -> BaseProvider:
        if source_name not in self._providers:
            raise KeyError(f"Unknown source: {source_name}")
        return self._providers[source_name]

    def list_sources(self) -> list[str]:
        return list(self._providers.keys())

    def list_downloadable(self) -> list[str]:
        return [
            name
            for name, p in self._providers.items()
            if p.capabilities.can_download
        ]

    def list_reference(self) -> list[str]:
        return [
            name
            for name, p in self._providers.items()
            if p.capabilities.is_reference_only
        ]


registry = ProviderRegistry()
