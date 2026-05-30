import re

from app.providers.base import BaseProvider, ProviderCapabilities


class WeiboProvider(BaseProvider):
    """微博 (Weibo) download provider — user feeds, albums, and statuses."""

    @property
    def source_name(self) -> str:
        return "weibo"

    @property
    def display_name(self) -> str:
        return "微博 (Weibo)"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_download=True,
            supports_gallerydl=True,
            supports_tags=True,
        )

    def normalize_url(self, input_text: str) -> str | None:
        # Status URL: https://weibo.com/USER/STATUS or https://m.weibo.cn/detail/STATUS
        match = re.search(r"(?:weibo\.com|weibo\.cn)/(\d+)/(\w+)", input_text)
        if match:
            return f"https://weibo.com/{match.group(1)}/{match.group(2)}"
        # User by numeric ID: https://weibo.com/u/1234567890
        match = re.search(r"(?:weibo\.com|weibo\.cn)/u/(\d+)", input_text)
        if match:
            return f"https://weibo.com/u/{match.group(1)}"
        # User by screen name: https://weibo.com/username
        match = re.search(r"(?:weibo\.com|weibo\.cn)/([\w\u4e00-\u9fff]+)(?:/|$)", input_text)
        if match:
            return f"https://weibo.com/{match.group(1)}"
        return None

    def validate_url(self, url: str) -> bool:
        return bool(re.match(
            r"https?://(?:www\.|m\.)?weibo\.(?:com|cn)/(?:u/\d+|[\w\u4e00-\u9fff]+(?:/\w+)?)?/?(?:\?.*)?$",
            url,
        ))

    def build_gallerydl_config(self, subscription_source, naming_template) -> dict:
        config: dict = {
            "extractor": {
                "weibo": {
                    "directory": [naming_template.template if naming_template else "weibo/{user[id]}/{id}"],
                    "videos": True,
                    "retweets": False,
                }
            }
        }
        cookies_path = "/gallerydl-config/cookies/weibo.txt"
        config["extractor"]["weibo"]["cookies"] = cookies_path
        return config

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        user = raw_metadata.get("user", {})
        user_id = str(user.get("id", ""))
        name = user.get("name") or user.get("screen_name") or user_id
        return {
            "source": self.source_name,
            "source_creator_id": user_id,
            "source_url": f"https://weibo.com/u/{user_id}" if user_id else None,
            "display_name": name,
            "raw_metadata": user,
        }

    def parse_work_source(self, raw_metadata: dict) -> dict:
        status_id = str(raw_metadata.get("id", ""))
        user = raw_metadata.get("user", {})
        user_id = str(user.get("id", ""))
        name = user.get("name") or user.get("screen_name") or ""
        # Weibo status URL uses the mblogid (base62) when available, fall back to numeric ID
        mblogid = raw_metadata.get("mblogid") or status_id
        source_url = f"https://weibo.com/{user_id}/{mblogid}" if user_id and mblogid else None
        text = raw_metadata.get("text_raw") or raw_metadata.get("text") or ""
        return {
            "source": self.source_name,
            "source_work_id": status_id,
            "source_url": source_url,
            "source_creator_id": user_id,
            "title": text[:200] if text else None,
            "description": text,
            "posted_at": raw_metadata.get("created_at"),
            "raw_metadata": raw_metadata,
        }

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        status_id = str(raw_metadata.get("id", ""))
        # gallery-dl merges image/video into a flat list per status;
        # use num for index when available, otherwise derive from file list
        num = raw_metadata.get("num", 0)
        pic_info = raw_metadata.get("pic_infos", {})
        # Try to find this specific asset's URL
        url = raw_metadata.get("url") or raw_metadata.get("original") or raw_metadata.get("large_url")
        width = raw_metadata.get("width") or raw_metadata.get("pic_width")
        height = raw_metadata.get("height") or raw_metadata.get("pic_height")
        asset_id = f"{status_id}_{num}"
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
        text = raw_metadata.get("text_raw") or raw_metadata.get("text") or ""
        # Extract #hashtag# patterns common in Weibo posts
        for tag in re.findall(r"#([^#]+)#", text):
            tag = tag.strip()
            if tag:
                result.append({
                    "source": self.source_name,
                    "original_name": tag,
                    "category": "hashtag",
                })
        return result

    def get_creator_directory_name(self, raw_metadata: dict) -> str:
        user = raw_metadata.get("user", {})
        return str(user.get("id") or user.get("name", "unknown"))

    def get_creator_dir_from_url(self, source_url: str) -> str | None:
        m = re.search(r'weibo\.com/u/(\d+)', source_url)
        return m.group(1) if m else None
