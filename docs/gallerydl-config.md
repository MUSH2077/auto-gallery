# gallery-dl Extractor Configuration Reference

## Overview

auto-gallery manages per-source gallery-dl extractor settings through the admin web UI under **Settings -> gallery-dl Config**. Settings are persisted to the gallery-dl `config.json` file under `extractor.<source>` sections.

This document describes every available configuration option for each supported source.

---

## Connectivity Test

Each source tab in **Settings -> gallery-dl Config** includes a **Test Connection** button. This calls `POST /api/v1/admin/gallerydl-config/test-connection` with the source name and runs gallery-dl against the current credentials to verify connectivity. Results show success/failure with a diagnostic message. Use this after updating cookies or authentication credentials to confirm they are valid before triggering a sync.

---

## auto_enable_on_import

Each source section includes an **Auto-enable on import** toggle. This setting controls whether newly imported subscription sources for that extractor default to `is_enabled=True`.

**Important**: This setting is stored in `GALLERYDL_CONFIG_ROOT/config.json` under each extractor section (e.g. `extractor.pixiv.auto-enable-on-import`), NOT in the database `system_settings` table. The backend reads this from the config file at import time.

Only Pixiv defaults to `auto_enable_on_import=true`. All other sources default to `false`.

---

## Per-Job Config Generation

When a download job is created, the provider's `build_gallerydl_config()` generates a per-job config dict. This per-job config only includes the `directory` field when a **naming template** is assigned to the subscription. When no naming template exists, the per-job config omits the directory, allowing the base `config.json` defaults to control the output path. This prevents per-job configs from overriding the admin-configured base directory patterns.

---

## Pixiv

**Status**: Fully supported (gallery-dl built-in)
**Extractor key**: `extractor.pixiv`
**URL patterns**: `pixiv.net/artworks/<id>`, `pixiv.net/users/<id>` (optionally with `/en/` locale prefix)

### Authentication

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `refresh_token` | Refresh Token | `extractor.pixiv.refresh-token` | Pixiv OAuth refresh token. The recommended auth method. Obtain via gallery-dl's `oauth:pixiv` flow. Takes precedence over cookies. |
| `cookies_path` | Cookies Path | `extractor.pixiv.cookies` | Path to a Netscape-format cookie file for pixiv.net session auth. Default: `/gallerydl-config/cookies/pixiv.txt` |

Auth priority: refresh-token > cookies > unauthenticated (rate-limited).

### Content Selection

| Field | UI Label | Config Key | Values | Description |
|-------|----------|------------|--------|-------------|
| `include` | Include | `extractor.pixiv.include` | `artworks`, `favorites`, `bookmarks` | What to download from a user URL. `artworks` downloads public illustrations and manga. `favorites`/`bookmarks` require authentication. |
| `tags` | Tag Language | `extractor.pixiv.tags` | `japanese`, `english`, `translated` | Language version of tags to include in metadata. |
| `ugoira` | Ugoira Format | `extractor.pixiv.ugoira` | `zip`, `gif` | How to handle animated ugoira. `zip`: keep original ZIP of frames. `gif`: convert to GIF animation via ffmpeg. |
| `max_posts` | Max Posts | `extractor.pixiv.max-posts` | Integer or empty | Maximum number of artworks to download per job. Empty = unlimited. |

### Metadata Options

| Field | UI Label | Config Key | Default | Description |
|-------|----------|------------|---------|-------------|
| `metadata` | Write Metadata | `extractor.pixiv.metadata` | `false` | Write per-work metadata to a JSON sidecar file alongside downloaded images. |
| `metadata_bookmark` | Bookmark Metadata | `extractor.pixiv.metadata-bookmark` | `false` | Include bookmark/collection metadata in the JSON file. Requires metadata=true. |
| `captions` | Captions | `extractor.pixiv.captions` | `false` | Download work captions/descriptions as text. |
| `comments` | Comments | `extractor.pixiv.comments` | `false` | Download comments on the work. |
| `sanity` | Artwork Age | `extractor.pixiv.sanity` | `false` | Skip "new" artworks under a certain age threshold (sanity check). |

