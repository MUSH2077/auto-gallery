---
name: provider-design
description: Use when implementing or modifying a source provider (Pixiv, X/Twitter, Iwara, Danbooru, Weibo, Bilibili, Pinterest, LOFTER) — the provider interface, source abstraction (no platform-specific names in core), URL normalize/validate, gallery-dl config, and creator/work/asset/tag parsing.
---

# Provider Design Skill

Guidance for implementing source providers. Read `.claude/constraints/source-abstraction.md` and `.claude/constraints/creator-display-name.md` before implementing any provider.

## Provider interface (base.py)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class ProviderCapabilities:
    can_download: bool
    can_import_local: bool
    supports_gallerydl: bool
    supports_tags: bool
    is_reference_only: bool

class BaseProvider(ABC):
    @property
    @abstractmethod
    def source_name(self) -> str: ...

    @property
    @abstractmethod
    def display_name(self) -> str: ...

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    def normalize_url(self, input_text: str) -> str | None: ...

    @abstractmethod
    def validate_url(self, url: str) -> bool: ...

    @abstractmethod
    def build_gallerydl_config(
        self, subscription_source, naming_template
    ) -> dict: ...

    @abstractmethod
    def parse_source_creator(self, raw_metadata: dict) -> dict: ...

    @abstractmethod
    def parse_work_source(self, raw_metadata: dict) -> dict: ...

    @abstractmethod
    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]: ...

    @abstractmethod
    def parse_source_tags(self, raw_metadata: dict) -> list[dict]: ...
```

## Adding a new provider

1. Create `backend/app/providers/<name>.py`
2. Implement `BaseProvider`
3. Register in `registry.py`
4. Add the `source` enum value in the database migration
5. Add any provider-specific gallery-dl config templates

## Pixiv provider (first downloadable provider)

- `source_name = "pixiv"`
- `capabilities.can_download = True`
- `capabilities.supports_gallerydl = True`
- `validate_url`: accept `pixiv.net/artworks/...`, `pixiv.net/users/...` (with or without `/en/` locale prefix)
- `normalize_url`: strip tracking params and `/en/` locale, normalize to `https://www.pixiv.net/artworks/<id>`
- `build_gallerydl_config`: generate per-job config with cookie/auth from GALLERYDL_CONFIG_ROOT

## Iwara and X providers (placeholders initially)

- `can_download = False`
- `supports_gallerydl = False` (until gallery-dl support is confirmed)
- `validate_url`: accept known URL patterns
- Raise `NotImplementedError` on `build_gallerydl_config` until implemented

## Danbooru reference provider (not downloadable)

- `is_reference_only = True`
- `can_download = False`
- `normalize_url`: accept Danbooru artist URLs, normalize artist tag input
- Implements artist reference fetching, URL extraction, link suggestion
- Does NOT implement `build_gallerydl_config` or `parse_assets`

## Provider return types

All `parse_*` methods return plain dicts matching the corresponding Pydantic create schemas. Never return SQLAlchemy models from providers. Providers have no database access — they only transform metadata.
