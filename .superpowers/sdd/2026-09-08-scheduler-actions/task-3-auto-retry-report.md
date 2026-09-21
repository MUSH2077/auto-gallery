# D27 automatic download retry capacity correction

Base: `1d0eaff6cda7816b5ef1f891e61cc4f9ab6bde5d`. Scope: worker-created download retry publication, bounded outbox outcome accounting, focused backend regressions, and this report. No frontend, migration, dependency, runtime configuration, resource-capacity, production, or fullsite state changed.

## Result

`_enqueue_download_retry` now commits its prepared deterministic dispatch intent before Redis publication and invokes the existing bounded `recover_download_dispatch_candidate` contract. A typed temporary admission refusal therefore returns `deferred` while the `DownloadJob` and `TaskRun` remain `enqueued`, the dispatch remains `pending`, the original retry count and 60-second delay remain intact, and the scheduler batch retains the same bound child. A later bounded outbox scan republishes the exact attempt-2 RQ identity once; repeated recovery does not create attempt 3 or a second publication.

Both worker call sites (`auto_retry` and `unexpected_error_retry`) consume the explicit recovery outcome. Logs say `Enqueued` only for `replayed`/`existing`, say `Deferred` for durable pending work, and warn with the concrete outcome for cancelled/invalid/skipped/terminal work. The direct HTTP/service publisher and its terminal compensation behavior are unchanged.

The cancellation regression confirmed the adjacent hypothesis from dispatch: `recover_download_dispatch_candidate` can return `cancelled`, but `recover_download_dispatch_outbox` did not have a `cancelled` result key and raised `KeyError`. The result map now accounts for that outcome. The parent fence still prevents Redis publication and leaves cleanup to the existing cancellation path.

## Regression coverage

The real PostgreSQL fixture seeds a scheduler batch, source membership, owned `DownloadJob`, existing `TaskRun` attempt 1, and batch-item binding. Only resource-pressure observation and Redis lookup/admission/supplier boundaries are substituted.

- Cartesian cases cover `auto_retry` and `unexpected_error_retry` with `queue_saturated`, `enqueue_busy`, and `redis_unwritable`.
- Each case verifies attempt 2 is durable and pending after refusal; job/task status, retry count 1, owner, triggering membership, source, batch link, delay, and fixed RQ ID are preserved; no `enqueue_failed` manifest event or terminal receipt exists; reconciliation reports the bound item as `queued`.
- Capacity restoration runs the real bounded outbox query/recovery and verifies one attempt-2 publication. A repeated scan checks zero candidates and no duplicate supplier call.
- Cancellation after durable retry preparation verifies the parent fence blocks publication and returns a counted `cancelled` outcome without changing the child to published.
- Focused existing cases retain direct publisher terminal compensation, deterministic-ID ambiguous acceptance, transient recovery, terminal RQ records, superseded attempts, scheduler publication/cancellation fencing, provider retry exhaustion, and pause/cancel finalization checkpoints.
- A malformed durable retry payload is marked `invalid` before any Redis lookup.

Tests do not infer provider executions from scheduler-batch attempt counters. The retry regression exercises the exact admission boundary; its accepting supplier is a deterministic fake and is asserted only as an RQ publication call.

## RED evidence

Common runtime:

```sh
docker exec \
  -e DATABASE_URL=postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test \
  -e TEST_DATABASE_URL=postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test \
  -e REDIS_URL=redis://ag-button-redis:6379/15 \
  -e MEILI_URL=http://localhost:9 \
  ag-button-runner sh -lc 'cd /workspace/backend && pytest <arguments>'
```