### File Organization

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `directory` | Directory Pattern | `extractor.pixiv.directory` (array) | Output directory structure. gallery-dl format tokens supported. Default: `["pixiv", "{user[account]}", "{id}"]` |
| `filename` | Filename Pattern | `extractor.pixiv.filename` | Output filename pattern. Default: `{id}_p{num}.{extension}` |

#### Useful Tokens

| Token | Example Value |
|-------|--------------|
| `{user[id]}` | `1980643` |
| `{user[account]}` | `askzy` |
| `{user[name]}` | `ASK` |
| `{id}` | `38362603` |
| `{title}` | Work title |
| `{date}` | `2024-01-01` |
| `{num}` | Page number (1-indexed) |
| `{extension}` | `jpg`, `png`, `gif` |

### Rate Limiting

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `sleep_request` | Sleep (seconds) | `extractor.pixiv.sleep-request` | Seconds to wait between HTTP requests. Helps avoid rate limiting. Default: `0` (no delay). |

---

## X / Twitter

**Status**: Supported (gallery-dl built-in)
**Extractor key**: `extractor.twitter`
**URL patterns**: `x.com/<user>`, `twitter.com/<user>`, `x.com/<user>/status/<id>`

### Authentication

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `cookies_path` | Cookies Path | `extractor.twitter.cookies` | Path to a Netscape-format cookie file. Required for most content types (especially likes and bookmarks). Default: `/gallerydl-config/cookies/twitter.txt` |
| `cookie_content` | Cookie Content | (written to file) | Direct cookie text paste. Auto-saved to the cookies path on save. |

### API Strategy

| Field | UI Label | Config Key | Values | Default | Description |
|-------|----------|------------|--------|---------|-------------|
| `strategy` | Fetch Strategy | `extractor.twitter.strategy` | `tweets`, `media`, `with_replies` | `tweets` | Controls which GraphQL endpoint gallery-dl uses to fetch the user timeline. `tweets` uses UserTweets (recommended, actively maintained). `media` uses UserMedia (currently broken). `with_replies` includes replies in the timeline fetch. |

The deprecated SearchTimeline fallback has been removed from the container image via a Dockerfile patch. Only the configured strategy endpoint is used.

### Content Selection

| Field | UI Label | Config Key | Values | Description |
|-------|----------|------------|--------|-------------|
| `include` | Include | `extractor.twitter.include` | `timeline`, `media`, `tweets`, `likes` | What to download from a user URL. `timeline` is the standard user timeline. `media` filters to media-only tweets. `tweets` is the tweet list. `likes` requires authentication. |
| `max_posts` | Max Posts | `extractor.twitter.max-posts` | Integer or empty | Maximum tweets to download per job. Empty = unlimited. |

### Content Filters

| Field | UI Label | Config Key | Default | Description |
|-------|----------|------------|---------|-------------|
| `retweets` | Retweets | `extractor.twitter.retweets` | `false` | Include retweets made by the user. |
| `replies` | Replies | `extractor.twitter.replies` | `false` | Include replies to other users. |
| `cards` | Cards | `extractor.twitter.cards` | `true` | Download images from Twitter Cards (link previews, summary cards). |
| `videos` | Videos | `extractor.twitter.videos` | `true` | Download embedded video (MP4). |
| `text_tweets` | Text Tweets | `extractor.twitter.text-tweets` | `false` | Include text-only tweets that have no media attachments. |
| `quoted` | Quoted Tweets | `extractor.twitter.quoted` | `false` | Include media from quoted tweets. |
| `pinned` | Pinned | `extractor.twitter.pinned` | `false` | Include the user's pinned tweet even if already archived. |
| `previews` | Previews | `extractor.twitter.previews` | `false` | Download preview/thumbnail images instead of full-size. |
| `articles` | Articles | `extractor.twitter.articles` | `false` | Download Twitter Articles (long-form content). |

