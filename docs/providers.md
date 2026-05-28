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
- URL patterns: `pixiv.net/artworks/<id>`, `pixiv.net/users/<id>` (optionally with `/en/` locale prefix)
- Uses gallery-dl Pixiv extractor with cookie-based auth

### Iwara (`iwara.py`)
- **Status**: Downloadable (gallery-dl >= 1.32.0 required)

- `source_name`: `"iwara"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- URL patterns: `iwara.tv/video/<id>`, `iwara.tv/image/<id>`, `iwara.tv/profile/<username>`
- Supports auth via username/password or cookie file
- Extractors: user, user-videos, user-images, user-playlists, videos, images, playlists, favorites, followers, following, search, tag

### X / Twitter (`x.py`)
- **Status**: Downloadable (gallery-dl built-in Twitter extractor)
- `source_name`: `"x"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- URL patterns: `x.com/<user>`, `x.com/<user>/status/<id>`, `twitter.com/<user>`
- Uses gallery-dl Twitter extractor with cookie-based auth
- Extractors: timeline, tweets, media, likes, search, list, bookmark, avatar, background

### Danbooru (`danbooru.py`)
- **Status**: Downloadable
- `source_name`: `"danbooru"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- `capabilities.supports_tags`: True (5 categories: artist, character, copyright, general, meta)
- URL patterns: `danbooru.donmai.us/posts?tags=...`, `danbooru.donmai.us/artists/<id>`, `danbooru.donmai.us/pools/<id>`
- Auth via username/password or API key

### Danbooru Reference (`danbooru_reference.py`)
- **Status**: Reference only
- `source_name`: `"danbooru_reference"`
- `capabilities.is_reference_only`: True
- `capabilities.can_download`: False
- Handles: Danbooru artist tag normalization, URL extraction, creator_link suggestion
- Does NOT implement `build_gallerydl_config` or `parse_assets`

### Pinterest (`pinterest.py`)
- **Status**: Downloadable
- `source_name`: `"pinterest"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- `capabilities.supports_tags`: False
- URL patterns: `pinterest.com/pin/<id>`, `pinterest.com/<user>/pins/`, `pinterest.com/<user>/<board>/`
- Public API only — no authentication required

### Lofter (`lofter.py`)
- **Status**: Downloadable
- `source_name`: `"lofter"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- `capabilities.supports_tags`: False
- URL patterns: `<blog>.lofter.com/post/<id>`, `<blog>.lofter.com/`
- Excludes www.lofter.com (not a blog host)
- No authentication required

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
