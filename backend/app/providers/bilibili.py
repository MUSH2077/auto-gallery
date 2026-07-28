import re

from app.providers.base import BaseProvider, ProviderCapabilities


class BilibiliProvider(BaseProvider):
    """Bilibili download provider — user articles, individual articles, and article favorites."""

    @property
    def source_name(self) -> str:
        return "bilibili"

    @property
    def display_name(self) -> str:
        return "哔哩哔哩 (Bilibili)"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_download=True,
            supports_gallerydl=True,
            supports_tags=True,
        )

    def normalize_url(self, input_text: str) -> str | None:
        # User articles page: https://space.bilibili.com/{uid}/article
        match = re.search(r"space\.bilibili\.com/(\d+)(?:/article)?", input_text)
        if match:
            uid = match.group(1)
            return f"https://space.bilibili.com/{uid}/article"

        # Individual article: https://www.bilibili.com/read/cv{id} or /read/mobile/{id}
        match = re.search(r"bilibili\.com/read/(?:cv|mobile/)?(\d+)", input_text)
        if match:
            article_id = match.group(1)
            return f"https://www.bilibili.com/read/cv{article_id}"

        # User article favorites: https://space.bilibili.com/{uid}/favlist?fid={fid}&ftype=article
        match = re.search(r"space\.bilibili\.com/(\d+)/favlist\?.*ftype=article", input_text)
        if match:
            return input_text.strip()

        return None

    def validate_url(self, url: str) -> bool:
        patterns = [
            r"https?://space\.bilibili\.com/\d+/article",
            r"https?://(?:www\.)?bilibili\.com/read/(?:cv|mobile/)?\d+",
            r"https?://space\.bilibili\.com/\d+/favlist\?.*ftype=article",
        ]
        return any(re.match(p, url) for p in patterns)

    def build_gallerydl_config(self, subscription_source) -> dict:
        config: dict = {
            "extractor": {
                "bilibili": {
                    "livephoto": True,
                    "sleep-request": "3.0-6.0",
                }
            }
        }
        return config

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        user = raw_metadata.get("user", {})
        # gallery-dl exposes user as {id, name} for bilibili
        user_id = str(user.get("id", ""))
        name = user.get("name") or user_id
        return {
            "source": self.source_name,
            "source_creator_id": user_id,
            "source_url": f"https://space.bilibili.com/{user_id}" if user_id else None,
            "display_name": name,
            "raw_metadata": user,
        }

    def parse_work_source(self, raw_metadata: dict) -> dict:
        article_id = str(raw_metadata.get("id", ""))
        user = raw_metadata.get("user", {})
        user_id = str(user.get("id", ""))
        title = raw_metadata.get("title") or ""
        description = raw_metadata.get("summary") or raw_metadata.get("content") or ""
        source_url = f"https://www.bilibili.com/read/cv{article_id}" if article_id else None

        # gallery-dl provides "date" as a Unix timestamp for bilibili
        posted_at = raw_metadata.get("date")

        return {
            "source": self.source_name,
            "source_work_id": article_id,
            "source_url": source_url,
            "source_creator_id": user_id,
            "title": title[:500] if title else None,
            "description": description,
            "posted_at": posted_at,
            "raw_metadata": raw_metadata,
        }

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        article_id = str(raw_metadata.get("id", ""))
        num = raw_metadata.get("num", 0)
        url = raw_metadata.get("url") or raw_metadata.get("image_url")
        width = raw_metadata.get("width")
        height = raw_metadata.get("height")
        asset_id = f"{article_id}_{num}" if article_id else None
        return [{
            "source": self.source_name,
            "source_asset_id": asset_id,
            "source_url": url,
            "width": width,
            "height": height,
            "raw_metadata": raw_metadata,
        }]

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        result = []
        # gallery-dl exposes article categories/tags as a list of strings or dicts
        tags = raw_metadata.get("tags", [])
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, str) and tag.strip():
                    result.append({
                        "source": self.source_name,
                        "name": tag.strip(),
                        "raw": tag,
                    })
                elif isinstance(tag, dict):
                    name = tag.get("tag") or tag.get("name") or ""
                    if name.strip():
                        result.append({
                            "source": self.source_name,
                            "name": name.strip(),
                            "raw": tag,
                        })
        return result

    def get_creator_directory_name(self, raw_metadata: dict) -> str:
        user = raw_metadata.get("user", {})
        return str(user.get("id") or user.get("name", "unknown"))

    def get_creator_dir_from_url(self, source_url: str) -> str | None:
        m = re.search(r'space\.bilibili\.com/(\d+)', source_url)
        return m.group(1) if m else None
