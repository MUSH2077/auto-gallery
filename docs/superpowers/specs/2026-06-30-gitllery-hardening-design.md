# Gitllery Hardening (v2 — Bundle B) Design

**Date:** 2026-06-30
**Status:** Approved (design); ready for implementation plan
**Branch context:** `gitlike-gallery`
**Precedes/relates:** builds on the shipped v1 `docs/superpowers/specs/2026-06-29-gitllery-curation-history-design.md`; addresses three non-blocking follow-ups from the v1 final whole-feature review.

## Summary

Three small, independent hardening changes to the already-shipped gitllery
projection, all confined to `backend/app/services/gitllery/` (+ its curation
routes). No new tables, no schema change, no API surface change except turning a
silent 200 into a 404. DB stays authoritative; `.gitllery` stays a one-way
projection.

## Decisions (locked during brainstorming)

### B1 — Eliminate query amplification (performance)
- **Problem:** `status()`, `project_pending()`, and `_pending_count_by_repo()`
  each iterate ALL `curation_commits`, and for every change call
  `_repos_for_change()`, which issues fresh DB queries per change (WorkSource
  lookup for `work`; join for `asset`; SourceCreator iteration for `creator`;
  SubscriptionSource lookup for `repository`). For N commits × M changes this is
  O(N×M) queries; the same entity (a work trashed→restored→favorited) is
  re-resolved every time.
- **Fix:** add a per-instance cache on `RepoResolver`:
  `self._change_repos_cache: dict[tuple[str, str], list[RepoDescriptor]]` keyed by
  `(subject_type, subject_id)`. `_repos_for_change()` checks the cache first and
  stores its result. `RepoResolver` is already created once per projection/status
  pass, and an entity's repo membership does not change within a pass, so the
  cache is safe. Same slicing results, far fewer queries.
- **No behavior change** — pure speedup.

### B2 — Deep blob integrity in `_integrity_ok` (correctness)
- **Problem:** `_integrity_ok()` verifies the HEAD commit object and its tree with
  `objects.verify()` (decompress + re-hash), but checks tree-referenced blobs only
  with `objects.exists()` (file presence). A corrupt-but-present blob (bit rot,
  partial write) is not detected, so `status().object_integrity_ok` can be True
  while a blob is silently corrupt.
- **Fix:** change the blob loop from `repo.objects.exists(blob_hash)` to
  `repo.objects.verify(blob_hash)`. `verify()` decompresses, re-canonicalizes, and
  re-hashes, so it fails on corrupt/tampered blobs. Cost: `status()` decompresses
  O(entities) blobs per repo — same order as the existing O(commits) walk, so
  acceptable. Makes `object_integrity_ok` a genuine three-level check
  (commit → tree → blobs).
- `ObjectStore.verify()` itself is unchanged (its decompress→re-canonicalize→re-hash
  is correct: objects are stored compressed and addressed by the uncompressed
  canonical hash, so raw-byte checksumming is not possible; corrupt zlib fails to
  decompress and tampered content re-hashes differently).

### B3 — Unknown `repository_id` returns 404 (API correctness)
- **Problem:** `GET /api/v1/curation/repositories/{repository_id}/gitllery/status`
  and `.../log` return `200` with an empty structure for an unknown
  `repository_id`, so a client cannot distinguish "no history yet" from "no such
  repository."
- **Fix:** in `GitlleryService.status(repository_id)` and
  `GitlleryService.log(repository_id)`, when `repository_id` is provided and is not
  in the known-repository set (the keys of `RepoResolver.all_repositories()`),
  `raise HTTPException(status_code=404, detail="repository not found")`. This
  matches the existing codebase convention where `CurationService` raises
  `HTTPException` from the service layer (e.g. `trash_works` raises 400/404), so no
  route-layer change is needed. The library-wide `status()` (no `repository_id`)
  is unaffected.

## Files touched

| Path | Change |
|---|---|
| `backend/app/services/gitllery/slicing.py` | B1: add `_change_repos_cache` on `RepoResolver`; cache in `_repos_for_change` |
| `backend/app/services/gitllery/service.py` | B2: `verify()` instead of `exists()` for blobs in `_integrity_ok`; B3: 404 guard in `status`/`log` (import `HTTPException`) |
| `backend/tests/test_gitllery_slicing.py` | B1 test (single-query dedup) |
| `backend/tests/test_gitllery_service.py` | B2 test (tampered blob → integrity False); B3 test (unknown id → 404) |

## Testing

- **B1:** seed one work; build several changes with the same `(subject_type,
  subject_id)`; monkeypatch/count the underlying DB lookup and assert
  `_repos_for_change` hits the DB once across repeated changes; assert the sliced
  result equals the un-cached result (correctness preserved).
- **B2:** project a commit, then overwrite one tree-referenced blob object file
  with garbage bytes; assert `_integrity_ok(repo)` is False and
  `status()`'s repo entry has `object_integrity_ok=False` (today it stays True).
- **B3:** call `status(repository_id=<random-unknown>)` and
  `log(repository_id=<random-unknown>)` → assert `HTTPException` 404; a known
  `repository_id` → 200/normal.
- Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_slicing.py tests/test_gitllery_service.py -v` (in-container path is `tests/...`).

## Global Constraints (unchanged from v1)

- DB authoritative; `.gitllery` one-way projection; `CurationService` write methods unchanged.
- No NAS absolute paths in any API response; no shell; projection best-effort and DB-read-only.
- Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.

## Out of Scope

- Bundle A (reverse rebuild `rebuild_db_from_disk`), Bundle C (semi-auto curation
  rules), Bundle D (outbox delivery) — separate sub-projects, each its own
  spec/plan.
- Full git-binary compatibility / `git log` interop.
- Changing `ObjectStore.verify()`'s semantics.

## Open Questions

None.
