import re

from app.providers.base import BaseProvider, ProviderCapabilities


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
        )

    def normalize_url(self, input_text: str) -> str | None:
        patterns = [
            r"pixiv\.net/(?:en/)?artworks/(\d+)",
            r"pixiv\.net/(?:en/)?users/(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, input_text)
            if match:
                return f"https://www.pixiv.net/artworks/{match.group(1)}" if "artworks" in pattern else f"https://www.pixiv.net/users/{match.group(1)}"
        return None

    def validate_url(self, url: str) -> bool:
        return bool(
            re.match(r"https?://(?:www\.)?pixiv\.net/(?:en/)?(artworks|users)/\d+", url)
        )

    def build_gallerydl_config(self, subscription_source, naming_template) -> dict:
        return {
            "extractor": {
                "pixiv": {
                    "cookies": "/gallerydl-config/cookies/pixiv.txt",
                    "directory": [naming_template.template if naming_template else "pixiv/{user[id]}"],
                }
            }
        }

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
