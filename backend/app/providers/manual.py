"""Manual Upload provider.

Manual uploads don't come from gallery-dl. ManualUploadService (see
app/services/manual_upload.py) synthesizes one metadata.json per uploaded
work with this shape (task-8-brief.md Step 1):

    {"category": "manual", "id": <work_uuid>, "title": ..., "tags": [...],
     "is_nsfw": bool, "uploaded_by": <username>, "target_creator_id": <uuid|None>,
     "files": [{"name", "original_name", "size"}, ...], "date": <iso8601>}

"target_creator_id" is set only when a curator explicitly targeted an
existing creator (creator_id + curation permission); it is not part of the
brief's minimum shape but is additive and required for correct creator
linking below.

The parse_* methods below read that shape directly -- there is no upstream
platform metadata to normalize.
"""
from app.providers.base import BaseProvider, ProviderCapabilities


def _identity_key(raw_metadata: dict) -> str:
    """Return the manual-source creator identity key for this work's metadata.

    Curator-targeted uploads carry "target_creator_id" and resolve to
    "creator:<uuid>"; personal-space uploads (the default) resolve to
    "user:<username>". This MUST exactly match the source_creator_id
    ManualUploadService used when provisioning the identity chain via
    app/services/disk_identity.py, so the import runner's SourceCreator
    lookup finds -- rather than duplicates -- that row and inherits its
    already-correct creator_id (see import_runner.py's SourceCreator
    resolution block, which runs before the idempotency check).
    """
    target_creator_id = raw_metadata.get("target_creator_id")
    if target_creator_id:
        return f"creator:{target_creator_id}"
    return f"user:{raw_metadata.get('uploaded_by') or 'unknown'}"


class ManualProvider(BaseProvider):
    @property
    def source_name(self) -> str:
        return "manual"

    @property
    def display_name(self) -> str:
        return "Manual Upload"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_import_local=True,
        )

    def normalize_url(self, input_text: str) -> str | None:
        return input_text

    def validate_url(self, url: str) -> bool:
        return True

    def build_gallerydl_config(self, subscription_source) -> dict:
        raise NotImplementedError("Manual upload does not use gallery-dl")

    def parse_source_creator(self, raw_metadata: dict) -> dict:
        key = _identity_key(raw_metadata)
        uploaded_by = raw_metadata.get("uploaded_by") or "unknown"
        return {
            "source": self.source_name,
            "source_creator_id": key,
            "source_url": f"manual://{key}",
            "display_name": uploaded_by,
            "raw_metadata": {
                "uploaded_by": uploaded_by,
                "target_creator_id": raw_metadata.get("target_creator_id"),
            },
        }

    def parse_work_source(self, raw_metadata: dict) -> dict:
        work_id = str(raw_metadata.get("id", ""))
        return {
            "source": self.source_name,
            "source_work_id": work_id,
            "source_url": f"manual://{work_id}",
            "source_creator_id": _identity_key(raw_metadata),
            "title": raw_metadata.get("title"),
            "description": None,
            "posted_at": raw_metadata.get("date"),
            # Manual uploads are user-intentional -- the uploader explicitly
            # chose this content, unlike gallery-dl subscription discovery,
            # which defaults newly-discovered sources to is_enabled=False
            # pending admin review (CLAUDE.md gotcha: "Only Pixiv defaults to
            # is_enabled=True on import; others default disabled"). This key
            # documents that exception; the functionally-enforced half of it
            # is the SubscriptionSource created in
            # ManualUploadService.save_upload(), which is set is_enabled=True
            # explicitly instead of inheriting disk_identity's default False.
            "is_enabled": True,
            "raw_metadata": raw_metadata,
        }

    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]:
        result = []
        for entry in raw_metadata.get("files", []):
            result.append({
                "source": self.source_name,
                "source_asset_id": entry.get("name"),
                "source_url": None,
                "width": None,
                "height": None,
                "raw_metadata": entry,
            })
        return result

    def parse_source_tags(self, raw_metadata: dict) -> list[dict]:
        result = []
        for tag in raw_metadata.get("tags", []):
            if isinstance(tag, str) and tag.strip():
                result.append({
                    "source": self.source_name,
                    "original_name": tag.strip(),
                    "category": "general",
                })
        return result
