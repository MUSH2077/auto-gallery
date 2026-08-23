# Final review fix wave

## Root-cause investigation (recorded before production edits)

Reviewed base `5d940dd52163c8c4469c2c7a4342ec591ab26c6d` on branch
`fix/data-center-tag-repo-import-reconciliation`; the worktree was clean.

1. **Storage-root identity is omitted at four read boundaries.**
   `repository_artifact_reconciliation._locked_repository_metadata_rows`,
   `operation_attention._repository_has_recoverable_backlog`, the ordinary
   `disk_import` done-path/pending-owner queries, and the Data Center pipeline
   aggregates constrain source/type/state but not `storage_root`. The database
   uniqueness key is `(storage_root, file_path)`, so a downloads row and a
   library projection may intentionally have the same relative path. Without
   the root predicate, a library metadata row satisfies download ownership,
   completion, and backlog logic. Hypothesis confirmed directly from the query
   predicates and `StorageArtifact`'s `uq_storage_artifacts_root_path` identity.

2. **Data Center directory linking repeats repository work per ledger row.**
   `_ledger_storage_breakdown` groups repositories only by source, then for each
   ledger creator-directory iterates every repository of that source and calls
   provider URL normalization/directory extraction again. Runtime is therefore
   O(directories x repositories), and equal-score duplicates silently select
   whichever row PostgreSQL returned first. There is no ambiguity state.

3. **Ordinary disk recovery starts from the filesystem and loses every bounded
   boundary before import.** `reconcile_downloads_to_db` loads every source's
   done path into a set, recursively walks the entire source root, builds a
   dictionary containing every JSON path, and only then applies repository
   directory filtering. It passes an entire creator's paths to one import job.
   `run_import_job` then calls `ArtifactLedger.new_metadata_paths`, materializes
   all paths for the owner, and parses/group them all before the later 25-work
   resource slices. Thus repository scope still traverses siblings, first work
   latency/cardinality scale with the whole tree, and queue/resource pressure is
   not rechecked between bounded publications. The existing ledger already has
   source, creator directory, source-work identity and owner, so the recursive
   discovery is unnecessary except for explicit reset/untracked rebuild.

4. **Tag composition uses SQLAlchemy's expanding `IN` parameter.**
   `source_usage_by_tag` feeds every returned UUID to
   `WorkSourceTag.tag_id.in_(tag_ids)`. The PostgreSQL/asyncpg execution expands
   that collection into one bind per UUID, so an include-all response can cross
   the protocol's parameter ceiling even though aggregation itself is set based.

5. **The activity year picker mixes two focus models.** The listbox owns DOM
   focus and announces one active option through `aria-activedescendant`, but
   each option is also a native button with the default `tabIndex=0`. Tab and
   Shift+Tab therefore enter option buttons instead of leaving the composite;
   the popup also has no explicit Tab-close path.

### Regression contracts named before tests

- Removing the downloads-root predicate must let an identical-path library row
  affect reconciliation, backlog, disk recovery, or Data Center counts.
- Restoring per-directory repository scans must make provider normalization
  calls grow with directory count; duplicate exact directory ownership must be
  rendered unlinked.
- Restoring recursive ordinary discovery must touch a sibling trap; removing
  keyset/batch progression must publish more than 25 works or fail to advance
  across 25/25/1; removing between-batch capacity checks must reduce their count.
- Restoring expanding `IN` must compile a high-cardinality composition query
  with one parameter per tag rather than one PostgreSQL array bind.
- Restoring tabbable option buttons or omitting Tab-close must move focus into
  an option instead of the adjacent composite control.

## RED / GREEN evidence

Pending.

## Self-review and verification

Pending.

## Commits and remaining concerns

Pending.

## Completed RED / GREEN evidence

All PostgreSQL integration commands used the isolated test cluster at
`127.0.0.1:55432` with `autogallery_test`; no production database or service was
mutated.

### Initial backend regression RED

Command (seven new finding-specific tests):

