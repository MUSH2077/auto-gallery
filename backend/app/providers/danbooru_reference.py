import re

from app.providers.base import BaseProvider, ProviderCapabilities


class DanbooruReferenceProvider(BaseProvider):
    @property
    def source_name(self) -> str:
        return "danbooru_reference"

    @property
    def display_name(self) -> str:
        return "Danbooru (Reference)"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            is_reference_only=True,
        )

    def normalize_url(self, input_text: str) -> str | None:
        match = re.search(r"danbooru\.donmai\.us/artists/(\d+)", input_text)
        if match:
            return f"https://danbooru.donmai.us/artists/{match.group(1)}"
        match = re.search(r"danbooru\.donmai\.us/posts\?tags=([^&]+)", input_text)
        if match:
            return f"https://danbooru.donmai.us/posts?tags={match.group(1)}"
        return None

    def validate_url(self, url: str) -> bool:
        return bool(re.match(r"https?://danbooru\.donmai\.us/(artists/\d+|posts\?tags=.+)", url))

    def build_gallerydl_config(self, subscription_source, naming_template) -> dict:
        raise NotImplementedError("Danbooru reference does not support downloading")

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        artist_id = raw_metadata.get("id", "")
        return {
            "source": self.source_name,
            "source_creator_id": str(artist_id),
            "source_url": f"https://danbooru.donmai.us/artists/{artist_id}" if artist_id else None,
            "display_name": raw_metadata.get("name"),
            "raw_metadata": raw_metadata,
        }

    def parse_work_source(self, raw_metadata: dict) -> dict:
        raise NotImplementedError("Danbooru reference does not parse works")

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        raise NotImplementedError("Danbooru reference does not parse assets")

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        raise NotImplementedError("Danbooru reference tag parsing is not yet implemented")
