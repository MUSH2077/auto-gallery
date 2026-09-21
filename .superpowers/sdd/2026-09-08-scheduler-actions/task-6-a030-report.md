# Task 6 — A030 safe managed metadata cleanup

Status: implementation and scoped verification complete; independent root review and fullsite/browser acceptance pending. Base: `e81388954bca3de86eeebcdf2b722b7fa684d287`. Backend-only scope; no production/fullsite writes or publishing.

## Behavior

The registered maintenance operation enumerates download `metadata_json` ledger rows in UUID keyset pages of 25. It never walks the download tree. It preserves hidden/control/manual/untracked inputs, nonterminal or leased ownership, unsupported direct-writing configurations, missing/changed domain or projected copies, and ambiguous legacy compacted rows.

A nullable JSONB `StorageArtifact.metadata_completion_proof` is captured before existing compaction deletes owners. It contains successful original owner identities/status/attempts, the exact sidecar version, one work's WorkSource/Work and AssetSource/Asset tuples, assigned media versions/associations, and a digest explicitly representing the full retained database payload. It does not claim an import-time byte hash, duplicate full raw JSON, or backfill old rows. Capture uses batched ordinary MVCC queries under the compactor's existing artifact→parent→child→TaskRun locks; it adds no filesystem reads or hot domain locks. Assigned imports require matching v2 checkpoints. The existing-content branch requires successful owners and certifies redundancy, not authorship or recovered statistics.

Cleanup obtains fresh nonblocking row locks, validates exact retained provider metadata and the managed complete library projection, obtains the existing promotion flock nonblocking, then fences the exact running cleanup attempt. The bounded file finalizer uses `O_NOFOLLOW`/directory descriptors, inode/device/size/mtime/ctime plus ancestor identity, bounded JSON bytes and fresh SHA-256 checks before unlink. The finalizer runs in an awaited thread; outer cancellation waits for it before dropping transaction/fence/flock ownership. The legacy delivery entrypoint now fails explicitly without destructive work.

Every later ledger refresh/claim/reset/mark/lease mutation and the direct retry/repository/disk assignment seams clear proof. Cleanup also compares the full version/domain snapshot, so missed invalidation cannot authorize a changed version.

## Terminal contract

HTTP 202 remains admission-only; it has no removal count. The final result preserves `removed` and `message`, adds `scanned`, `skipped`, `failed`, reason counts, a 25-item error sample plus `errors_truncated`, `scope=managed_download_metadata`, and `status=complete|partial`.

Exactly one outcome is recorded per visited ledger row: `scanned = removed + skipped + failed`. Any failed items yield TaskRun `failed`, reason `metadata_cleanup_partial_failure`, **while retaining the full useful result including confirmed removals**. Skips without failures yield TaskRun `complete`. The message explicitly says partial failure when applicable. Frontend must read this terminal result even for failed TaskRuns.

Each candidate intent and completed count/cursor is checkpointed. Missing entries count as skipped/already_absent. A crash between unlink and checkpoint cannot invent a removed count. Recovery uses the original captured file/ancestor identity, protecting a replacement at the same path; a missing entry remains unconfirmed/already_absent.

## Evidence and original failures