```text
DATABASE_URL=postgresql+asyncpg://autogallery:final-fix-test@127.0.0.1:55432/autogallery \
TEST_DATABASE_URL=postgresql+asyncpg://autogallery:final-fix-test@127.0.0.1:55432/autogallery_test \
REDIS_URL=redis://127.0.0.1:6379/15 .venv/bin/pytest -q \
  tests/test_repository_artifact_reconciliation.py::test_repository_reconciliation_never_adopts_same_path_library_projection \
  tests/test_operation_attention.py::test_repository_backlog_guard_ignores_library_metadata_projection \
  tests/test_storage_breakdown.py::test_storage_breakdown_uses_ledger_rows_for_storage_identity_and_backlog \
  tests/test_storage_breakdown.py::test_storage_breakdown_indexes_repositories_once_and_unlinks_ambiguous_identity \
  tests/test_disk_import.py::test_ordinary_repository_drain_never_walks_sibling_directories \
  tests/test_disk_import.py::test_ordinary_disk_drain_keyset_feeds_25_work_batches_with_backpressure \
  tests/test_tags_api.py::test_source_usage_high_cardinality_uses_one_postgresql_array_parameter
```

Observed: `7 failed in 3.64s`. The failures were the expected behaviors: a
library projection was adopted; the backlog guard and pipeline counters counted
library state; repository normalization ran 24 rather than 2 times; scoped
ordinary import reached a sibling source-tree `rglob` trap; the old drain
published one 50-work feed instead of `25/25/1`; and the tag query compiled
70,000 expanded parameters.

After the minimum fixes, the same seven tests reported:

```text
7 passed in 1.53s
```

### Durable bounded assignment RED / GREEN

Before the production assignment update, the new `_enqueue_import` publication
test observed that the selected downloads artifact retained
`import_job_id=None` instead of the newly created ImportJob UUID:

```text
1 failed in 0.55s
```

After assigning only exact downloads metadata paths to the child ImportJob:

```text
1 passed in 0.52s
```

The regression was then extended with an identical-path library projection and
the exact repository-owner scope. Before the follow-up predicates:

```text
F.F                                                                      [100%]
2 failed, 1 passed in 1.00s
```

The scoped drain enqueued both repositories' same-directory paths, and
`claim_work_batch` changed the library row from `new` to `importing`. After the
owner-keyset and downloads-only lease/update fixes:

```text
...                                                                      [100%]
3 passed in 1.02s
```

### Shared bounded-parent lifecycle RED / GREEN

Command:

```text
pytest -q tests/test_import_lifecycle_batches.py
```

The first RED was the expected missing coordination seam:

```text
ImportError: cannot import name 'coordinate_import_parent_completion'
1 failed in 0.24s
```

After adding serialized parent aggregation, the publication-open race was made
explicit. Its RED was the expected missing close seam:

```text
ImportError: cannot import name 'close_bounded_import_publication'
1 failed in 0.29s
```

The GREEN proves a completed 25-work child cannot finalize the shared parent
while another child or the ledger publisher is active; closing publication
elects exactly the last ready participant and aggregates `25 + 1` works:

```text
.                                                                        [100%]
1 passed in 0.35s
```

### Frontend focus-model RED / GREEN

The isolated Playwright year-picker flow initially failed at
`expect(yearListbox).toBeHidden()` after Tab because focus entered an option.
After making options non-tabbable and closing on Tab without preventing native
focus traversal, the final command was:

```text
PLAYWRIGHT_BASE_URL=http://127.0.0.1:13001 npx playwright test \
  tests/e2e/admin-shell.spec.ts --project=chromium --workers=1 \
  --grep 'creator activity calendar aligns real month spans'
```

Output:

```text
1 passed (12.1s)
```

The test covers Tab to Next year and Shift+Tab back to the year trigger, with
the listbox hidden in both directions.

## Final implementation and self-review

- Centralized the `DOWNLOAD_ROOT` identity in
  `downloads_artifact_predicate()` and applied it to reconciliation, operation
  attention, Data Center pipeline counts, disk recovery, and every artifact
  import claim/renew/terminal/release mutation. Same-relative-path library rows
  remain independent and untouched.
