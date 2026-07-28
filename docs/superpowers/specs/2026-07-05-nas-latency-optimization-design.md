# NAS Latency Optimization (方案一) Design

**Date:** 2026-07-05
**Status:** Approved (diagnosis + approach); ready for spec review
**Branch context:** `gitlike-gallery`

## Problem (evidence-backed)

On an 8GB NAS, the frontend shows long delays/timeouts with many entries and
running tasks. Root-cause chain measured on the dev machine (NVMe + fast CPU —
the NAS amplifies everything below):

1. **`GitlleryService.status()` costs 2.3–3.4s at only 711 commits** (works
   page: 6.7ms, listTasks: 11.5ms). Cost structure:
   - `_pending_count_by_repo` walks ALL `curation_commits` issuing **one DB
     query per commit** (`_changes_for_commit` × 711, sequential);
   - per-repo `projected_db_commit_ids()` synchronously reads + zlib-inflates
     every commit object in the chain;
   - `_integrity_ok` synchronously `verify()`s (inflate + re-hash) every blob
     in every repo's HEAD tree; `_drift_keys` adds per-entity DB queries.
   - All the synchronous disk/zlib work runs **on the asyncio event loop**, and
     the backend is a **single uvicorn process** — while status runs, NO other
     request is served. One GitlleryPanel mount stalls the whole API.
   - The panel mounts on every creator detail page with `staleTime=0` and
     `refetchOnWindowFocus=true`.
2. **8GB memory pressure amplifier:** worker-import may burst to its 2GiB limit
   (pyvips), meilisearch has NO memory limit, 9 containers idle at ~0.6GB, NAS
   OS takes 1–1.5GB → swap on spinning disks during task bursts.
3. **Creator-detail request storm:** 13 parallel queries on mount; timeline +
   stats each hard-poll at 15s (not using the adaptive `lib/polling.ts`).
4. (Reliability side-find) redis runs `--maxmemory 64mb --maxmemory-policy
   allkeys-lru` while ALSO backing RQ queues — under memory pressure LRU can
   evict queued jobs silently.

## Decisions

### P1 — Make `status()` cheap, non-blocking, and cached (backend)

- **(a) Batch the DB walk.** Add a single-query loader
  `_all_changes_by_commit() -> dict[UUID, list[CurationChange]]` (one
  `select(CurationChange)` ordered by commit, grouped in Python) and use it in
  `_pending_count_by_repo` AND `project_pending` instead of per-commit
  `_changes_for_commit` calls. 711 queries → 1.
- **(b) Light mode by default.** `status(repository_id=None, deep=False)`:
  - light (default): `behind` (from the batched walk) + a cheap integrity probe
    (HEAD ref resolves and its commit object loads — one object read per repo);
    **skips** blob `verify()` and `_drift_keys`. Response gains `"deep": false`;
    `drift` is `[]`; `clean = exists and behind == 0 and integrity_ok`.
  - deep (`?deep=true`): current full behavior (blob verify + drift), for the
    manual "verify" action. `test_integrity_detects_corrupt_blob` switches to
    `status(deep=True)`.
- **(c) Move disk work off the event loop.** The per-repo disk walks
  (`projected_db_commit_ids`, integrity probe, deep verify/drift reads) run via
  `asyncio.to_thread(...)` so concurrent requests keep flowing.
- **(d) Redis cache.** Reuse `services/cache.py`: key
  `cache_key("gitllery:status", repository_id=..., deep=...)`, TTL registry
  entry `"gitllery:status": 30`. Invalidate (pattern
  `gitllery:status`) after any successful projection
  (`project_commit` with affected repos, `project_pending` with counts,
  `backfill`, `rebuild` non-dry-run) and after `reconcile`. Cache failures are
  best-effort (cache.py already degrades).
- Route: `GET /gitllery/status?deep=<bool=false>` (+ the per-repository
  variant). Schemas: `GitlleryStatusResponse` gains `deep: bool = False`.

Expected: cache hit <10ms; miss ~100–300ms; no event-loop blocking either way.

### P2 — Frontend query hygiene

- `GitlleryPanel` status query: `staleTime: 60_000`,
  `refetchOnWindowFocus: false` (reconcile/rebuild already invalidate).
- Creator detail page: `timeline` and `stats` queries switch
  `refetchInterval: 15000` → `POLL_IDLE_MS` (30s) from `@/lib/polling` and add
  `staleTime: POLL_IDLE_MS` — they are statistics, not live task state.

### P3 — 8GB NAS resource profile + redis policy

- **Base compose change (applies everywhere):** redis
  `--maxmemory-policy noeviction` and raise `--maxmemory` to `128mb` — queues
  must never be silently evicted; noeviction turns overflow into visible
  errors.
- **New override file `docker-compose.nas8g.yaml`** (documented usage:
  `docker compose -f docker-compose.yaml -f docker-compose.nas8g.yaml up -d`):
  - meilisearch: memory limit **256M** (currently unlimited)
  - worker-import: memory limit **1G** (from 2G) and command concurrency
    `imports 1` (from 2)
  - worker-download: memory limit **512M** (from 768M)
  - backend: **768M** (from 1G)
  - Totals at limits: ≈3.3G leaving headroom for postgres/redis/admin-web/OS on
    8GB.
- Bilingual note in `docs/deployment` (or the existing deployment constraint
  doc's referenced location) describing the profile.

## Out of Scope

- 方案二 (incremental materialized status counters) — revisit at ~10k commits.
- Restructuring the creator-detail page's 13-query fan-out beyond the two
  polling intervals above.
- Meilisearch/postgres internal tuning (shared_buffers etc.).

## Testing

- **P1 unit/integration (extend `tests/test_gitllery_service.py`):**
  - light default: seed + project → `status()` clean, `deep is False`,
    `drift == []`; corrupt a blob → light status still clean (probe only), but
    `status(deep=True)` reports `object_integrity_ok=False` (existing corrupt-
    blob test updated to deep).
  - batching: monkeypatch-count `_changes_for_commit` → `status()` calls it 0
    times (uses the batched loader).
  - cache: first `status()` miss → second hit returns equal payload without
    touching the DB (monkeypatch a counter on `_pending_count_by_repo`);
    projecting a new commit invalidates (third call recomputes).
- **P2:** `npm run build` green (no behavior tests).
- **P3:** `docker compose -f ... -f docker-compose.nas8g.yaml config` validates;
  services start healthy with the override locally.

## Global Constraints (unchanged)

- No NAS absolute paths in responses; `RequireAdmin`; projection stays
  best-effort and DB-read-only; commit messages end with
  `Co-Authored-By: Claude <noreply@anthropic.com>`.
