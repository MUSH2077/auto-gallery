# D27 automatic retry capacity correction R1 report

Base: `2729240152697d30c4a077ad0b4f39b7edab03dd`

Review input SHA256: `65ca72d2bc11eb072cff81ddb7bb4392bf3b5990934dae81f54c07301da79973`

## Result

- The worker establishes a remaining provider retry before considering partial import. `replayed`, `existing`, and `deferred` retain metadata/media ledger rows unassigned, create no intermediate `ImportJob`, and return normally. Nonzero, timeout, and unexpected-exception callers use the same fixed-ID dispatch seam. Pause/cancel do not enter the partial-import handoff.
- A later successful attempt uses `_successful_repository_import_plan`; the regression proves a retained metadata artifact is selected, durably assigned, and completed by the real import runner with one `Work` and one `Asset`.
- Exhausted provider failures write `manifest.unresolved_provider_failure`, version 1, with `kind`, sanitized `reason`, `retry_count`, `max_retries`, and `recorded_at`. Evidence is accepted only when its version/state/kind are valid, its retry count exactly matches the current job, and that attempt exhausted the configured budget. A zero retry budget is valid. Successful provider handling removes the field; a changed retry count also supersedes it.
- A successful salvage child remains `complete`, but ordinary import completion projects a valid unresolved provider failure back to the parent `DownloadJob` and `TaskRun`; finalization retains the provider reason and imported counts in a failed repository receipt. Batch reconciliation remains failed and includes the imported work count. An import-only completion therefore cannot turn an exhausted provider failure into sync success.
- Dispatch recovery now returns/counts `error` for non-transient faults. It terminalizes only the exact still-enqueued, still-pending TaskRun/DownloadJob/RQ-ID attempt under locks, persists `dispatch_recovery_error` diagnostics and traceback logging, and leaves provider retry count unchanged. If concurrent state or RQ identity changed, it returns `skipped` without overwriting the winner.
- Temporary classification is limited to the known capacity/lock admission codes and Redis connection, timeout, or BusyLoading errors. Authentication, authorization, command data, generic response, programming, and configuration faults are actionable errors. An existing deterministic RQ record remains publication proof even if its status adapter fails, so known published work is not compensated.

No state-machine/worker runnable guard, global capacity, dependency, migration, runtime configuration, frontend, production, or full-site files changed.

## Tests and evidence

All PostgreSQL runs used both database variables set to `postgresql+asyncpg://agbutton:button-test-db@ag-button-a030-postgres:5432/agbutton_test`, `REDIS_URL=redis://ag-button-redis:6379/15`, and `MEILI_URL=http://localhost:9` in `ag-button-runner`, working directory `/workspace/backend`. Each run used a unique `/dev/shm/auto-retry-r1-*` base temp directory.

Initial RED:

```text
pytest -q tests/test_download_dispatch.py::test_outbox_recovery_does_not_classify_programming_faults_as_capacity tests/test_download_failure_evidence.py tests/test_import_lifecycle_batches.py::test_completed_partial_salvage_keeps_exhausted_provider_failure --basetemp=/dev/shm/auto-retry-r1-red-20260909T022246Z
```

Outcome: 6 failed as expected. Evidence: `r1-red-20260909T022246Z.log`, SHA256 `73853a8b24addd207d75239263e5bb7ab45f323447f2816b5e85827d0a3aaa44`.

Final consolidated application regression:

```text
pytest -q tests/test_download_retry_r1.py tests/test_download_dispatch.py tests/test_download_retry_dispatch.py tests/test_download_failure_evidence.py tests/test_import_finalization_recovery.py tests/test_import_lifecycle_batches.py tests/test_scheduler_batches.py -k 'test_worker_partial_failure_defers_retry_without_starting_import or test_programming_fault_terminalizes_exact_pending_dispatch_attempt or test_outbox or test_publish or ambiguous or terminal or malformed or retry or partial or retained_retry or exhausted_provider or cancelled_parent_blocks' --basetemp=/dev/shm/auto-retry-r1-final-green-20260909T023747Z
```

Outcome: 56 passed, 85 deselected. Evidence: `r1-final-green-20260909T023747Z.log`, SHA256 `60102755c0e79a4440b9acec566769f2581f38e79f83857f9225b34548d56dc9`.

Final Redis classification and persisted error regression after narrowing the Redis exception hierarchy:

```text
pytest -q tests/test_download_dispatch.py tests/test_download_retry_r1.py::test_programming_fault_terminalizes_exact_pending_dispatch_attempt --basetemp=/dev/shm/auto-retry-r1-f2-final-20260909T024014Z
```

Outcome: 23 passed. Evidence: `r1-f2-final-20260909T024014Z.log`, SHA256 `11be565afeae0a0eda533b3732fc7dbe54d5852040cc0fc3c24eb37266842ca2`.

Finalized reviewer actual-caller probe:

```text
PYTHONPATH=/workspace/backend:/workspace/backend/tests pytest -q -o asyncio_mode=auto /evidence/backend-auto-retry/review-partial-callers5-20260909T021306Z.py --basetemp=/dev/shm/auto-retry-r1-review-probe-20260909T023726Z
```

Outcome: 2 passed. Evidence: `r1-review-probe-green-20260909T023726Z.log`, SHA256 `b6f15bc171e5e5d65970db608ecdec4b5e8812c68f06fbe8e0fe58802adf88a1`. The preserved source SHA256 remains `26e736423b4c8380f828633aa82fb4e0a31a0264e262d523d9ab1c9982ca41b0`; its original RED log remains `8db0a9fc859193b8d2300de70cee819f5814d0ca3a372ddecdb554b18a39c840`.

The first external-path invocation omitted `-o asyncio_mode=auto`, so pytest did not load the repository setting and reported two async-fixture setup errors. That harness-only output is preserved as `r1-review-probe-green-20260909T023719Z.log`, SHA256 `467bde8b41925c7f552100f9c29e25555a01521cf419db0e75366737a8f0fcc2`; the corrected command above passed.

Static gate:

```text
ruff check app/jobs/download.py app/services/download_dispatch.py app/services/download_failure_evidence.py app/services/import_lifecycle.py tests/test_download_dispatch.py tests/test_download_retry_dispatch.py tests/test_download_failure_evidence.py tests/test_download_retry_r1.py tests/test_import_finalization_recovery.py tests/test_import_lifecycle_batches.py tests/test_scheduler_batches.py
python -m py_compile app/jobs/download.py app/services/download_dispatch.py app/services/download_failure_evidence.py app/services/import_lifecycle.py tests/test_download_retry_r1.py tests/test_download_failure_evidence.py
git diff --check
```

Outcome: all passed. Evidence: `r1-static-final-20260909T024007Z.log`, SHA256 `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18`.

The PostgreSQL fixture uses tmpfs with `synchronous_commit=off`. These runs establish functional correctness only; they do not support performance or crash-durability claims.
