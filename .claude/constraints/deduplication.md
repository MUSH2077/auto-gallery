# Deduplication Constraint

## Rule

Strong deduplication must be configurable and defaults to OFF. The system must never auto-delete or auto-merge without explicit admin opt-in.

## Default behavior (strong dedup OFF)

- All downloads proceed regardless of cross-source similarity
- SHA-256 hashes are calculated and stored for every asset
- Perceptual hashes (pHash) are calculated where feasible
- No files are auto-deleted
- No works are auto-merged
- Duplicate/merge candidates are created for admin review where useful

## Opt-in behaviors

### Source-level dedup (per-source)
When enabled for a specific source:
- `(source, source_work_id)` already exists → skip download job creation
- `(source, source_asset_id)` already exists → link to existing asset record via `asset_sources`
- SHA-256 identical assets → reuse existing `asset` record, add new `asset_source`

### Cross-source dedup (per-system config)
When explicitly enabled:
- SHA-256 identical files across sources → link via `asset_sources`, skip file write
- Near-duplicate pHash → create merge candidate for admin review
- Must NOT auto-merge without admin action

## What must never happen automatically

- Deleting files based on similarity
- Merging works across sources without human review
- Skipping downloads because a "similar enough" work exists

## Settings schema

Deduplication configuration lives in system settings:
- `dedup.source_level_enabled: bool = False`
- `dedup.cross_source_enabled: bool = False`
- `dedup.auto_merge: bool = False`
- `dedup.phash_threshold: int = 8` (hamming distance)

## Why

Media archives are lossy by nature — different sources host different resolutions, crops, metadata. Auto-dedup that deletes "duplicates" can silently discard the best available version or unique metadata. This system prioritizes completeness over storage efficiency by default, with admin-controlled dedup as an informed choice.