### File Organization

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `directory` | Directory Pattern | `extractor.twitter.directory` (array) | Default: `["twitter", "{user[name]}"]` |
| `filename` | Filename Pattern | `extractor.twitter.filename` | Default: `{tweet_id}_{num}.{extension}` |

#### Useful Tokens

| Token | Example Value |
|-------|--------------|
| `{user[id]}` | Numeric user ID |
| `{user[name]}` | `@username` |
| `{tweet_id}` | `1234567890123456789` |
| `{date}` | `2024-01-01` |
| `{num}` | Media number within tweet |

---

## Iwara

**Status**: Supported (requires gallery-dl >= 1.32.0)
**Extractor key**: `extractor.iwara`
**URL patterns**: `iwara.tv/video/<id>`, `iwara.tv/image/<id>`, `iwara.tv/profile/<username>`

### Authentication

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `username` | Username | `extractor.iwara.username` | Iwara account username or email. Required with password for accessing favorites and age-restricted content. |
| `password` | Password | `extractor.iwara.password` | Iwara account password. Stored in config.json (plaintext). Use cookie auth if this is a concern. |
| `cookies_path` | Cookies Path | `extractor.iwara.cookies` | Path to a Netscape-format cookie file. Alternative to username/password auth. Default: `/gallerydl-config/cookies/iwara.txt` |
| `cookie_content` | Cookie Content | (written to file) | Direct cookie text paste. Auto-saved to the cookies path on save. |

Auth priority: username+password > cookies > unauthenticated. Unauthenticated access can only download public, non-age-restricted content.

### Content Selection

| Field | UI Label | Config Key | Values | Description |
|-------|----------|------------|--------|-------------|
| `include` | Include | `extractor.iwara.include` | `user-images`, `user-videos`, `user-playlists` | Which content types to download from a user profile. |

### Video Quality

| Field | UI Label | Config Key | Default | Description |
|-------|----------|------------|---------|-------------|
| `format` | Format | `extractor.iwara.format` | none | Comma-separated format preference list. gallery-dl tries each format in order and uses the first available. Example: `"Source, 1080, 720, 540, 360"`. `"Source"` selects the original uploaded quality. |

Common format values: `Source`, `1080`, `720`, `540`, `360`. If left empty, gallery-dl uses the default (first available format).

### File Organization

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `directory` | Directory Pattern | `extractor.iwara.directory` (array) | Default: `["iwara", "{user[name]}"]` |
| `filename` | Filename Pattern | `extractor.iwara.filename` | Default: `{date} {id} {title[:200]} {filename}.{extension}` |

#### Useful Tokens

| Token | Example Value |
|-------|--------------|
| `{user[id]}` | Numeric user ID |
| `{user[name]}` | Display name |
| `{id}` | Video/Image ID |
| `{title[:200]}` | Title (truncated to 200 chars) |
| `{date}` | Upload date |
| `{filename}` | Original filename |
| `{type}` | `video` or `image` |

---

## Danbooru

**Status**: Supported (gallery-dl built-in)
**Extractor key**: `extractor.danbooru`
**URL patterns**: `danbooru.donmai.us/posts?tags=<tags>`, `danbooru.donmai.us/artists/<id>`, `danbooru.donmai.us/pools/<id>`

Danbooru serves two roles in auto-gallery:
1. **Download provider** -- downloads posts by tag search (typically artist tag)
2. **Reference provider** -- artist identity mapping, URL extraction, creator link suggestions (separate `danbooru_reference` provider)