| Evidence | Exact pytest arguments and observed outcome | SHA-256 |
| --- | --- | --- |
| `red-worker-retry-20260909T1012.log` | `tests/test_scheduler_batches.py -k worker_retry_defers_temporary_admission --basetemp=/dev/shm/auto-retry-red-20260909T1012 -q` — expected **6 failed**, all because `_enqueue_download_retry` raised typed admission refusal through `publish_prepared_download`. | `aa60409af69ed9c1e15abef461a8093f5f085f39c7e0eb326b037daab0754d27` |
| `red-retry-logging-20260909T1023.log` | `tests/test_download_retry_dispatch.py --basetemp=/dev/shm/auto-retry-log-red-20260909T1023 -q` — expected **4 failed**, missing outcome-aware logging helper. | `ef3fa1bfdd6d03ccbd8e3575bc53dd5f15cac4a98a192be47eccf62c4f7fd390` |
| `red-cancel-outbox-20260909T1030.log` | `tests/test_scheduler_batches.py -k cancelled_parent_blocks_pending_retry_outbox --basetemp=/dev/shm/auto-retry-cancel-red-20260909T1030 -q` — expected **1 failed** with confirmed `KeyError: 'cancelled'`. | `af115684d5cfab492594c2826ba3cd2119d36a9d22c74b143af28c6b3df12d73` |

The first RED run is the released behavior reproduction. The other RED runs isolate logging truthfulness and the now-confirmed adjacent cancellation accounting defect. Original logs were never overwritten.

## Final verification

The final focused command was:

```sh
docker exec \
  -e DATABASE_URL=postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test \
  -e TEST_DATABASE_URL=postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test \
  -e REDIS_URL=redis://ag-button-redis:6379/15 \
  -e MEILI_URL=http://localhost:9 \
  ag-button-runner sh -lc 'cd /workspace/backend && pytest tests/test_download_dispatch.py tests/test_download_retry_dispatch.py tests/test_scheduler_batches.py::test_cancel_serializes_with_first_publication_and_blocks_replay tests/test_scheduler_batches.py::test_publication_pressure_keeps_bound_identity_waiting_then_resumes tests/test_scheduler_batches.py::test_worker_retry_defers_temporary_admission_and_replays_same_attempt tests/test_scheduler_batches.py::test_cancelled_parent_blocks_pending_retry_outbox_without_accounting_error tests/test_shared_remote_scheduling.py::test_tampered_personal_credential_fails_once_and_allows_peer_takeover tests/test_download_liveness.py::test_download_finalization_checkpoint_prevents_import_handoff --basetemp=/dev/shm/auto-retry-final-tests3-20260909T1105 -q'
```

Outcome: **31 passed in 14.67s**. Raw output: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-auto-retry/final-targeted-tests3-20260909T1105.log`, SHA-256 `96d9846fab09703d677c4aa71c6408dd56ff8bc8b4d58992c5d366d9b1e9a8ee`.

Static command:

```sh
docker exec ag-button-runner sh -lc 'cd /workspace/backend && ruff check app/jobs/download.py app/services/download_dispatch.py tests/test_download_dispatch.py tests/test_download_retry_dispatch.py tests/test_scheduler_batches.py && python -m compileall -q app/jobs/download.py app/services/download_dispatch.py tests/test_download_dispatch.py tests/test_download_retry_dispatch.py tests/test_scheduler_batches.py'
git diff --check
```

Outcome: Ruff and compilation passed; `git diff --check` passed. Raw static output: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-auto-retry/final-static-checks3-20260909T1107.log`, SHA-256 `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18`.

Final pre-report file SHA-256 values:

```text
ef78c938d8a1039db395c61425e78922eafc56d89cc60ff627c8688a6ab8f9c6  backend/app/jobs/download.py
dbe8a6b294b70efda87a237b786cc97029094d30b2aada7407e451ec4cbce5c0  backend/app/services/download_dispatch.py
44e6102181c2d30c386c0ce439beb397062522d3e4cf9b6b280a77e068032085  backend/tests/test_download_dispatch.py
62dc56be03be94581c65cd4e9d872e9ab94e67413c3367ba2ec588cbe7044732  backend/tests/test_download_retry_dispatch.py
5c2ec1432fe7803e4bfc84a16ec24eda0ad771f828f2ec2aa8712fb8a12b1a92  backend/tests/test_scheduler_batches.py
```

## Limits

PostgreSQL runs on tmpfs and the test database uses `synchronous_commit=off`; this is correctness evidence only, with no performance, WAL/crash-durability, or production-provider claim. Redis DB15 is isolated. Meilisearch is deliberately unavailable for unrelated best-effort teardown. Root retains independent review, fullsite acceptance, production changes, and rollout ownership.
