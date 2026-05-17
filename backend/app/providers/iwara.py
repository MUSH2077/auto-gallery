import re

from app.providers.base import BaseProvider, ProviderCapabilities


class IwaraProvider(BaseProvider):
    @property
    def source_name(self) -> str:
        return "iwara"

    @property
    def display_name(self) -> str:
        return "Iwara"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_download=True,
            supports_gallerydl=True,
            supports_tags=True,
        )

    def normalize_url(self, input_text: str) -> str | None:
        match = re.search(r"iwara\.tv/(video|profile)/[\w-]+", input_text)
        if match:
            return f"https://www.iwara.tv/{match.group(1)}/{match.group(0).split('/')[-1]}"
        return None

    def validate_url(self, url: str) -> bool:
        return bool(re.match(r"https?://(?:www\.)?iwara\.tv/(video|profile)/[\w-]+", url))

    def build_gallerydl_config(self, subscription_source, naming_template) -> dict:
        return {
            "extractor": {
                "iwara": {
                    "cookies": "/gallerydl-config/cookies/iwara.txt",
                    "directory": [naming_template.template if naming_template else "{user[name]}"],
                }
            }
        }

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        raise NotImplementedError("Iwara import is not yet supported")

    def parse_work_source(self, raw_metadata: dict) -> dict:
        raise NotImplementedError("Iwara import is not yet supported")

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        raise NotImplementedError("Iwara import is not yet supported")

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        raise NotImplementedError("Iwara import is not yet supported")