### Authentication

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `username` | Username | `extractor.danbooru.username` | Danbooru account username. Required for API access beyond basic browsing. |
| `password` | Password | `extractor.danbooru.password` | Danbooru account password (or API key, depending on Danbooru instance). |
| `api_key` | API Key | `extractor.danbooru.api-key` | Danbooru API key (Danbooru v2+). Alternative to password-based auth. |
| `cookies_path` | Cookies Path | `extractor.danbooru.cookies` | Path to a Netscape-format cookie file. Default: `/gallerydl-config/cookies/danbooru.txt` |
| `cookie_content` | Cookie Content | (written to file) | Direct cookie text paste. Auto-saved to the cookies path on save. |

### Metadata Options

| Field | UI Label | Config Key | Default | Description |
|-------|----------|------------|---------|-------------|
| `external` | External Source | `extractor.danbooru.external` | `false` | Follow `source` field to download from the original external URL instead of Danbooru's CDN. Useful for getting original-quality files. |
| `metadata` | Write Metadata | `extractor.danbooru.metadata` | `false` | Write per-post metadata JSON alongside downloaded files. |

### File Organization

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `directory` | Directory Pattern | `extractor.danbooru.directory` (array) | Default: `["danbooru", "{artist[name]}"]` |
| `filename` | Filename Pattern | `extractor.danbooru.filename` | Default: `{id}_{num}.{extension}` |

#### Useful Tokens

| Token | Example Value |
|-------|--------------|
| `{id}` | Post ID (e.g. `1234567`) |
| `{artist[name]}` | First artist tag |
| `{tag_string_artist}` | All artist tags, space-separated |
| `{tag_string_character}` | Character tags |
| `{tag_string_copyright}` | Copyright/series tags |
| `{tag_string_general}` | General tags |
| `{width}`, `{height}` | Image dimensions |
| `{file_size}` | File size in bytes |
| `{rating}` | `s`, `q`, or `e` |
| `{score}` | Community score |
| `{date}` | Upload date |
| `{num}` | Media number (for posts with multiple files) |

---

## Pinterest

**Status**: Supported (gallery-dl built-in)
**Extractor key**: `extractor.pinterest`
**URL patterns**: `pinterest.com/pin/<id>`, `pinterest.com/<username>/pins/`, `pinterest.com/<username>/<board>/`

### Authentication

No authentication required. Pinterest content is publicly accessible. No cookies or login needed.

### Domain

| Field | UI Label | Config Key | Default | Description |
|-------|----------|------------|---------|-------------|
| `domain` | Domain | `extractor.pinterest.domain` | auto | Pinterest regional domain to use (e.g. `pinterest.com`, `pinterest.de`, `pinterest.jp`). Leave empty for automatic detection based on the URL. |

### Content Filters

| Field | UI Label | Config Key | Default | Description |
|-------|----------|------------|---------|-------------|
| `stories` | Stories | `extractor.pinterest.stories` | `true` | Download story pins (galleries with multiple images). |
| `videos` | Videos | `extractor.pinterest.videos` | `true` | Download video pins. |
| `sections` | Sections | `extractor.pinterest.sections` | `true` | Follow board sections when downloading an entire board. |

### File Organization

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `directory` | Directory Pattern | `extractor.pinterest.directory` (array) | Default: `["pinterest", "{category}", "{user}", "{board[name]}"]` |
| `filename` | Filename Pattern | `extractor.pinterest.filename` | Default: `{id}_{num}.{extension}` |

#### Useful Tokens

| Token | Example Value |
|-------|--------------|
| `{user}` | Pinterest username |
| `{board[name]}` | Board name |
| `{id}` | Pin ID |
| `{category}` | Pin category |
| `{title}` | Pin title/description |
| `{num}` | Image number within pin |
| `{extension}` | `jpg`, `png`, `mp4` |

---

## LOFTER

**Status**: Supported (gallery-dl built-in)
**Extractor key**: `extractor.lofter`
**URL patterns**: `<blog>.lofter.com/post/<id>`, `<blog>.lofter.com/`

### Authentication

No authentication required. LOFTER blog content is publicly accessible.

