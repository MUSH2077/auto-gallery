import re

from app.providers.base import BaseProvider, ProviderCapabilities


class PinterestProvider(BaseProvider):
    """Pinterest download provider — pins, boards, user all-pins."""

    @property
    def source_name(self) -> str:
        return "pinterest"

    @property
    def display_name(self) -> str:
        return "Pinterest"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_download=True,
            supports_gallerydl=True,
            supports_tags=False,
        )

    def normalize_url(self, input_text: str) -> str | None:
        match = re.search(r"pinterest\.\w+(?:\.\w+)?/pin/(\d+)", input_text)
        if match:
            return f"https://www.pinterest.com/pin/{match.group(1)}"
        match = re.search(r"pinterest\.\w+(?:\.\w+)?/([\w.-]+)/pins", input_text)
        if match:
            return f"https://www.pinterest.com/{match.group(1)}/pins/"
        match = re.search(r"pinterest\.\w+(?:\.\w+)?/([\w.-]+)/([\w.-]+)", input_text)
        if match:
            return f"https://www.pinterest.com/{match.group(1)}/{match.group(2)}/"
        return None

    def validate_url(self, url: str) -> bool:
        return bool(re.match(
            r"https?://(?:[\w-]+\.)?pinterest\.\w+(?:\.\w+)?/(pin/\d+|[\w.-]+/(?:pins|[\w.-]+/?))",
            url,
        ))

    def build_gallerydl_config(self, subscription_source, naming_template) -> dict:
        return {
            "extractor": {
                "pinterest": {
                    "directory": [naming_template.template if naming_template else "pinterest/{user}/{board[name]}"],
                }
            }
        }

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        user = raw_metadata.get("user", "")
        board = raw_metadata.get("board", {})
        username = board.get("owner", {}).get("username", "") if isinstance(board, dict) else ""
        creator_id = username or user or "unknown"
        return {
            "source": self.source_name,
            "source_creator_id": creator_id,
            "source_url": f"https://www.pinterest.com/{creator_id}/" if creator_id != "unknown" else None,
            "display_name": creator_id,
            "raw_metadata": {"user": user, "board_name": board.get("name", "") if isinstance(board, dict) else ""},
        }

    def parse_work_source(self, raw_metadata: dict) -> dict:
        pin_id = str(raw_metadata.get("id", ""))
        return {
            "source": self.source_name,
            "source_work_id": pin_id,
            "source_url": f"https://www.pinterest.com/pin/{pin_id}",
            "source_creator_id": raw_metadata.get("user", ""),
            "title": None,
            "description": raw_metadata.get("description") or raw_metadata.get("closeup_description"),
            "posted_at": None,
            "raw_metadata": raw_metadata,
        }

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        pin_id = str(raw_metadata.get("id", ""))
        return [{
            "source": self.source_name,
            "source_asset_id": pin_id,
            "source_url": raw_metadata.get("url"),
            "width": None,
            "height": None,
            "raw_metadata": raw_metadata,
        }]

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        return []