External raw logs: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-a030/`.

- `red-initial.log`: genuine original defect — recursive cleanup deleted protected `.import-lists/active.json`; 1 failed.
- `red-natural.log`: fixture import path collection error (`ModuleNotFoundError`), corrected to package-qualified fixture import.
- `red-natural-retry.log`: model/schema mismatch before the scoped new column existed, not a behavioral cleanup regression result.
- `migration-upgrade.log`: attempted guard assertion failed. `migration-upgrade-retry.log`: whole migration history encountered an existing historical constraint and rolled back; test DB was created from Base.metadata with no Alembic version. No stamp/reset/guard changes. Only the new column was subsequently applied through scoped Alembic Operations (`migration-scoped.log`).
- `green-initial.log`: prior interrupted implementation's first 2 focused tests passed.
- `red-partial.log`: terminal TaskRun incorrectly reported complete despite an unlink failure; fixed semantic terminal classification while retaining result_data.
- `green-covering-1.log`: 6 passed, all-existing fixture assertion failed. `all-existing-diagnostic.log` reproduced the failure with no import error. Root cause was reading the cached pre-import child instance after a no-op rollback; the assertion now reloads the committed row with `populate_existing=True`.
- `red-proof-validation.log`: malformed checkpoint signature raised AttributeError; parser now rejects malformed signatures safely.
- `green-expanded-2.log`: exploratory persistent-disk run interrupted with SIGINT after fixture/teardown delays. Partial output and failure are preserved; it is not final verification.
- `tmpfs-focused-first.log` / `tmpfs-focused-second.log`: corrected the artificial all-existing fixture. The final fixture uses the actual `_successful_repository_import_plan` → existing-content reconciliation (`pending_work_count=0`, no child) → `classify_no_metadata_outcome` / `finalize_download_job` → normal compaction → registered cleanup, with no hand-written proof. Intermediate failures: 1 failed/4 passed each.
- `tmpfs-focused-third.log`: prior implementation checkpoint, 16 passed in 65.13s; capture of 26 works used 14 statements / 12 SELECTs, maximum proof 1,388 bytes.
- `retained-owner-red.log`: new review regression initially failed on missing fixture subscription_id; not behavioral evidence. `retained-owner-red-corrected.log`: confirmed cleanup wrongly accepted a child reparented to another complete owner. Final cleanup explicitly checks the locked child belongs to the locked parent.
- `existing-regressions-final.log`: 67 passed, one stale test failed in 55.37s. The unchanged direct detail/list test omitted the authenticated actor required by the prior authorization change. Its failing test, endpoint, and permission functions were AST-identical to BASE (comparison preserved). Root authorized a test-only correction: reuse `_seed_user` and explicitly pass a persisted system-permitted user to both calls, preserving actual permission enforcement and Redis-down/PostgreSQL-first assertions.
- `focused-and-reconciliation-final.log`: after the ownership guard, 35 passed in 69.91s (16 cleanup + 19 existing repository reconciliation). Capture: 14 statements / 12 SELECTs; max proof 1,388 bytes.
- `proof-veto-red-and-actor-green.log`: the corrected actor test passed; a new compaction negative failed because the certificate ignored an explicit `import_completion_identity_mismatch` TaskRun reason. Capture now vetoes either explicit completion-evidence failure reason in its existing batch query, matching retained-owner cleanup behavior, without extra locks or queries.

## Commands and validation

All runtime commands use the exclusively assigned `ag-button-runner`, isolated `agbutton_test`, and Redis DB15. The original persistent database is on `ag-button-postgres`. No production or `agbutton_fullsite_actions`/Redis8 access was used.

Commands executed after resuming:

```sh
docker exec -w /workspace/backend ag-button-runner sh -c 'pytest -q tests/test_metadata_cleanup.py::test_natural_all_existing_import_keeps_proof_after_compaction > /evidence/backend-a030/all-existing-diagnostic.log 2>&1'
docker exec -w /workspace/backend ag-button-runner sh -c 'python -c "from app.services.metadata_cleanup_proof import index_checkpoint; assert index_checkpoint({\"version\":2,\"signature\":\"bad\"}) is None" > /evidence/backend-a030/red-proof-validation.log 2>&1'
docker exec -w /workspace/backend ag-button-runner sh -c 'pytest -q tests/test_metadata_cleanup.py > /evidence/backend-a030/green-expanded-2.log 2>&1'
python3 -m py_compile backend/app/services/metadata_cleanup.py backend/app/services/metadata_cleanup_proof.py backend/tests/test_metadata_cleanup.py
git diff --check
```

Persistent-db test setup repeatedly spent minutes in PostgreSQL `DataFileImmediateSync` during fixture TRUNCATE; root observed host I/O pressure. The original host-disk `ag-button-postgres/agbutton_test` remains intact. Root stopped the previous test process with SIGINT and verified it gone before granting the replacement runner slot.

Final checks use the separately approved real PostgreSQL 16.14 container `ag-button-a030-postgres` (`d819caa53862456f7f61ed9f994292545bca6acb91c8c4b17d6abfff6b2b067c`), same pinned image as the original, with tmpfs PGDATA (256 MiB), memory 512 MiB, CPU .35, PID limit 96, no published ports, on `ag-button-test-20260908`. `fsync` and `full_page_writes` remain on. The preexisting unchanged conftest sets test-database `synchronous_commit=off`. This is correctness evidence only, not crash/WAL durability or production performance evidence. Preparation details are in `tmpfs-postgres-preparation.json`.

Every final runtime test explicitly sets both database URLs to `postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test`, Redis to `redis://ag-button-redis:6379/15`, and `MEILI_URL=http://localhost:9` for bounded irrelevant teardown. Logs explicitly report skipped Meilisearch cleanup; no search integration claim is made. Tests use unique `/dev/shm/a030-*` basetemps. Runner processes and PostgreSQL logs were checked before the first new run; runs are sequential.