**Important**: The directory pattern must use the `["lofter", "{blog_name}", "{id}"]` structure. Using a flat directory (without `{id}`) causes all posts to merge into a single work.

### File Organization

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `directory` | Directory Pattern | `extractor.lofter.directory` (array) | Default: `["lofter", "{blog_name}", "{id}"]` -- The `{id}` subdirectory is critical for per-work separation. |
| `filename` | Filename Pattern | `extractor.lofter.filename` | Default: `{id}_{num}.{extension}` |

#### Useful Tokens

| Token | Example Value |
|-------|--------------|
| `{blog_name}` | Blog subdomain name |
| `{id}` | Post ID |
| `{title}` | Post title |
| `{date}` | Post date |
| `{num}` | Image number within post |
| `{extension}` | `jpg`, `png`, `gif` |

---

## Weibo

**Status**: Supported (gallery-dl built-in)
**Extractor key**: `extractor.weibo`
**URL patterns**: `weibo.com/<uid>`, `weibo.com/u/<uid>`, `weibo.com/n/<name>`, `weibo.com/detail/<id>`, `m.weibo.cn/<uid>`

### Authentication

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `cookies_path` | Cookies Path | `extractor.weibo.cookies` | Path to a Netscape-format cookie file. Required for most content types. Default: `/gallerydl-config/cookies/weibo.txt` |
| `cookie_content` | Cookie Content | (written to file) | Direct cookie text paste. Auto-saved to the cookies path on save. |

### Content Filters

| Field | UI Label | Config Key | Default | Description |
|-------|----------|------------|---------|-------------|
| `videos` | Videos | `extractor.weibo.videos` | `true` | Download video posts. |
| `retweets` | Retweets | `extractor.weibo.retweets` | `false` | Include retweeted/forwarded posts. |
| `gifs` | GIFs | `extractor.weibo.gifs` | `true` | Download animated GIF posts. |
| `livephoto` | Live Photo | `extractor.weibo.livephoto` | `false` | Download live photo (short video clip) posts. |
| `movies` | Movies | `extractor.weibo.movies` | `false` | Download full movie/long-form video posts. |
| `text` | Text Posts | `extractor.weibo.text` | `false` | Include text-only posts that have no media attachments. |

### File Organization

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `directory` | Directory Pattern | `extractor.weibo.directory` (array) | Default: `["weibo", "{user[screen_name]}"]` |
| `filename` | Filename Pattern | `extractor.weibo.filename` | Default: `{id}_{num}.{extension}` |

#### Useful Tokens

| Token | Example Value |
|-------|--------------|
| `{user[id]}` | Numeric user ID |
| `{user[screen_name]}` | User display name |
| `{id}` | Status/post ID |
| `{date}` | Post date |
| `{num}` | Media number within post |
| `{extension}` | `jpg`, `png`, `gif`, `mp4` |

---

## Bilibili

**Status**: Supported (gallery-dl built-in)
**Extractor key**: `extractor.bilibili`
**URL patterns**: `space.bilibili.com/<uid>/article`, `bilibili.com/read/cv<id>`, `space.bilibili.com/<uid>/favlist?ftype=article`

### Authentication

No authentication required for public articles and user article pages.

### Content Options

| Field | UI Label | Config Key | Default | Description |
|-------|----------|------------|---------|-------------|
| `livephoto` | Live Photo | `extractor.bilibili.livephoto` | `true` | Download live photo files (short video clips attached to some articles). Set to `false` to skip. |

### File Organization

| Field | UI Label | Config Key | Description |
|-------|----------|------------|-------------|
| `directory` | Directory Pattern | `extractor.bilibili.directory` (array) | Default: `["bilibili", "{user[name]}", "{id}"]` |
| `filename` | Filename Pattern | `extractor.bilibili.filename` | Default: `{id}_{num}.{extension}` |

#### Useful Tokens

