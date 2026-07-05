# Gitllery Status Checkpoint (方案二) Design

**Date:** 2026-07-05
**Status:** Approved (direction A chosen); ready for spec review
**Branch context:** `gitlike-gallery`
**Builds on:** `2026-07-05-nas-latency-optimization-design.md` (方案一 shipped: batch
loader, light/deep, to_thread, 30s cache). 方案一 left the status **miss** path
O(entire history) in both DB (loads ALL commits + ALL changes into RAM) and disk
(full chain walk per repo): 638ms at 711 commits ⇒ tens of seconds and
swap-inducing RAM at the 50k+ commits a larger NAS library will reach.

## Goal

`status()` becomes **O(repos + tail)** — independent of total history size —
where *tail* = commits not yet verified projected (steady state ≈ 0–10).

## Design (Option A — checkpoint + tail-only slicing)

### The checkpoint invariant

A single global checkpoint `P = (created_at, id)` stored in Redis (plain key
`gitllery:checkpoint`, JSON `{"created_at": iso, "id": uuid}`, best-effort via
`get_redis()`):

> **Invariant:** every `curation_commit` with position ≤ P has been verified
> present in the on-disk chain of every repo it affects.

- **Axis is `created_at` (+ `id` tiebreak), NOT `occurred_at`** — baseline
  backfill and rebuild backdate `occurred_at`, but `created_at` is set at
  insert time, so new rows can never appear "behind" P on this axis.
- P is only ever advanced by a status computation that **verified** cleanliness
  (see below), and only to positions older than `now() − 5s` (safety window
  against in-flight transactions whose rows flush early but commit late).

### status() light fast path

1. Read P. **Absent/corrupt → full walk** (existing 方案一 path) — behavior
   identical to today; if it finds `behind_total == 0`, SET P (self-healing
   bootstrap).
2. P present → load ONLY the tail: commits with `(created_at, id) > P`
   (`created_at > ts OR (created_at = ts AND id > uid)`; no new index/migration —
   a Postgres scan-count over the commits table without ORM hydration of
   history is not the bottleneck) and only their changes
   (`WHERE commit_id IN tail_ids`).
3. **Tail empty →** behind = 0 for every repo; repo list from
   `all_repositories()`; probe integrity; NO chain walks at all.
4. **Tail non-empty →** slice only the tail. Per-repo membership check walks
   the on-disk chain from HEAD backwards and **stops at the first
   `db_commit_id` ∉ tail-set**. This is exact, not heuristic: once P is set,
   every commit ≤ P is already in the chains (invariant), and anything
   projected after P was set has position > P — so from HEAD backwards the
   chain is a contiguous run of tail ids followed only by ≤ P ids. Cost:
   O(tail-per-repo + 1) object reads.
5. **Self-advance:** if the computed `behind_total == 0`, advance P to the max
   position verified (tail max, or global max on a full walk), clamped by the
   5s safety window. A missed projection (behind > 0) blocks advancement, so P
   can never move past an unprojected commit.
6. The 方案一 30s response cache stays on top (hits stay 0.3ms; misses become
   cheap).

### deep mode

`status(deep=True)` **bypasses the checkpoint** and runs the full walk + full
blob verify + drift — it is the ground-truth path. It may still advance P when
it finds everything clean.

### Checkpoint reset

`rebuild_db_from_disk` (non-dry-run) **deletes the checkpoint** where it
already invalidates the status cache: its single long transaction can flush
rows whose `created_at` predates the commit instant, which a concurrent status
could otherwise leapfrog. Ordinary curation routes are millisecond
transactions and are additionally covered by the 5s clamp. `reconcile` /
`project_pending` need no special handling (they only project; the next status
verifies and advances P).

### Side fix in scope

`RepoResolver.all_repositories()` currently issues one `WorkSource` query per
distinct `(source, source_creator_id)` pair. When `preload_work_sources()` has
run, derive the repo list from the preloaded map instead (zero extra queries).
status's fast path calls preload only when the tail is non-empty; the
tail-empty path uses `all_repositories()` with preload.

## What does NOT change

- `project_pending` / `reconcile` / `backfill` keep their full ordered walk —
  completeness is their job.
- Projection stays best-effort, DB-read-only; the cache layer, light/deep
  semantics, schemas and routes are unchanged (no API change at all).
- No new tables, no migrations; Redis loss degrades to today's behavior and
  self-heals.

## Failure modes (enumerated)

| scenario | behavior |
|---|---|
| Redis down / key lost | full walk every status (today's behavior); P re-set on first clean result |
| best-effort projection failed | commit stays in tail (> P); behind > 0; P blocked until reconcile projects it |
| catch-up appends an old missed commit after newer ones | it is a tail id; the HEAD-backwards walk still finds it before the first ≤ P id (append-only chain: post-P projections are all tail ids) |
| rebuild inserts backdated history | `created_at` axis keeps them in the tail; rebuild also resets P |
| in-flight tx commits late | 5s advancement clamp; worst case one extra tail member next pass |

## Testing (extend `tests/test_gitllery_service.py`)

- **fast path:** full walk sets P (redis key present); a new projected commit →
  next status (cache cleared) runs tail-only (monkeypatch-count
  `_all_changes_by_commit` == 0) with `behind_total == 0`, and advances P.
- **missed projection:** commit without projecting → tail path reports
  `behind == 1`, P not advanced; after `project_pending` + status → 0 and P
  advanced.
- **fallback:** delete/corrupt the key → status still correct (full walk) and
  re-sets P.
- **deep bypass:** `deep=True` uses the full loader even with P present.
- **catch-up ordering:** project c2 before c1 (existing pattern), then status —
  tail membership walk stops correctly, behind reflects only c1 until caught up.
- **rebuild reset:** after a non-dry-run rebuild the checkpoint key is gone.
- 5s clamp: unit-assert the advancement helper refuses positions younger
  than the window (no timing-sensitive sleep tests).

## Global Constraints (unchanged)

No NAS paths in responses; `RequireAdmin`; best-effort caching; commit messages
end with `Co-Authored-By: Claude <noreply@anthropic.com>`.
