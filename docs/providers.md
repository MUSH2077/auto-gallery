# Provider System

## Overview

Providers encapsulate all source-specific behavior. The rest of the application only deals with generic domain models.

## Provider Interface

Defined in `backend/app/providers/base.py`:

```python
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
    def parse_assets(
        self, raw_metadata: dict, files: list[str]
    ) -> list[dict]: ...

    @abstractmethod
    def parse_source_tags(self, raw_metadata: dict) -> list[dict]: ...
```

Where:

```python
@dataclass
class ProviderCapabilities:
    can_download: bool
    can_import_local: bool
    supports_gallerydl: bool
    supports_tags: bool
    is_reference_only: bool
```

## Provider Registry

`backend/app/providers/registry.py` maintains a dict of `source_name → provider instance`. Resolution:

```python
registry = ProviderRegistry()
provider = registry.get("pixiv")  # raises if not found
all_sources = registry.list_sources()
downloadable = registry.list_downloadable()
```

## Implemented Providers

### Pixiv (`pixiv.py`)
- **Status**: First supported downloadable provider
- `source_name`: `"pixiv"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- URL patterns: `pixiv.net/en/artworks/<id>`, `pixiv.net/artworks/<id>`, `pixiv.net/users/<id>`
- Uses gallery-dl Pixiv extractor with cookie-based auth

### Iwara (`iwara.py`)
- **Status**: Placeholder
- `capabilities.can_download`: False
- `capabilities.supports_gallerydl`: False
- URL patterns: accepted but download not implemented
- Will raise `NotImplementedError` on `build_gallerydl_config`

### X / Twitter (`x.py`)
- **Status**: Placeholder
- `capabilities.can_download`: False
- `capabilities.supports_gallerydl`: False
- URL patterns: accepted but download not implemented
- Will raise `NotImplementedError` on `build_gallerydl_config`

### Danbooru Reference (`danbooru_reference.py`)
- **Status**: Reference only
- `capabilities.is_reference_only`: True
- `capabilities.can_download`: False
- Handles: Danbooru artist tag normalization, URL extraction, creator_link suggestion
- Does NOT implement `build_gallerydl_config` or `parse_assets`

### Local Folder (`local.py`)
- **Status**: Planned
- `capabilities.can_download`: False
- `capabilities.can_import_local`: True
- Handles: directory scanning, metadata inference from file structure

### Manual Upload (`manual.py`)
- **Status**: Planned
- `capabilities.can_download`: False
- `capabilities.can_import_local`: True
- Handles: admin-uploaded files with manual metadata entry

## Adding a New Provider

1. Create `backend/app/providers/<name>.py`
2. Subclass `BaseProvider` and implement all abstract methods
3. Register in `registry.py`: `registry.register(MyProvider())`
4. Add the `source` value to the database enum (migration)
5. If gallery-dl supports the source, implement `build_gallerydl_config`
6. Add provider-specific `raw_metadata` rendering in admin-web

## Provider Design Rules

- Providers have NO database access
- `parse_*` methods receive raw metadata dicts, return plain dicts
- Provider-specific field names never appear in API responses
- Everything source-specific that doesn't fit the generic schema goes into `raw_metadata` JSONB
- Admin web may have provider-specific detail components that know how to render `raw_metadata` per source
