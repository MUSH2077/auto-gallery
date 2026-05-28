import re

from app.providers.base import BaseProvider, ProviderCapabilities


class LofterProvider(BaseProvider):
    """LOFTER download provider — blog posts and images."""

    @property
    def source_name(self) -> str:
        return "lofter"

    @property
    def display_name(self) -> str:
        return "LOFTER"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_download=True,
            supports_gallerydl=True,
            supports_tags=False,
        )

    def normalize_url(self, input_text: str) -> str | None:
        match = re.search(r"([\w-]+)\.lofter\.com/post/([\w_]+)", input_text)
        if match:
            return f"https://{match.group(1)}.lofter.com/post/{match.group(2)}"
        match = re.search(r"([\w-]+)\.lofter\.com", input_text)
        if match:
            return f"https://{match.group(1)}.lofter.com/"
        return None

    def validate_url(self, url: str) -> bool:
        return bool(re.match(
            r"https?://(?!www\.)[\w-]+\.lofter\.com(/post/[\w_]+)?/?$",
            url,
        ))

    def build_gallerydl_config(self, subscription_source, naming_template) -> dict:
        return {
            "extractor": {
                "lofter": {
                    "directory": [naming_template.template if naming_template else "lofter/{blog_name}/{id}"],
                }
            }
        }

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        blog_name = raw_metadata.get("blog_name", "unknown")
        return {
            "source": self.source_name,
            "source_creator_id": blog_name,
            "source_url": f"https://{blog_name}.lofter.com/",
            "display_name": blog_name,
            "raw_metadata": {"blog_name": blog_name},
        }

    def parse_work_source(self, raw_metadata: dict) -> dict:
        post_id = str(raw_metadata.get("id", ""))
        blog_name = raw_metadata.get("blog_name", "")
        return {
            "source": self.source_name,
            "source_work_id": post_id,
            "source_url": f"https://{blog_name}.lofter.com/post/{post_id}" if blog_name and post_id else None,
            "source_creator_id": blog_name,
            "title": raw_metadata.get("title"),
            "description": raw_metadata.get("content"),
            "posted_at": raw_metadata.get("date"),
            "raw_metadata": raw_metadata,
        }

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        post_id = str(raw_metadata.get("id", ""))
        return [{
            "source": self.source_name,
            "source_asset_id": f"{post_id}_{raw_metadata.get('num', 0)}",
            "source_url": raw_metadata.get("url"),
            "width": None,
            "height": None,
            "raw_metadata": raw_metadata,
        }]

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        return []