- Replaced directory-by-repository scans with precomputed exact
  `(source, source_creator_id)` and `(source, provider_directory)` indexes.
  Provider normalization is once per repository/source-creator, and competing
  identities become explicitly unlinked.
- Ordinary disk import now starts from downloads-ledger keysets ordered by
  source, creator directory, repository owner, and source-work ID. It publishes
  at most 25 works per child, rechecks resource/Redis capacity between task
  batches, uses exact repository ownership, and never recursively scans.
  The only remaining `rglob` is below the early ordinary return in the explicit
  `reset_ledger` rebuild path; repository reset traverses only its resolved
  directories.
- Child path feeds are durable PostgreSQL assignments. The importer reads its
  own ImportJob assignment, and the publication update accepts only unleased
  `new` downloads rows. A parent-row `FOR UPDATE` coordination seam prevents
  early parent completion while batches are still being published or run, then
  aggregates the bounded child results. The close query refreshes the identity
  map under lock so concurrent child manifest updates are not overwritten.
- Preserved artifact-before-DownloadJob locking in ledger adoption: artifact
  rows remain ordered by owner/creation/id and locked first; owners remain
  ordered by ID and locked second. Existing live-lease and exact source-work
  deduplication remain intact.
- Replaced expanding tag UUID `IN` with one typed PostgreSQL UUID array bind and
  `ANY`, retaining one set-based grouped aggregation.
- Kept one React listbox focus model: DOM focus remains on the listbox,
  `aria-activedescendant` identifies the active option, option buttons have
  `tabIndex=-1`, and Tab closes without suppressing native traversal.
- Reviewed the complete diff for unrelated changes, schema changes, circular
  imports, API contract drift, recursive ordinary discovery, expanding
  high-cardinality parameters, and generated dev-server files. No migration or
  external API shape was added. The requested Minor cached-refresh alert remains
  deliberately deferred.

## Final verification

Fresh widened backend command:

```text
DATABASE_URL=postgresql+asyncpg://autogallery:final-fix-test@127.0.0.1:55432/autogallery \
TEST_DATABASE_URL=postgresql+asyncpg://autogallery:final-fix-test@127.0.0.1:55432/autogallery_test \
REDIS_URL=redis://127.0.0.1:6379/15 .venv/bin/pytest -q \
  tests/test_disk_import.py \
  tests/test_repository_artifact_reconciliation.py \
  tests/test_operation_attention.py \
  tests/test_storage_breakdown.py \
  tests/test_tags_api.py \
  tests/test_import_microbatch.py \
  tests/test_import_lifecycle_batches.py \
  tests/test_import_recovery.py \
  tests/test_import_file_list.py \
  tests/test_download_defaults_threshold.py \
  tests/test_manual_upload.py \
  tests/test_video_backfill.py
```

Output: `94 passed in 12.91s`.

Backend static verification:

```text
.venv/bin/ruff check app tests
.venv/bin/python -m compileall -q app
git diff --check
```

Output: `All checks passed!`; compile and whitespace checks exited 0.

Frontend static/build verification:

```text
npm run check:charts && npm run check:i18n && npm run check:api-types \
  && npm run typecheck && npm run build
```

Output: chart validation passed; 2,566 bilingual keys passed; generated API
types match `docs/api/openapi.json`; TypeScript passed; Next.js production build
compiled and generated all routes successfully.

## Commits and concerns (finalized below after commit)

No known code correctness concern remains. Environmental-only notices were the
existing Next.js module-type performance warning and best-effort Meilisearch
test cleanup skipping because the optional SOCKS transport is absent; neither
affected a test or build result.

## Commit record

- Implementation, regressions, verification record, and initial report:
  `fdeb11ac2c82e5ea961724203a495adf5a213e95`
- This appended commit record is committed separately as the final report-only
  commit; its SHA is returned to the controller alongside the implementation
  SHA to avoid rewriting the completed implementation commit.