| Token | Example Value |
|-------|--------------|
| `{user[id]}` | Numeric UID |
| `{user[name]}` | Display name |
| `{id}` | Article ID (e.g. `12345678`) |
| `{title}` | Article title |
| `{date}` | Publication date |
| `{num}` | Image number within article |
| `{extension}` | `jpg`, `png`, `gif` |

### Rate Limiting

| Field | UI Label | Config Key | Default | Description |
|-------|----------|------------|---------|-------------|
| `sleep_request` | Sleep (seconds) | `extractor.bilibili.sleep-request` | `"3.0-6.0"` | Seconds to wait between HTTP requests. Range string supported (e.g. `"3.0-6.0"` picks a random delay in that interval). |

---

## Config File Structure

All per-source settings map to gallery-dl's `config.json`:

```json
{
  "extractor": {
    "pixiv": {
      "refresh-token": "token_string",
      "cookies": "/gallerydl-config/cookies/pixiv.txt",
      "auto-enable-on-import": true,
      "include": "artworks",
      "tags": "japanese",
      "ugoira": "zip",
      "max-posts": null,
      "sleep-request": 2.5,
      "directory": ["pixiv", "{user[account]}", "{id}"],
      "filename": "{id}_p{num}.{extension}"
    },
    "twitter": {
      "cookies": "/gallerydl-config/cookies/twitter.txt",
      "auto-enable-on-import": false,
      "strategy": "tweets",
      "include": "timeline",
      "retweets": false,
      "replies": false,
      "cards": true,
      "videos": true,
      "text-tweets": false,
      "quoted": false,
      "pinned": false,
      "previews": false,
      "articles": false,
      "max-posts": null,
      "directory": ["twitter", "{user[name]}"],
      "filename": "{tweet_id}_{num}.{extension}"
    },
    "iwara": {
      "username": "your_username",
      "password": "your_password",
      "cookies": "/gallerydl-config/cookies/iwara.txt",
      "auto-enable-on-import": false,
      "include": "user-videos",
      "format": "Source, 1080, 720",
      "directory": ["iwara", "{user[name]}"],
      "filename": "{date} {id} {title[:200]} {filename}.{extension}"
    },
    "danbooru": {
      "username": null,
      "password": null,
      "api-key": null,
      "cookies": "/gallerydl-config/cookies/danbooru.txt",
      "auto-enable-on-import": false,
      "external": false,
      "metadata": false,
      "directory": ["danbooru", "{artist[name]}"],
      "filename": "{id}_{num}.{extension}"
    },
    "pinterest": {
      "auto-enable-on-import": false,
      "domain": null,
      "stories": true,
      "videos": true,
      "sections": true,
      "directory": ["pinterest", "{category}", "{user}", "{board[name]}"],
      "filename": "{id}_{num}.{extension}"
    },
    "lofter": {
      "auto-enable-on-import": false,
      "directory": ["lofter", "{blog_name}", "{id}"],
      "filename": "{id}_{num}.{extension}"
    },
    "weibo": {
      "cookies": "/gallerydl-config/cookies/weibo.txt",
      "auto-enable-on-import": false,
      "videos": true,
      "retweets": false,
      "gifs": true,
      "livephoto": false,
      "movies": false,
      "text": false,
      "directory": ["weibo", "{user[screen_name]}"],
      "filename": "{id}_{num}.{extension}"
    },
    "bilibili": {
      "auto-enable-on-import": false,
      "livephoto": true,
      "sleep-request": "3.0-6.0",
      "directory": ["bilibili", "{user[name]}", "{id}"],
      "filename": "{id}_{num}.{extension}"
    }
  }
}
```

## Key-Value Mapping (API <-> Config)

The admin API uses camelCase field names. The table below maps API field names to gallery-dl config keys.

### Pixiv Mapping