Final reproducible runtime prefix (all logs under `/evidence/backend-a030` inside the runner, matching the external evidence directory):

```sh
docker exec \
  -e DATABASE_URL=postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test \
  -e TEST_DATABASE_URL=postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test \
  -e REDIS_URL=redis://ag-button-redis:6379/15 -e MEILI_URL=http://localhost:9 \
  -w /workspace/backend ag-button-runner sh -c '<pytest command> > /evidence/backend-a030/<log> 2>&1'
```

Exact final regression commands:

```sh
pytest -q --basetemp=/dev/shm/a030-existing-final tests/test_admin_operation_dispatch.py tests/test_operation_attention.py tests/test_import_finalization_recovery.py
pytest -q -s --basetemp=/dev/shm/a030-focused-final tests/test_metadata_cleanup.py tests/test_repository_artifact_reconciliation.py
pytest -q --basetemp=/dev/shm/a030-proof-veto-red tests/test_metadata_cleanup.py::test_compaction_does_not_certify_explicit_completion_evidence_failure tests/test_admin_operation_dispatch.py::test_operation_reads_are_postgresql_first_when_redis_is_down
pytest -q -s --basetemp=/dev/shm/a030-frozen-final tests/test_metadata_cleanup.py tests/test_operation_attention.py::test_compaction_rechecks_artifacts_after_competing_claim_commits tests/test_admin_operation_dispatch.py::test_operation_reads_are_postgresql_first_when_redis_is_down tests/test_admin_operation_dispatch.py::test_cleaning_danbooru_and_job_diagnostic_starts_are_durable_without_redis
```

`frozen-final.log`: **20 passed in 61.81s**, comprising all 17 cleanup tests plus existing competing-claim compaction, corrected PostgreSQL-first detail/list, and admission-only cleanup dispatch checks. The final 26-work capture measured 14 SQL statements (12 SELECTs), maximum proof 1,388 bytes, 0.03344 seconds in this correctness fixture. Elapsed time is not a production throughput claim. Together with the earlier 67 passing registered/attention/checkpoint regressions and 19 passing repository reconciliation tests, the formerly failing direct-call regression is now resolved. The earlier run contains one RuntimeWarning about an unawaited registered-operation coroutine; the final run has no pytest warnings. Source hashes were verified unchanged after the final run. Compilation of all 15 scoped Python files and `git diff --check` passed. `alembic heads` reports the single `fc24d6e8fa02` head. The scoped migration test exercises downgrade/upgrade under rollback and verifies legacy rows remain NULL; a full historical migration upgrade was not validated from this preexisting metadata-created fixture.

## Deliberate limits

- Old already-compacted rows without a certificate remain protected; there is no aggregate-receipt/timestamp backfill.
- Full retained payload must match. Providers/sidecars without that retained copy are skipped.
- Canonical staging disabled is conservatively skipped, because the promotion flock cannot serialize bypass writers.
- Metadata input is bounded to 8 MiB; proofs to 64 KiB; domain/media snapshots are bounded. Oversized/ambiguous evidence is protected rather than guessed.
- Cleanup and PostgreSQL cannot form a single atomic filesystem transaction. Counts are confirmed per recorded execution, never reconstructed from later absence.
- No public HTTP/browser acceptance claim belongs to this backend report; root owns those independent checks. Backend tests exercise real provider import, projection, compaction, endpoint admission/registered dispatch, and real unlink, with narrow deterministic fault barriers.

