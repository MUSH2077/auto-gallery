# Gitllery Reverse Rebuild (v2 — Bundle A) Design

**Date:** 2026-06-30
**Status:** Approved (design); ready for implementation plan
**Branch context:** `gitlike-gallery`
**Relates:** the marquee v2 of `docs/superpowers/specs/2026-06-29-gitllery-curation-history-design.md`
(which reserved `db_commit_id` + a self-describing `config.json` for exactly this).

## Summary

`rebuild_db_from_disk` is the one place gitllery **writes to the database** (v1
projection is DB-read-only; this is the inverse direction). Target scenario
(chosen: A1): after a DB loss, `disk-import` re-creates the entities
(creators/works/work_sources) with **new UUIDs** and re-records equivalent
baseline/import curation commits. Bundle A then restores the operator's
**manual** curation — both the final state and the manual commit history — from
`.gitllery`, re-mapping the old entity UUIDs to the current ones by natural keys.
It replays only manual commits (baseline/import are skipped — disk-import already
regenerated them). Transactional, idempotent, never deletes existing data.

## Decisions (locked during brainstorming)

1. **Scenario A1 — state restoration after disk-import.** Entities already exist
   in the DB with new UUIDs; A restores manual curation on top of them.
2. **Depth: state + manual commit history.** Restore both the final state tables
   AND re-insert the manual curation commits/changes for audit continuity.
3. **A writes to the DB** — `curation_commits`, `curation_changes`, and the state
   tables (`work_curation_states`, `creator_curation_states`,
   `asset_storage_states`, and `Work.is_favorite`). One transaction; idempotent.
4. **Replay manual commits only.** MANUAL triggers = `work_trash`, `work_restore`,
   `work_favorite`, `creator_{action}` (e.g. `creator_archive`/`creator_restore`),
   `work_purge`, `commit_revert`. AUTO (skipped) = `baseline_backfill`,
   `source_synced`, and the import trigger. (The exact import trigger is confirmed
   from `record_imported_work` during planning.)

## Entity Re-mapping (the crux)

Old subject UUIDs in `.gitllery` changes are re-mapped to current DB entity UUIDs:

| entity | anchor for old→new mapping |
|---|---|
| **work** | the work blob's `state.source` + `state.source_work_id` → current `WorkSource(source, source_work_id)` → `work_id`. Import/baseline changes always carry `source_work_id` in `after_state`, so build `old_work_uuid → (source, source_work_id)` by scanning ALL commits' changes (not just the latest tree, since a later trash blob's `after_state` may omit it). |
| **creator** | each repo's `config.json` carries the old `creator_id` + `source` + `source_creator_id` → current `SourceCreator(source, source_creator_id)` → `creator_id`. |
| **repository** | each repo's `config.json` carries the old `repository_id` + `(source, source_creator_id)` → current `SubscriptionSource(source, source_creator_id)`. |
| **asset** | no natural key in the asset blob (`AssetStorageState`) → **best-effort skip**. Purge's work-level state (`Work.visibility = purged`) still restores via the work mapping; asset-level `storage_state` is reported as skipped. |

Changes whose subject cannot be mapped (an entity present in history but no longer
on disk/in the DB) are dropped and counted in the report — never an error.

## Rebuild Algorithm (5 phases)

1. **Collect + merge.** Walk each repo's commit chain (HEAD→parent). A single
   original DB commit was sliced across repos, so **group all on-disk commits by
   `db_commit_id` and union their `changes[]`** (dedupe by
   `(subject_type, subject_id, action)`), reconstructing each original commit with
   its full change set + preserved metadata (`occurred_at`, `message`, `trigger`,
   `actor_*`, `stats`, `reverts`).
2. **Build maps.** From all work blobs → `old_work_uuid → (source, source_work_id)`
   then → current `work_id`. From all `config.json` → creator and repository maps.
3. **Filter + re-map.** Keep only MANUAL-trigger commits. Re-map each change's
   `subject_id` old→new; drop unmappable changes (report them); drop commits that
   become empty.
4. **Idempotent insert.** In `occurred_at` order, insert a new `CurationCommit`
   (fresh UUID) preserving `occurred_at`/`message`/`trigger`/`actor_*`/`stats`,
   with `dedupe_key = f"rebuild:{original_db_commit_id}"` (skip if it already
   exists → re-run safe). Re-establish `parent_commit_id` by `occurred_at` order
   within the rebuilt set. Re-map `reverts` via an `original_db_commit_id → new
   commit UUID` map built as commits are inserted. Insert `CurationChange` rows
   with re-mapped `subject_id`, preserving `action`/`before_state`/`after_state`/
   `diff`/`impact`.
5. **Apply final state.** From each repo's latest tree blobs, read each entity's
   final `visibility` / `is_favorite` / `storage_state`, re-map, and write the
   state tables directly. This is the authoritative current state — correct even
   if some commits were skipped.

## Idempotency & Safety

- **`dry_run`** mode: produce the report (commits to restore, states to apply,
  unmapped/skipped counts) without writing.
- `dedupe_key = "rebuild:{orig_db_commit_id}"` → re-running skips already-inserted
  commits. State writes are naturally idempotent (`set visibility=X`).
- **Insert-only + set-state; never delete** existing DB rows.
- One transaction; roll back on failure.

## Entry Point / API / Frontend

- `GitlleryService.rebuild_db_from_disk(repository_id: str | None = None, dry_run: bool = False) -> dict`
  returning a structured report (never raw NAS paths).
- Runs on the **operations queue** (like disk-import) with progress; API
  `POST /api/v1/curation/gitllery/rebuild` (`RequireAdmin`, `dry_run` flag).
- Admin-web Gitllery panel: a "Rebuild from disk" action — dry-run preview →
  confirm → execute.

## Testing

- **Unit:** merge on-disk commits by `db_commit_id` (union changes); old→new
  mapping (work via `source_work_id`, creator via `config.json`); manual/auto
  trigger filtering; `reverts` re-mapping.
- **Integration (full loop):** seed a work → manually trash + favorite → project to
  `.gitllery` → TRUNCATE curation tables AND re-create the work/creator with NEW
  UUIDs (simulating disk-import) → run `rebuild_db_from_disk` → assert: state
  restored (trashed + favorite), manual commits re-inserted, baseline/import NOT
  re-inserted, and a second run is idempotent (no duplicates via `dedupe_key`).
- Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_rebuild.py -v` (in-container path `tests/...`).

## Global Constraints

- Bundle A is the ONE gitllery path that writes the DB; all writes are insert-only
  + set-state, transactional, idempotent, and never delete.
- No NAS absolute paths in the API report; `RequireAdmin` on the endpoint.
- Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.

## Out of Scope

- Precise asset-level re-mapping (best-effort skip in A).
- Cross-instance portability import with entity provisioning (Bundle-D-adjacent;
  separate sub-project).
- Preserved-UUID verbatim full-DAG re-insert (the other scenario branch; not chosen).
- Bundles C (semi-auto curation rules) and D (outbox delivery).

## Open Questions

None.