| API Field | Config Key |
|-----------|-----------|
| `refresh_token` | `refresh-token` |
| `cookies_path` | `cookies` |
| `cookie_content` | (written to cookies file) |
| `auto_enable_on_import` | `auto-enable-on-import` |
| `filename` | `filename` |
| `directory` | `directory` (string -> split to array) |
| `include` | `include` |
| `tags` | `tags` |
| `ugoira` | `ugoira` |
| `sleep_request` | `sleep-request` |
| `max_posts` | `max-posts` |
| `metadata` | `metadata` |
| `metadata_bookmark` | `metadata-bookmark` |
| `captions` | `captions` |
| `comments` | `comments` |
| `sanity` | `sanity` |

### Twitter Mapping

| API Field | Config Key |
|-----------|-----------|
| `cookies_path` | `cookies` |
| `cookie_content` | (written to cookies file) |
| `auto_enable_on_import` | `auto-enable-on-import` |
| `filename` | `filename` |
| `directory` | `directory` (string -> split to array) |
| `strategy` | `strategy` |
| `include` | `include` |
| `retweets` | `retweets` |
| `replies` | `replies` |
| `cards` | `cards` |
| `videos` | `videos` |
| `text_tweets` | `text-tweets` |
| `quoted` | `quoted` |
| `pinned` | `pinned` |
| `previews` | `previews` |
| `articles` | `articles` |
| `max_posts` | `max-posts` |

### Iwara Mapping

| API Field | Config Key |
|-----------|-----------|
| `cookies_path` | `cookies` |
| `cookie_content` | (written to cookies file) |
| `auto_enable_on_import` | `auto-enable-on-import` |
| `username` | `username` |
| `password` | `password` |
| `filename` | `filename` |
| `directory` | `directory` (string -> split to array) |
| `include` | `include` |
| `format` | `format` |

### Danbooru Mapping

| API Field | Config Key |
|-----------|-----------|
| `username` | `username` |
| `password` | `password` |
| `api_key` | `api-key` |
| `cookies_path` | `cookies` |
| `cookie_content` | (written to cookies file) |
| `auto_enable_on_import` | `auto-enable-on-import` |
| `external` | `external` |
| `metadata` | `metadata` |
| `filename` | `filename` |
| `directory` | `directory` (string -> split to array) |

### Pinterest Mapping

| API Field | Config Key |
|-----------|-----------|
| `auto_enable_on_import` | `auto-enable-on-import` |
| `domain` | `domain` |
| `stories` | `stories` |
| `videos` | `videos` |
| `sections` | `sections` |
| `filename` | `filename` |
| `directory` | `directory` (string -> split to array) |

### Lofter Mapping

| API Field | Config Key |
|-----------|-----------|
| `auto_enable_on_import` | `auto-enable-on-import` |
| `filename` | `filename` |
| `directory` | `directory` (string -> split to array) |

### Weibo Mapping

| API Field | Config Key |
|-----------|-----------|
| `cookies_path` | `cookies` |
| `cookie_content` | (written to cookies file) |
| `auto_enable_on_import` | `auto-enable-on-import` |
| `videos` | `videos` |
| `retweets` | `retweets` |
| `gifs` | `gifs` |
| `livephoto` | `livephoto` |
| `movies` | `movies` |
| `text` | `text` |
| `filename` | `filename` |
| `directory` | `directory` (string -> split to array) |

### Bilibili Mapping

| API Field | Config Key |
|-----------|-----------|
| `auto_enable_on_import` | `auto-enable-on-import` |
| `livephoto` | `livephoto` |
| `filename` | `filename` |
| `directory` | `directory` (string -> split to array) |
| `sleep_request` | `sleep-request` |

## Naming Template Integration

Per-source gallery-dl configs defined here provide base directory patterns. Naming Templates (**Settings -> Naming Templates**) can override these per-creator:

1. Create a Naming Template with a specific `source`, `template` pattern, and optional `is_default`
2. When a download job runs, the provider's `build_gallerydl_config()` checks for a matching naming template
3. If found and `is_default=True`, the template's pattern overrides the base `directory` config

This allows per-creator file organization without changing global settings.
