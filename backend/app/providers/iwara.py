import os
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
        match = re.search(r"iwara\.tv/(video|profile|users)/[\w-]+", input_text)
        if match:
            id_part = match.group(0).split('/')[-1]
            return f"https://www.iwara.tv/{match.group(1)}/{id_part}"
        return None

    def validate_url(self, url: str) -> bool:
        return bool(re.match(r"https?://(?:www\.)?iwara\.tv/(video|profile|users)/[\w-]+", url))

    def build_gallerydl_config(self, subscription_source) -> dict:
        cfg = {
            "extractor": {
                "iwara": {}
            }
        }
        cookies_path = "/gallerydl-config/cookies/iwara.txt"
        if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
            cfg["extractor"]["iwara"]["cookies"] = cookies_path
        return cfg

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        user = raw_metadata.get("user", {})
        user_id = str(user.get("id", ""))
        return {
            "source": self.source_name,
            "source_creator_id": user_id,
            "source_url": f"https://www.iwara.tv/profile/{user.get('name', user_id)}" if user_id else None,
            "display_name": user.get("nick") or user.get("name"),
            "raw_metadata": user,
        }

    def parse_work_source(self, raw_metadata: dict) -> dict:
        media_type = raw_metadata.get("type", "video")
        media_id = str(raw_metadata.get("id", ""))
        user = raw_metadata.get("user", {})
        return {
            "source": self.source_name,
            "source_work_id": media_id,
            "source_url": f"https://www.iwara.tv/{media_type}/{media_id}" if media_id else None,
            "source_creator_id": str(user.get("id", "")),
            "title": raw_metadata.get("title") or "",
            "description": raw_metadata.get("description") or "",
            "posted_at": raw_metadata.get("date"),
            "raw_metadata": raw_metadata,
        }

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        return [{
            "source": self.source_name,
            "source_asset_id": str(raw_metadata.get("file_id", raw_metadata.get("id", ""))),
            "source_url": raw_metadata.get("url"),
            "width": raw_metadata.get("width"),
            "height": raw_metadata.get("height"),
            "raw_metadata": raw_metadata,
        }]

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        tags = raw_metadata.get("tags", [])
        result = []
        for tag in tags:
            if isinstance(tag, str):
                result.append({"source": self.source_name, "original_name": tag, "category": "general"})
            elif isinstance(tag, dict):
                result.append({"source": self.source_name, "original_name": tag.get("name") or tag.get("id", str(tag)), "category": "general"})
        # Also emit rating tag if present
        rating = raw_metadata.get("rating")
        if rating:
            result.append({"source": self.source_name, "original_name": f"rating:{rating}", "category": "meta"})
        return result

    def get_creator_directory_name(self, raw_metadata: dict) -> str:
        user = raw_metadata.get("user", {})
        return str(user.get("id") or user.get("name", "unknown"))

    def get_creator_dir_from_url(self, source_url: str) -> str | None:
        # Iwara profile/user URLs contain the username, which matches
        # the gallery-dl template: ["iwara", "{user[name]}"]
        m = re.search(r'iwara\.tv/(?:profile|users)/([\w-]+)', source_url)
        return m.group(1) if m else None
