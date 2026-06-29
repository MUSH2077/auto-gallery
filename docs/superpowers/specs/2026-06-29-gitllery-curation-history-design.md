# Gitllery — On-Disk Curation History Design

**Date:** 2026-06-29
**Status:** Approved (design); ready for implementation plan
**Branch context:** `gitlike-gallery`

## Summary

The curation history already exists as a git-like commit DAG in PostgreSQL
(`curation_commits`, `curation_changes`, `work_curation_states`,
`creator_curation_states`, `asset_storage_states`). This project gives that DAG a
durable, portable, git-isomorphic home on disk: a per-source-creator `.gitllery/`
directory that mirrors the database and lives alongside the library files it
describes.

`.gitllery` is a **one-way projection** of the authoritative database. It is
created on a subscription source's first sync, written after every curation
commit, and inspectable like a real git repository.

## Decisions (locked during brainstorming)

1. **Source of truth:** PostgreSQL stays authoritative. `.gitllery` is a
   mirror/projection. `CurationService` write paths are NOT rewritten.
2. **Repository boundary:** one repo per source-creator, rooted at
   `LIBRARY_ROOT/{source}/{creator_dir}/.gitllery/`. This matches the existing
   "repository = `subscription_source`" notion and the on-disk library layout.
3. **Fidelity:** git-isomorphic layout (objects / refs / HEAD / index / logs)
   with JSON payloads and sha256 content addressing. NOT git-binary-compatible
   (no SHA-1, no git tree/commit wire format — git trees model filesystem trees,
   not curation DAGs).
4. **v1 read-back scope:** write path + projection + a `status`/verify
   (drift detection) + `reconcile` (catch-up). Full DB rebuild from disk is **v2**.

## Architecture

Approach: **idempotent projector** (`GitlleryService`).

```
route/job: await svc.trash_works(...) ; await db.commit()   # unchanged
           ↓ best-effort (try/except → log; never blocks the user action)
           await GitlleryService(db).project_commit(commit_id)
```

Rejected alternatives:
- **In-transaction synchronous write** inside `CurationService`: couples every
  write method, and filesystem/Postgres cannot be made atomic (no 2PC). Drift on
  crash between `db.commit()` and disk write is unrecoverable without the catch-up
  path we already need. Rejected.
- **Outbox table + worker**: most robust (at-least-once delivery) but adds a
  table and worker plumbing. The idempotent catch-up in the chosen approach
  already covers failures. Noted as a v2 hardening, not v1.

Content addressing + idempotency are what make best-effort projection safe:
re-projecting an already-projected commit is a no-op, and a missed projection is
detected by `status` and repaired by `reconcile`.

## On-Disk Layout

One repository per source-creator:

```
LIBRARY_ROOT/{source}/{creator_dir}/.gitllery/
├── HEAD                      # "ref: refs/heads/main"
├── config.json              # repository identity + schema_version
├── description              # human label, e.g. "pixiv / 七诗 curation history"
├── refs/
│   └── heads/main           # → latest commit sha256 for THIS repo
├── objects/
│   └── {aa}/{bbbb…}         # zlib(canonical JSON), sha256-addressed (git-style shard)
├── logs/
│   └── HEAD                 # reflog: append-only old→new, actor, timestamp, message
└── index.json               # materialized current worktree (status cache)
```

`config.json` (self-describing, so a v2 rebuild can read it back):

```json
{
  "schema_version": 1,
  "repository_id": "<subscription_source uuid>",
  "source": "pixiv",
  "creator_id": "<creator uuid>",
  "source_creator_id": "<platform id>",
  "creator_dir": "七诗",
  "object_hash": "sha256",
  "compression": "zlib"
}
```

The folder name `.gitllery` is configurable (single constant) but defaults as shown.

## Object Model (git → gitllery)

| git | gitllery | content |
|---|---|---|
| blob (file content) | entity-state blob | snapshot of one entity's curation state: a Work's `WorkCurationState`, a Creator's `CreatorCurationState`, or an Asset's `AssetStorageState`, serialized as canonical JSON |
| tree (dir → hash) | state tree | the repo's full worktree at a point in time: a manifest of `work/{id}→blobhash`, `creator/{id}→blobhash`, `asset/{id}→blobhash` |
| commit | curation commit | points to a tree + parent commit (**repo-local** parent chain); actor as author/committer; message; plus curation fields: `trigger`, `dedupe_key`, `reverts`, `stats`, `db_commit_id`, inline `changes[]` |
| refs/heads/main | branch | this repo's curation-timeline HEAD commit hash |
| logs/HEAD | reflog | one line per ref movement — natively an audit trail |
| index | worktree cache | materialization of the latest tree; `status` diffs it against the DB |

