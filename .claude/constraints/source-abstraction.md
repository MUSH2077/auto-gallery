# Source Abstraction Constraint

## Rule

All core domain models MUST be source-agnostic. Pixiv-specific model names must never appear in the database schema, shared Pydantic schemas, or API responses.

## Enforced model naming

Use these names, never Pixiv-specific equivalents:

| Correct | Forbidden |
|---|---|
| `creator` | `pixiv_user` |
| `source_creator` | `pixiv_user` |
| `work` | `pixiv_illust` |
| `asset` | `pixiv_image` |
| `work_source` | `pixiv_illust_source` |
| `asset_source` | `pixiv_image_source` |
| `tag` | `pixiv_tag` |

## Where provider-specific code lives

- `backend/app/providers/base.py` — abstract provider interface
- `backend/app/providers/registry.py` — provider lookup
- `backend/app/providers/pixiv.py` — Pixiv implementation
- `backend/app/providers/iwara.py` — Iwara implementation
- `backend/app/providers/x.py` — X/Twitter implementation
- `backend/app/providers/danbooru_reference.py` — Danbooru reference
- `backend/app/providers/local.py` — local folder import
- `backend/app/providers/manual.py` — manual upload

Provider modules may contain provider-specific logic, but their public interface must return generic domain objects. No Pixiv-specific field names leak into API responses.

## Provider interface

Every provider must implement:

- `source_name: str`
- `display_name: str`
- `capabilities: ProviderCapabilities`
- `normalize_url(input_text: str) -> str | None`
- `validate_url(url: str) -> bool`
- `build_gallerydl_config(subscription_source, naming_template) -> dict`
- `parse_source_creator(raw_metadata: dict) -> SourceCreatorData`
- `parse_work_source(raw_metadata: dict) -> WorkSourceData`
- `parse_assets(raw_metadata: dict, files: list[str]) -> list[AssetData]`
- `parse_source_tags(raw_metadata: dict) -> list[SourceTagData]`

## Why

Hard-coding to Pixiv would make adding Iwara, X, and other sources require schema migrations, API breakage, and rewrites. The generic model is the foundation that makes this a multi-source system.