## Final source integrity

Exact SHA-256 manifest is also preserved externally as `final-source-sha256.json`; report is committed with the implementation.

```text
970ea75fd30a24ae49d2f147104801316b945cd7a205012daf5500e3d028c69a  backend/alembic/versions/fc24d6e8fa02_metadata_completion_proof.py
27fd501a63df11068438d14ed3aec1f184dba9d9859ba89a5be672b5b0463ad4  backend/app/api/admin/data.py
e927678adf260393dbbd1a1c44d4daf0ce123852e2e92f043783367069a8adea  backend/app/jobs/admin_operations.py
721e0d745e3dde91d4870b7fa0f12c28b7e37ba134b433e96eba70a750d9abdf  backend/app/jobs/download.py
81df4f8590b688b83127e08cd3f1c6d0b712c39c59b39cae95ce0ee382463fa6  backend/app/jobs/import_runner.py
eb8eb6ffa949e36505a101f8676d163a7cf9f9502e78785684031f7fa6d0cb26  backend/app/models/storage_artifact.py
913d38b9951ee934272eab70496ed7729e4df842fbc54ceaa2a5adccb365b10b  backend/app/schemas/data_center.py
e31e70b31044dd4119c02b1919455be9cebcf9d9860fc845df91614d92cc52ab  backend/app/services/artifact_ledger.py
f7ddfe663de97720fb1918d70efc5ba5ec2ca6f9a7c645be1188f2d3ccab4e7b  backend/app/services/disk_import.py
ebc846e0ebe78add01d2ae05f4dd17addd9e0c123dffea10953f570e818af149  backend/app/services/metadata_cleanup.py
0ac91e6070e8ba949e7946e32ceda0c9fe97eba02b4d5af35061c6b619bcf009  backend/app/services/metadata_cleanup_proof.py
9bb890899a88ad2c02e021398ede66b655022c7ed4016ac03b24bc1fd5cefa84  backend/app/services/operation_attention.py
ee51e791fb378bd7b70f339b147dcd86a2063c4236ea6304b286fd9d7491ebd7  backend/app/services/repository_artifact_reconciliation.py
b58151b002017eb030151dca63b98419e9aebb99533ed53c3f24511400203624  backend/tests/test_admin_operation_dispatch.py
1f6a03785c3e82938b519a9f6fb49320355eeba1dd50ee9b1d870f791c4f396f  backend/tests/test_metadata_cleanup.py
```

## Self-review and acceptance boundary

The review rechecked managed-scope enumeration, owner/checkpoint/proof authority, proof invalidation mutations, task-last fence and cancellation containment, descriptor-based unlink, per-item accounting, public202 admission, partial-failure retention, and the scoped migration head. It found and fixed the retained child-parent mismatch and explicit completion-evidence failure certification described above.

The focused runtime cases include natural first-attempt and all-existing compaction positives, raw-payload replacement with copied size/mtime, current owner statuses, projection/media identity vetoes, ledger contention, cancellation before/during file finalization, external-process promotion flock, direct-writer refusal, symlink/outside sentinel, legacy-no-proof protection, hidden/control/restore sentinels, keyset continuation, crash-after-unlink replacement recovery, per-file failures and bounded errors, atomic proof rollback, stale cached-owner reloading, and scoped migration NULL preservation. The mutation test directly exercises mark_work/mark_works/mark_work_results and same-mtime size/work/name upserts; reset/claim/renew/release and direct download/repository/disk invalidation seams were inspected in source, with existing integration regressions for those surrounding paths, not a fabricated claim that each individual seam has a dedicated proof-assertion test. Ancestor identity checks are implemented but this scoped suite has no separate deterministic ancestor-swap barrier case. Full public HTTP/auth/browser acceptance and production rollout remain root-owned.