**Object addressing:** `sha256(canonical_json_bytes)` where canonical JSON has
sorted keys and UTF-8 encoding. Stored at `objects/{hash[:2]}/{hash[2:]}`, content
zlib-compressed (faithful to git's loose-object model). Raw bytes are not directly
human-readable; inspection is via the `log`/`cat` API and the admin-web panel —
exactly git's `git log` model.

## Slicing the Global DB DAG into Per-Repo Branches

⚠️ This is the subtlest part of the design.

The DB `curation_commits` chain is **global**: `_latest_commit_id()` returns the
single most-recent commit, and `parent_commit_id` chains every commit across all
repositories. On disk, repositories are per-source-creator. Projection therefore
**fans out and slices**:

- A DB commit maps to affected repositories via its `CurationChange.subject_*`:
  - `subject_type=work` → resolve the work's source_creator → `(source, creator_dir)`
  - `subject_type=creator` → a creator may span multiple source_creators → **multiple repos**
  - `subject_type=asset` → resolve via asset → work → source_creator
  - `subject_type=repository` → `subject_id` is the subscription_source → direct hit
- One DB commit can fan out to several repositories. Each repository receives only
  the **subset of changes** relevant to it. The on-disk commit's parent pointer is
  the **previous commit in that repository** (not the global parent), and the object
  retains `db_commit_id` for cross-reference and v2 rebuild.

## Write / Projection Flow

Single projection entry point: a new `GitlleryService`. `CurationService` is not
modified; callers invoke the projector after their existing `db.commit()`.

`project_commit(commit_id)` — idempotent:
1. Load the commit and its changes; slice into affected repositories (see above).
2. For each repository: ensure `.gitllery` is initialized (lazily create HEAD,
   refs, config.json, objects/).
3. Write the needed entity-state blobs → write the tree → write the commit object.
   All writes are atomic (`tmp` file + `os.rename`); existing objects are skipped
   (idempotent).
4. CAS-update `refs/heads/main`, append to `logs/HEAD`, refresh `index.json`.

Three create/catch-up triggers:
- **First-sync create:** when a subscription_source first produces an import commit,
  the `record_imported_work` path (which already carries `repository_id`) lazily
  initializes the repo.
- **Catch-up:** `project_pending()` scans for commits that affect a repository but
  are not yet in its `refs` and projects them in order. This is both the crash-recovery
  path and the `reconcile` repair action.
- **Backfill:** a one-time projection of the existing global DAG into all repos,
  reusing the entity enumeration from the existing `run_backfill`.

Concurrency: projection per repository is serialized with the existing async
`redis_lock()` keyed by `repository_id`.

## status / Verify (v1 — no reverse rebuild)

`GitlleryService.status(repository_id | all)` performs a git fsck/status-style
comparison and returns a structured result (never raw NAS absolute paths):

| check | meaning |
|---|---|
| `behind` | count of commits in DB that affect this repo but are not yet projected (→ one-click catch-up) |
| `missing_repos` | source-creators that have works but no `.gitllery` yet |
| `object_integrity` | sample objects: `sha256(content) == filename`, refs resolve, tree-referenced blobs exist |
| `worktree_drift` | `index.json` current state vs DB state tables (visibility/trash/purge), per entity |

`reconcile()` = run `project_pending()` + rebuild the index of any drifted repo.

**v2 (not built now):** `rebuild_db_from_disk()` — reconstruct
`curation_commits` / `curation_changes` + state tables from disk. The design already
reserves `db_commit_id` and a self-describing `config.json` for this.

## API + Frontend

Backend (all `RequireAdmin`, mounted under the existing curation router):
- `GET  /api/v1/curation/repositories/{repository_id}/gitllery/status`
- `GET  /api/v1/curation/gitllery/status` (library-wide summary)
- `POST /api/v1/curation/gitllery/reconcile` (optional repository_id)
- `POST /api/v1/curation/gitllery/backfill` (project existing DAG)
- `GET  /api/v1/curation/repositories/{repository_id}/gitllery/log` (render reflog/commits)

Frontend: reuse the existing `RepositoryGraphResponse` (commit DAG visualization).
Add a "Curation History / Gitllery" panel on the creator/repository detail page:
status badge (clean / behind N / drift) + reconcile button + commit timeline. Use
the already-tokenized semantic colors.

## Security / Constraints (per CLAUDE.md)

- **Path containment:** all `.gitllery` paths validated with `Path.relative_to()`
  (reuse `curation.py:_safe_path`), never string-prefix matching.
- **No NAS path leakage:** APIs return only hashes, relative paths, and counts —
  never `/library/...` absolute paths.
- **No shell:** pure Python file IO; never shells out to `git`.
- **Atomic writes:** `tmp` + `os.rename`; projection serialized per `repository_id`
  via `redis_lock()`.
- **Auth:** every endpoint requires `RequireAdmin`.
- **Non-blocking:** projection is best-effort (mirrors the landed account-event
  pattern: try/except → log; a user action never fails because projection failed).

## Testing

- **Unit:**
  - canonical-JSON hash determinism (same input → same hash; key order irrelevant)
  - object store round-trip (write → read → zlib-inflate → equal)
  - slicing logic (work/creator/asset/repository → correct repo set; a creator
    fans out to multiple repos)
  - reflog append format
- **Integration:**
  - trash a work → project → `status` is clean
  - simulate projector raising → user action still succeeds → `project_pending()`
    catches up → `status` is clean
  - backfill of existing data → `status` is clean
- Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest backend/tests/test_gitllery*.py -v`

## Out of Scope (v1)

- Reverse rebuild of the DB from `.gitllery` (v2).
- Outbox table + worker delivery (v2 hardening).
- Full git-binary compatibility / `git log` interop.
- Branching/merging semantics beyond the single `main` per repo.

## Open Questions

None. All four framing decisions are locked (see Decisions).
