# A030 R1 — mandatory safety coverage

Base: `861bdd3c74c22bd2d9401366e4237ed54ff30f49`. Scope: new integration test module and this report only. No application behavior, migration, lock order, frontend, production, or fullsite fixture changes. Independent review's two Important coverage gaps are addressed directly. Existing reports, source manifest, and logs remain unchanged.

## Ancestor replacement

Two deterministic cases pause after the exact candidate identity is committed in the registered TaskRun checkpoint and before the actual visitor/finalizer runs. A separate observer session verifies the saved checkpoint. The real creator ancestor is renamed outside downloads, and a replacement ancestor is installed.

- `replacement-file`: a same-named replacement JSON is written in the new subtree. The displaced original and both outside/replacement sentinels retain identical bytes.
- `same-inode-subtree`: the original work directory is moved beneath the replacement creator ancestor. The test asserts that the candidate device/inode/size/mtime/ctime tuple is unchanged; only the higher ancestor identity changes. This specifically exercises the ancestor-chain guard rather than relying on changed file inode detection.

Both require zero removals, one explicit replacement/ancestor skip, and byte-for-byte sentinel/sidecar survival. The visitor wrapper is only an event barrier; `finalize_file`, descriptor reads, row locks, and unlink are real.

## Proof invalidation

Every case begins with the actual successful Pixiv import → projection → normal compaction fixture and its naturally captured certificate. A real certificate may remain as a negative sentinel while the owned fixture is reset/reassigned. No hand-written proof authorizes a positive cleanup.

Six ledger/import-intent cases directly invoke `reset_retry_assignment`, `claim_work_batch` for new and expired leases, `renew_work_leases`, `release_owned_leases`, and `_prepare_import_intent`. Each verifies that the real mutation affected the expected work/owner and that persisted `metadata_completion_proof` is NULL immediately afterward.

Repository reconciliation runs both existing-content completion and recovered-backlog reassignment through `reconcile_repository_artifacts`, verifying exact resulting state/owner/counters and certificate removal.

Disk import runs existing-content, missing-file, importable-ledger, and explicit reset discovery paths through `reconcile_downloads_to_db`. Importable/reset cases observe the committed artifact in a separate session at downstream publication entry, before import-intent code could independently clear it. Only downstream queue publication and external optional creator enrichment are substituted; the actual ledger selection, discovery, provisioning, assignment updates, and commits execute. Missing-file and existing-content cases require the real corresponding counters/status/error.

After invalidation, candidates are also checked in an otherwise completed/unowned shape, restoring the retained source identity and neighboring media state where needed. This setup uses no other proof-clearing helper. Cleanup must report missing/changed completion evidence and preserve the sidecar; stale certificates cannot reappear just because work returns to `done`.

## Verification and mutation sensitivity

All verification used the exclusively assigned real PostgreSQL16 tmpfs fixture `ag-button-a030-postgres/agbutton_test`, `ag-button-runner`, and `ag-button-redis` DB15. The original host-disk database and fullsite database/Redis8 were untouched. Runner/process state and PostgreSQL logs were checked before execution.

Common runtime command prefix:

```sh
docker exec \
  -e DATABASE_URL=postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test \
  -e TEST_DATABASE_URL=postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test \
  -e REDIS_URL=redis://ag-button-redis:6379/15 -e MEILI_URL=http://localhost:9 \
  -w /workspace/backend ag-button-runner pytest <arguments>
```

Commands and raw output under `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-a030/`:

| Evidence | Pytest arguments / outcome |
| --- | --- |
| `r1-coverage-first.log` | `-q -x --basetemp=/dev/shm/a030-r1-coverage-first tests/test_metadata_cleanup_invalidation.py` — **14 passed in 32.18s** against the existing correct implementation. |
| `r1-mutation-release.log` | `-q --basetemp=/dev/shm/a030-r1-mutation-release tests/test_metadata_cleanup_invalidation.py::test_owned_mutations_clear_naturally_captured_proof[release]` — expected **1 failed in 3.71s** at persisted proof-is-NULL assertion when only the release invalidator was temporarily removed. |
| `r1-mutation-ancestor.log` | `-q --basetemp=/dev/shm/a030-r1-mutation-ancestor tests/test_metadata_cleanup_invalidation.py::test_ancestor_swap_after_persisted_intent_preserves_replacement[same-inode-subtree]` — expected **1 failed in 3.78s**, observing removed=1 when both ancestor checks were temporarily removed. |
| `r1-coverage-final.log` | `-q -s --basetemp=/dev/shm/a030-r1-coverage-final tests/test_metadata_cleanup_invalidation.py` — **14 passed in 30.65s**, no pytest warnings. |

These are new-coverage GREEN results and representative mutation-sensitivity failures, not claims that the reviewed implementation had a demonstrated production bug. The external `r1-mutation-check.py` sequentially applied each narrow mutation, verified the expected failing exit code, and restored the exact reviewed bytes in `finally`. `r1-mutation-results.json` contains restored-source and failing-log hashes. All 15 original source files match the original frozen manifest after restoration. The final run includes stronger return-to-done assertions added after the first run.

The new test module compiles, `git diff --check` passes, and `r1-source-sha256.json` records its frozen hash plus original-source identity. The final commit's exact binary diff/hash and log hashes are recorded externally in `r1-completion.json` / `r1-final-diff.patch`.

This remains correctness-only evidence: unchanged conftest uses `synchronous_commit=off`; tmpfs PostgreSQL retains `fsync`/`full_page_writes` on. No crash/WAL durability, performance, public HTTP/browser, or search-integration claim is made. Meilisearch is deliberately unavailable for bounded irrelevant teardown. Root owns independent review and rollout.

Final new-test SHA-256: `acc2da943ef945053816739c5fcdf12221b2ffb28ffa84f3460a4bb10f5346d2`. Original 15-file source manifest matches after the final run.
