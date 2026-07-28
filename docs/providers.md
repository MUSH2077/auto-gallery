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
    def build_gallerydl_config(self, subscription_source) -> dict: ...

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

Additional methods:

```python
    def get_creator_dir_from_url(self, url: str, source_creator_id: str) -> str: ...
```

`get_creator_dir_from_url()` maps a source URL to a filesystem-safe creator directory name. Implemented by all downloadable providers.

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

All 8 downloadable providers (Pixiv, X, Iwara, Danbooru, Weibo, Bilibili, Pinterest, Lofter) have full `build_gallerydl_config()` implementations.

The `auto_enable_on_import` flag is per-source configurable in the gallery-dl settings page. Each source has a toggle that controls whether a newly imported subscription source defaults to `is_enabled=True`. Only Pixiv defaults to auto-enabled; all other sources default to disabled.

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
- **Status**: Fully supported downloadable provider
- `source_name`: `"pixiv"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- URL patterns: `pixiv.net/artworks/<id>`, `pixiv.net/users/<id>` (optionally with `/en/` locale prefix)
- Uses gallery-dl Pixiv extractor with cookie-based auth

### X / Twitter (`x.py`)
- **Status**: Downloadable
- `source_name`: `"x"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- `capabilities.supports_tags`: True
- URL patterns: `x.com/<user>`, `x.com/<user>/status/<id>`, `twitter.com/<user>`
- Uses gallery-dl Twitter extractor with cookie-based auth (`strategy: "tweets"`)
- The SearchTimeline fallback endpoint is patched out in the Dockerfile (Twitter has deprecated it)

### Iwara (`iwara.py`)
- **Status**: Downloadable (gallery-dl >= 1.32.0 required)
- `source_name`: `"iwara"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- URL patterns: `iwara.tv/video/<id>`, `iwara.tv/image/<id>`, `iwara.tv/profile/<username>`
- Supports auth via username/password or cookie file
- Extractors: user, user-videos, user-images, user-playlists, videos, images, playlists, favorites, followers, following, search, tag

### Danbooru (`danbooru.py`)
- **Status**: Downloadable (and serves as a reference for tag metadata)
- `source_name`: `"danbooru"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- `capabilities.supports_tags`: True (5 tag categories: artist, character, copyright, general, meta)
- Downloads posts via tag search (`posts?tags=artist_name`)
- Auth via username/password or API key
- URL patterns: `danbooru.donmai.us/posts?tags=...`, `danbooru.donmai.us/artists/<id>`, `danbooru.donmai.us/pools/<id>`

### Danbooru Reference (`danbooru_reference.py`)
- **Status**: Reference only
- `source_name`: `"danbooru_reference"`
- `capabilities.is_reference_only`: True
- `capabilities.can_download`: False
- Handles: Danbooru artist tag normalization, URL extraction, creator_link suggestion
- Does NOT implement `build_gallerydl_config` or `parse_assets`

### 微博 / Weibo (`weibo.py`)
- **Status**: Downloadable
- `source_name`: `"weibo"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- `capabilities.supports_tags`: True
- URL patterns: `weibo.com/u/<uid>`, `weibo.com/<username>`, `weibo.com/<uid>/<status_id>`
- Also supports `weibo.cn` mobile domain
- Username validation rejects hyphen-prefixed names and bare prefix keywords
- Tags extracted from `#hashtag#` patterns in post text
- Cookies optional — set `data/config/gallery-dl/cookies/weibo.txt` for authenticated access
- gallery-dl rate-limit: 1.0–2.0 s between requests

### 哔哩哔哩 / Bilibili (`bilibili.py`)
- **Status**: Downloadable
- `source_name`: `"bilibili"`
- `capabilities.can_download`: True
- `capabilities.supports_gallerydl`: True
- `capabilities.supports_tags`: True
- URL patterns:
  - User articles: `space.bilibili.com/<uid>/article`
  - Single article: `bilibili.com/read/cv<id>`
  - User article favorites: `space.bilibili.com/<uid>/favlist?ftype=article`
- No authentication required for public content
- Tags extracted from article tag list
- gallery-dl rate-limit: 3.0–6.0 s between requests
- `livephoto` files downloaded by default (toggleable via config)

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
- Must use `["lofter", "{blog_name}", "{id}"]` directory pattern to avoid flat directories merging all posts into one work
- No authentication required

### Local Folder (`local.py`)
- **Status**: Planned
- `capabilities.can_download`: False
- `capabilities.can_import_local`: True
- Handles: directory scanning, metadata inference from file structure

### Manual Upload (`manual.py`)
- **Status**: Supported through the permission-gated admin upload page
- `capabilities.can_download`: False
- `capabilities.can_import_local`: True
- Handles: admin-uploaded images and archives with manual metadata, tags,
  creator targeting, quota enforcement, and attribution

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
