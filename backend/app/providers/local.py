import re

from app.providers.base import BaseProvider, ProviderCapabilities


class LocalProvider(BaseProvider):
    @property
    def source_name(self) -> str:
        return "local"

    @property
    def display_name(self) -> str:
        return "Local Folder"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_import_local=True,
        )

    def normalize_url(self, input_text: str) -> str | None:
        return input_text

    def validate_url(self, url: str) -> bool:
        return True

    def build_gallerydl_config(self, subscription_source) -> dict:
        raise NotImplementedError("Local import does not use gallery-dl")

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        raise NotImplementedError("Local import is not yet implemented")

    def parse_work_source(self, raw_metadata: dict) -> dict:
        raise NotImplementedError("Local import is not yet implemented")

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        raise NotImplementedError("Local import is not yet implemented")

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        raise NotImplementedError("Local import is not yet implemented")
