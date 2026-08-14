import re
import urllib.parse

from app.providers.base import BaseProvider, ProviderCapabilities, ProviderSearchURL


class DanbooruProvider(BaseProvider):
    """Danbooru download provider — downloads posts by tag search (e.g. artist tag)."""

    @property
    def source_name(self) -> str:
        return "danbooru"

    @property
    def display_name(self) -> str:
        return "Danbooru"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_download=True,
            supports_gallerydl=True,
            supports_tags=True,
        )

    def normalize_url(self, input_text: str) -> str | None:
        # Posts by tag search
        match = re.search(r"danbooru\.donmai\.us/posts\?tags=([^&\s]+)", input_text)
        if match:
            tags = urllib.parse.unquote(match.group(1))
            return f"https://danbooru.donmai.us/posts?tags={urllib.parse.quote(tags, safe='')}"
        # Artist page
        match = re.search(r"danbooru\.donmai\.us/artists/(\d+)", input_text)
        if match:
            return f"https://danbooru.donmai.us/artists/{match.group(1)}"
        # Pools
        match = re.search(r"danbooru\.donmai\.us/pools/(\d+)", input_text)
        if match:
            return f"https://danbooru.donmai.us/pools/{match.group(1)}"
        return None

    def validate_url(self, url: str) -> bool:
        return bool(re.match(
            r"https?://danbooru\.donmai\.us/(posts\?tags=.+|artists/\d+|pools/\d+)",
            url,
        ))

    def parse_search_url(self, input_text: str) -> ProviderSearchURL | None:
        match = re.search(r"danbooru\.donmai\.us/posts/(\d+)", input_text)
        if match:
            return ProviderSearchURL(
                kind="work",
                normalized_url=f"https://danbooru.donmai.us/posts/{match.group(1)}",
            )
        match = re.search(r"danbooru\.donmai\.us/artists/(\d+)", input_text)
        if match:
            return ProviderSearchURL(
                kind="creator",
                normalized_url=f"https://danbooru.donmai.us/artists/{match.group(1)}",
            )
        match = re.search(r"danbooru\.donmai\.us/posts\?tags=([^&\s]+)", input_text)
        if match:
            tags = urllib.parse.unquote(match.group(1))
            return ProviderSearchURL(
                kind="creator",
                normalized_url=f"https://danbooru.donmai.us/posts?tags={urllib.parse.quote(tags, safe='')}",
            )
        return None

    def build_gallerydl_config(self, subscription_source) -> dict:
        cfg = {
            "extractor": {
                "danbooru": {
                    "username": None,  # filled from config.json at runtime
                    "password": None,
                }
            }
        }
        return cfg

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        # Danbooru tag_string_artist uses spaces to separate artist tags
        # and underscores within artist names (e.g., "john_doe ask_askzy"
        # = two artists).  Use only the first artist to match the
        # Danbooru pre-import convention of one SourceCreator per artist.
        tag_artist = raw_metadata.get("tag_string_artist", "")
        first_artist = tag_artist.split()[0] if tag_artist else ""
        artist_name = first_artist if first_artist else "unknown"
        return {
            "source": self.source_name,
            "source_creator_id": artist_name,
            "source_url": f"https://danbooru.donmai.us/posts?tags={urllib.parse.quote(artist_name)}",
            "display_name": artist_name,
            "raw_metadata": {"tag_string_artist": tag_artist},
        }

    def parse_work_source(self, raw_metadata: dict) -> dict:
        post_id = str(raw_metadata.get("id", ""))
        return {
            "source": self.source_name,
            "source_work_id": post_id,
            "source_url": f"https://danbooru.donmai.us/posts/{post_id}",
            "source_creator_id": (first_artist if (first_artist := (raw_metadata.get("tag_string_artist", "").split()[0] if raw_metadata.get("tag_string_artist", "").strip() else "")) else None),
            "title": None,
            "description": raw_metadata.get("artist_commentary_desc"),
            "posted_at": raw_metadata.get("created_at"),
            "raw_metadata": raw_metadata,
        }

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        post_id = str(raw_metadata.get("id", ""))
        width = raw_metadata.get("image_width") or raw_metadata.get("width")
        height = raw_metadata.get("image_height") or raw_metadata.get("height")
        return [{
            "source": self.source_name,
            "source_asset_id": post_id,
            "source_url": raw_metadata.get("file_url"),
            "width": width,
            "height": height,
            "raw_metadata": raw_metadata,
        }]

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        result = []
        # Danbooru tag categories: artist, character, copyright, general, meta
        categories = {
            "tag_string_artist": "artist",
            "tag_string_character": "character",
            "tag_string_copyright": "copyright",
            "tag_string_general": "general",
            "tag_string_meta": "meta",
        }
        seen: set[str] = set()
        for field, category in categories.items():
            tag_str = raw_metadata.get(field, "")
            if not tag_str:
                continue
            for name in tag_str.split():
                name = name.strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                result.append({
                    "source": self.source_name,
                    "original_name": name,
                    "category": category,
                })
        return result
