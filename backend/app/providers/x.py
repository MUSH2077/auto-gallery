import os
import re

from app.providers.base import BaseProvider, ProviderCapabilities, ProviderSearchURL


class XProvider(BaseProvider):
    @staticmethod
    def _user(raw_metadata: dict) -> dict:
        user = raw_metadata.get("user") or raw_metadata.get("author") or {}
        return user if isinstance(user, dict) else {}

    @property
    def source_name(self) -> str:
        return "x"

    @property
    def display_name(self) -> str:
        return "X / Twitter"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_download=True,
            supports_gallerydl=True,
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
        return bool(re.match(r"https?://(?:twitter\.com|x\.com)/\w+(?:/status/\d+)?/?(?:\?.*)?$", url))

    def parse_search_url(self, input_text: str) -> ProviderSearchURL | None:
        match = re.search(r"(?:twitter\.com|x\.com)/(\w+)/status/(\d+)", input_text)
        if match:
            return ProviderSearchURL(
                kind="work",
                normalized_url=f"https://x.com/{match.group(1)}/status/{match.group(2)}",
            )
        match = re.search(r"(?:twitter\.com|x\.com)/(\w+)/?(?:\?.*)?$", input_text)
        if match:
            return ProviderSearchURL(
                kind="creator",
                normalized_url=f"https://x.com/{match.group(1)}",
            )
        return None

    def build_gallerydl_config(self, subscription_source) -> dict:
        cfg = {
            "extractor": {
                "twitter": {}
            }
        }
        cookies_path = "/gallerydl-config/cookies/twitter.txt"
        if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
            cfg["extractor"]["twitter"]["cookies"] = cookies_path
        return cfg

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        user = self._user(raw_metadata)
        name = user.get("name") or user.get("screen_name") or ""
        user_id = str(user.get("id") or user.get("id_str") or name)
        return {
            "source": self.source_name,
            "source_creator_id": user_id,
            "source_url": f"https://x.com/{name}" if name else None,
            "display_name": user.get("nick") or name,
            "raw_metadata": user,
        }

    def parse_work_source(self, raw_metadata: dict) -> dict:
        tweet_id = str(
            raw_metadata.get("tweet_id")
            or raw_metadata.get("id_str")
            or raw_metadata.get("id")
            or ""
        )
        user = self._user(raw_metadata)
        screen_name = user.get("name") or user.get("screen_name") or ""
        source_creator_id = str(user.get("id") or user.get("id_str") or screen_name)
        body = (
            raw_metadata.get("content")
            or raw_metadata.get("full_text")
            or raw_metadata.get("text")
            or ""
        )
        return {
            "source": self.source_name,
            "source_work_id": tweet_id,
            "source_url": f"https://x.com/{screen_name}/status/{tweet_id}" if screen_name and tweet_id else None,
            "source_creator_id": source_creator_id,
            "title": body[:200],
            "description": body,
            "posted_at": raw_metadata.get("date") or raw_metadata.get("created_at"),
            "raw_metadata": raw_metadata,
        }

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        # Twitter metadata has entities.media array for images/videos
        entities = raw_metadata.get("entities", {})
        media_list = entities.get("media", [])
        if not media_list:
            # Fallback: single asset from raw_metadata
            return [{
                "source": self.source_name,
                "source_asset_id": str(raw_metadata.get("id_str") or raw_metadata.get("id", "")),
                "source_url": raw_metadata.get("url") or raw_metadata.get("media_url"),
                "width": raw_metadata.get("width"),
                "height": raw_metadata.get("height"),
                "raw_metadata": raw_metadata,
            }]

        result = []
        for media in media_list:
            result.append({
                "source": self.source_name,
                "source_asset_id": str(media.get("id_str", "")),
                "source_url": media.get("media_url") or media.get("media_url_https"),
                "width": (media.get("sizes", {}).get("large", {}).get("w") or media.get("sizes", {}).get("medium", {}).get("w")),
                "height": (media.get("sizes", {}).get("large", {}).get("h") or media.get("sizes", {}).get("medium", {}).get("h")),
                "raw_metadata": media,
            })
        return result

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        result = []
        seen: set[str] = set()

        # Current gallery-dl emits a top-level list of strings. Older metadata
        # fixtures and API responses use entities.hashtags objects.
        for hashtag in raw_metadata.get("hashtags", []) or []:
            text = hashtag.get("text", "") if isinstance(hashtag, dict) else str(hashtag)
            normalized = text.strip()
            if normalized and normalized.casefold() not in seen:
                seen.add(normalized.casefold())
                result.append({"source": self.source_name, "original_name": normalized, "category": "hashtag"})

        entities = raw_metadata.get("entities", {})
        for hashtag in entities.get("hashtags", []):
            text = hashtag.get("text", "") if isinstance(hashtag, dict) else str(hashtag)
            normalized = text.strip()
            if normalized and normalized.casefold() not in seen:
                seen.add(normalized.casefold())
                result.append({"source": self.source_name, "original_name": normalized, "category": "hashtag"})
        # User mentions
        for mention in entities.get("user_mentions", []):
            name = mention.get("name") or mention.get("screen_name", "")
            if name:
                result.append({"source": self.source_name, "original_name": f"@{name}", "category": "mention"})
        return result

    def get_creator_dir_from_url(self, source_url: str) -> str | None:
        m = re.search(r"(?:twitter\.com|x\.com)/(\w+)(?:/status/\d+)?/?(?:\?.*)?$", source_url)
        return m.group(1) if m else None

    def get_creator_directory_name(self, raw_metadata: dict) -> str:
        user = self._user(raw_metadata)
        return str(user.get("name") or user.get("screen_name") or user.get("id", "unknown"))
