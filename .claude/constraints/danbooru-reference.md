# Danbooru Constraint

## Rule

Danbooru serves dual roles: a downloadable source provider AND a reference provider for creator identity mapping. It must not be treated as the complete union of works or as the default media download source.

## What Danbooru CAN be used for

- Downloading posts by artist tag search, artist ID, or pool ID (downloadable provider)
- Fetching and storing Danbooru artist tag reference records
- Extracting related URLs from artist pages (Pixiv, X, Iwara, personal sites)
- Suggesting `creator_link` records for admin review
- Enriching creator identity metadata
- Normalized tag reference for cross-source tag mapping (future)
- Favorites sync: bidirectional sync between Danbooru favorites and local creators

## What Danbooru MUST NOT be used for

- Assuming it contains all works from any creator
- Default media download source
- Replacing direct source downloads ("just download from Danbooru instead of Pixiv")
- Canonical work records — Danbooru posts are one source among many

## Implementation

- `danbooru` provider (`danbooru.py`): downloadable provider with gallery-dl support, 5 tag categories (artist/character/copyright/general/meta), AI/NSFW detection
- `danbooru_reference` provider (`danbooru_reference.py`): artist tag normalization, URL extraction, reference data
- Both registered separately with different source_name values

## Why

Danbooru is excellent for identity mapping (artist tags linking to source profiles) and a useful download source for tagged content, but it is a curated booru, not an archival mirror. Treating it as authoritative would create an incomplete archive and skip the actual source platforms.
