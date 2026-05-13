# Danbooru Reference Constraint

## Rule

Danbooru is a reference provider only in the current phase. It must not be treated as a complete union of works or as the default media download source.

## What Danbooru CAN be used for

- Fetching and storing Danbooru artist tag reference records
- Extracting related URLs from artist pages (Pixiv, X, Iwara, personal sites)
- Suggesting `creator_link` records for admin review
- Enriching creator identity metadata
- Normalized tag reference for cross-source tag mapping (future)

## What Danbooru MUST NOT be used for

- Assuming it contains all works from any creator
- Default media download source
- Replacing direct source downloads ("just download from Danbooru instead of Pixiv")
- Canonical work records — Danbooru posts are one source among many

## Implementation boundary

- `danbooru_reference` provider: handles artist tag normalization, URL extraction, reference data
- Must NOT implement `parse_work_source` that returns downloadable works in the current phase
- Must NOT appear in `subscription_source` as a downloadable source in the current phase
- Danbooru posts as a full downloadable source is a future optional feature, not current scope

## Why

Danbooru is excellent for identity mapping (artist tags linking to source profiles) but it is a curated booru, not an archival mirror. Treating it as authoritative would create an incomplete archive and skip the actual source platforms. The system's value is archiving from original sources — Danbooru is a reference tool, not a shortcut.
