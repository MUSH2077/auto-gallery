import os
import re

from app.providers.base import BaseProvider, ProviderCapabilities, ProviderSearchURL
from app.services.cookie_paths import resolve_cookie_path


class PixivProvider(BaseProvider):
    @property
    def source_name(self) -> str:
        return "pixiv"

    @property
    def display_name(self) -> str:
        return "Pixiv"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_download=True,
            supports_gallerydl=True,
            supports_tags=True,
            supports_remote_discovery=True,
            discovery_auth_methods=("refresh_token",),
            supports_collection_selectors=True,
        )

    def normalize_url(self, input_text: str) -> str | None:
        patterns = [
            r"pixiv\.net/(?:en/)?artworks/(\d+)",
            r"pixiv\.net/(?:en/)?users/(\d+)",
            r"pixiv\.net/stacc/([A-Za-z0-9_]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, input_text)
            if match:
                if "artworks" in pattern:
                    return f"https://www.pixiv.net/artworks/{match.group(1)}"
                if "stacc" in pattern:
                    return f"https://www.pixiv.net/stacc/{match.group(1)}"
                return f"https://www.pixiv.net/users/{match.group(1)}"
        return None

    def validate_url(self, url: str) -> bool:
        return bool(
            re.match(
                r"https?://(?:www\.)?pixiv\.net/(?:(?:en/)?(?:artworks|users)/\d+|stacc/[A-Za-z0-9_]+)",
                url,
            )
        )

    def parse_search_url(self, input_text: str) -> ProviderSearchURL | None:
        match = re.search(r"pixiv\.net/(?:en/)?(artworks|users)/(\d+)", input_text)
        if match:
            kind, identity = match.groups()
            entity = "work" if kind == "artworks" else "creator"
            return ProviderSearchURL(
                kind=entity,
                normalized_url=f"https://www.pixiv.net/{kind}/{identity}",
            )
        stacc = re.search(r"pixiv\.net/stacc/([A-Za-z0-9_]+)", input_text)
        if stacc:
            return ProviderSearchURL(
                kind="creator",
                normalized_url=f"https://www.pixiv.net/stacc/{stacc.group(1)}",
            )
        return None

    def build_gallerydl_config(self, subscription_source) -> dict:
        cfg = {
            "extractor": {
                "pixiv": {}
            }
        }
        # Only set cookies path if the file has actual content (>0 bytes).
        # An empty/touched file causes gallery-dl to try cookie auth with no
        # valid cookies, which fails even for public content.
        cookies_path = str(resolve_cookie_path("cookies/pixiv.txt"))
        if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
            cfg["extractor"]["pixiv"]["cookies"] = cookies_path
        return cfg

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        user = raw_metadata.get("user", {})
        return {
            "source": self.source_name,
            "source_creator_id": str(user.get("id", "")),
            "source_url": f"https://www.pixiv.net/users/{user.get('id', '')}",
            "display_name": user.get("name") or user.get("account"),
            "raw_metadata": raw_metadata.get("user", {}),
        }

    def parse_work_source(self, raw_metadata: dict) -> dict:
        return {
            "source": self.source_name,
            "source_work_id": str(raw_metadata.get("id", "")),
            "source_url": f"https://www.pixiv.net/artworks/{raw_metadata.get('id', '')}",
            "source_creator_id": str(raw_metadata.get("user", {}).get("id", "")),
            "title": raw_metadata.get("title"),
            "description": raw_metadata.get("description"),
            "posted_at": raw_metadata.get("date"),
            "raw_metadata": raw_metadata,
        }

    def parse_work_flags(self, raw_metadata: dict) -> dict[str, bool]:
        flags = super().parse_work_flags(raw_metadata)
        ai_type = raw_metadata.get("illust_ai_type")
        flags["is_ai_generated"] = flags["is_ai_generated"] or ai_type == 2
        rating = raw_metadata.get("rating")
        flags["is_nsfw"] = rating in ("R-18", "R-18G") or any(
            isinstance(raw_metadata.get(key), (int, float)) and raw_metadata[key] >= 1
            for key in ("x_restrict", "restrict")
        )
        return flags

    def get_creator_profile_url(self, source_creator_id: str) -> str:
        return f"https://www.pixiv.net/users/{source_creator_id}"

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        pages = raw_metadata.get("pages", [])
        if not pages and raw_metadata.get("url"):
            pages = [{"url": raw_metadata["url"], "width": raw_metadata.get("width"), "height": raw_metadata.get("height")}]
        result = []
        for page in pages:
            result.append({
                "source": self.source_name,
                "source_asset_id": str(page.get("id", page.get("url", ""))),
                "source_url": page.get("url"),
                "width": page.get("width"),
                "height": page.get("height"),
                "raw_metadata": page,
            })
        return result

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        tags = raw_metadata.get("tags", [])
        result = []
        for tag in tags:
            if isinstance(tag, dict):
                result.append({
                    "source": self.source_name,
                    "original_name": tag.get("name") or tag.get("tag"),
                    "category": "general",
                })
            elif isinstance(tag, str):
                result.append({
                    "source": self.source_name,
                    "original_name": tag,
                    "category": "general",
                })
        return result

    def get_creator_dir_from_url(self, source_url: str) -> str | None:
        m = re.search(r'pixiv\.net/(?:en/)?users/(\d+)', source_url)
        return m.group(1) if m else None
