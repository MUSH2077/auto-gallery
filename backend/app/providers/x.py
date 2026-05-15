import re

from app.providers.base import BaseProvider, ProviderCapabilities


class XProvider(BaseProvider):
    @property
    def source_name(self) -> str:
        return "x"

    @property
    def display_name(self) -> str:
        return "X / Twitter"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_download=False,
            supports_gallerydl=False,
            supports_tags=True,
        )

    def normalize_url(self, input_text: str) -> str | None:
        match = re.search(r"(?:twitter\.com|x\.com)/(\w+)/status/(\d+)", input_text)
        if match:
            return f"https://x.com/{match.group(1)}/status/{match.group(2)}"
        match = re.search(r"(?:twitter\.com|x\.com)/(\w+)/?$", input_text)
        if match:
            return f"https://x.com/{match.group(1)}"
        return None

    def validate_url(self, url: str) -> bool:
        return bool(re.match(r"https?://(?:twitter\.com|x\.com)/\w+(?:/status/\d+)?/?$", url))

    def build_gallerydl_config(self, subscription_source, naming_template) -> dict:
        raise NotImplementedError("X/Twitter download is not yet supported")

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        raise NotImplementedError("X/Twitter import is not yet supported")

    def parse_work_source(self, raw_metadata: dict) -> dict:
        raise NotImplementedError("X/Twitter import is not yet supported")

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        raise NotImplementedError("X/Twitter import is not yet supported")

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        raise NotImplementedError("X/Twitter import is not yet supported")
