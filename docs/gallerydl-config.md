# gallery-dl Extractor Configuration Reference

## Overview

auto-gallery manages per-source gallery-dl extractor settings through the admin web UI under **Settings → gallery-dl Config**. Settings are persisted to the gallery-dl `config.json` file under `extractor.<source>` sections.

This document describes every available configuration option for each supported source.

---

## Pixiv

**Status**: Fully supported (gallery-dl built-in)  
**Extractor key**: `extractor.pixiv`  
**URL patterns**: `pixiv.net/en/artworks/<id>`, `pixiv.net/users/<id>`

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

### Content Selection

| Field | UI Label | Config Key | Values | Description |
|-------|----------|------------|--------|-------------|
| `include` | Include | `extractor.twitter.include` | `timeline`, `media`, `tweets`, `likes` | What to download from a user URL. `timeline` is the standard user timeline. `media` filters to media-only tweets. `likes` requires authentication. |
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

Auth priority: username+password > cookies > unauthenticated. Unauthenticated access can only download public, non-age-restricted content.

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

## Config File Structure

All per-source settings map to gallery-dl's `config.json`:

```json
{
  "extractor": {
    "pixiv": {
      "refresh-token": "token_string",
      "cookies": "/gallerydl-config/cookies/pixiv.txt",
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
      "include": "timeline",
      "retweets": false,
      "replies": false,
      "cards": true,
      "videos": true,
      "text-tweets": false,
      "quoted": false,
      "max-posts": null,
      "directory": ["twitter", "{user[name]}"],
      "filename": "{tweet_id}_{num}.{extension}"
    },
    "iwara": {
      "username": "your_username",
      "password": "your_password",
      "cookies": "/gallerydl-config/cookies/iwara.txt",
      "format": "Source, 1080, 720",
      "directory": ["iwara", "{user[name]}"],
      "filename": "{date} {id} {title[:200]} {filename}.{extension}"
    }
  }
}
```

## Key-Value Mapping (API ↔ Config)

The admin API uses camelCase field names. The table below maps API field names to gallery-dl config keys.

### Pixiv Mapping

| API Field | Config Key |
|-----------|-----------|
| `refresh_token` | `refresh-token` |
| `cookies_path` | `cookies` |
| `filename` | `filename` |
| `directory` | `directory` (string → split to array) |
| `include` | `include` |
| `tags` | `tags` |
| `ugoira` | `ugoira` |
| `sleep_request` | `sleep-request` |
| `max_posts` | `max-posts` |

### Twitter Mapping

| API Field | Config Key |
|-----------|-----------|
| `cookies_path` | `cookies` |
| `filename` | `filename` |
| `directory` | `directory` (string → split to array) |
| `include` | `include` |
| `retweets` | `retweets` |
| `replies` | `replies` |
| `cards` | `cards` |
| `videos` | `videos` |
| `text_tweets` | `text-tweets` |
| `quoted` | `quoted` |
| `max_posts` | `max-posts` |

### Iwara Mapping

| API Field | Config Key |
|-----------|-----------|
| `cookies_path` | `cookies` |
| `username` | `username` |
| `password` | `password` |
| `filename` | `filename` |
| `directory` | `directory` (string → split to array) |
| `format` | `format` |

## Naming Template Integration

Per-source gallery-dl configs defined here provide base directory patterns. Naming Templates (**Settings → Naming Templates**) can override these per-creator:

1. Create a Naming Template with a specific `source`, `template` pattern, and optional `is_default`
2. When a download job runs, the provider's `build_gallerydl_config()` checks for a matching naming template
3. If found and `is_default=True`, the template's pattern overrides the base `directory` config

This allows per-creator file organization without changing global settings.
