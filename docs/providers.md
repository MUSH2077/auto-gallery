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
    supports_download_cursor: bool
    supports_remote_discovery: bool
    discovery_auth_methods: tuple[str, ...]
    supports_collection_selectors: bool
```

The rollout object in the provider API response is a backend-computed effective
overlay; it is not a static `ProviderCapabilities` dataclass field.

All 8 downloadable providers (Pixiv, X, Iwara, Danbooru, Weibo, Bilibili, Pinterest, Lofter) have full `build_gallerydl_config()` implementations.

The `auto_enable_on_import` flag is per-source configurable in the gallery-dl settings page. Each source has a toggle that controls whether a newly imported subscription source defaults to `is_enabled=True`. Only Pixiv defaults to auto-enabled; all other sources default to disabled.

## Remote follow discovery

Pixiv, X, and Bilibili expose a separate `RemoteDiscoveryAdapter` contract:
`validate_account()`, `list_collections()`, `fetch_page()`, and
`build_download_auth()`. The source capability response includes
`supports_remote_discovery`, `discovery_auth_methods`,
`supports_collection_selectors`, and a backend-effective rollout object. The
admin UI never reconstructs deployment flags locally.

| Source | Status | Authentication | Collection selectors |
|---|---|---|---|
| Pixiv | Experimental | App API refresh token | public/private following |
| X | Supported when an X Developer App is configured; Cookie fallback is best-effort | OAuth 2.0 PKCE preferred; Cookie fallback | following and Lists |
| Bilibili | Experimental | `SESSDATA` | all following and following groups |

X OAuth requests exactly `users.read`, `follows.read`, `list.read`, and
`offline.access`. Register the provider redirect as the admin frontend
`/admin/discovery` URL; the frontend completes the exchange through a
query-free backend POST after synchronously scrubbing the external callback
URL. OAuth tokens are discovery-only unless a separate download
cookie is present; the Cookie fallback may break when X changes private Web
API behavior. Pixiv and Bilibili use undocumented or reverse-engineered API
surfaces and must remain marked experimental. Bilibili creator normalization
accepts `/dynamic` and `/upload/opus` so discovered identities can subscribe to
dynamic images.

Candidate confidence is explainable: a unique local identity, verified
Danbooru/cross-site link, or Pixiv illustration preview is high confidence; X
and Bilibili need two of art-focused bio, recent visual content, and a supported
site link for high confidence. One creator signal is medium and no signal is
low. Multiple local identity matches are `conflict` and are never automatically
imported. Manual import does not synchronize immediately unless selected.
Automatic import is separately opted in per account, preserves dismissed
candidates, defaults to high confidence and 25 candidates, and is capped at
1–200 per completed scan. Remote unfollow only updates candidate state and
never disables or deletes a local subscription.

### Live Pixiv work state

Pixiv work detail pages can show **live** total views, total bookmarks, and
whether the viewing user has bookmarked the illustration. Each page mount makes
a fresh Pixiv App API detail request with that viewing user's enabled, healthy
Pixiv account; the response is `private, no-store`, is not refetched on window
focus, and has no server or client cache. Local `raw_metadata` is never used as
a fallback, so an unavailable live request shows an unavailable state while the
local work page remains readable.

This endpoint is read-only: it never creates or removes a Pixiv bookmark. The
current rollout is **Pixiv manual preview only**. Keep Pixiv automatic import,
X discovery/automatic import, and Bilibili discovery/automatic import disabled.
The live state requires both the private-members foundation and the Pixiv
preview gate; if either is closed, it returns `503` while the local work page
remains available. A missing or invalid `REMOTE_CREDENTIAL_KEY`, or ciphertext
that the configured vault cannot authenticate, returns the same sanitized
`503` without contacting Pixiv or changing account/binding health. Restore the
protected key used to encrypt existing accounts rather than generating a new
one; reconnect only an affected account if its stored ciphertext is damaged.
Never copy keys, ciphertext, or decryptor diagnostics into logs or tickets.
Restore only the approved gate or gates during rollout recovery, and keep the
automatic-import and X/B gates closed.
Do not use a real provider as a smoke test; use fixture transports and injected
adapters in automated verification.

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
- Live work-state reads use the App API refresh-token account selected for the
  viewing user; this is separate from gallery-dl download authentication.

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
