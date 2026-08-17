# Task 6 report — scoped/global backlog drain and lifecycle hardening

Implementation commit: `dadcb07` (`feat: harden scoped import backlog lifecycle`)

## RED evidence

- Compaction tests initially deleted both an `attention_state=open` task and a
  DownloadJob with a `failed` artifact (`deleted_tasks == 1` / `deleted_download_jobs == 1`).
- Reconciliation initially marked a failed task recovered after a later receipt
  despite `no_changes` plus repository backlog, and auto-resolved a failed
  orphan receipt (`recovered == 1`).
- Pausing a failed task reached an uncaught `InvalidTaskTransition`, which
  would surface as a 500 rather than a structured conflict.
- Scoped disk import initially had no `scanned` counter and accepted a
  repository/source mismatch without raising.
- A per-creator enqueue failure initially escaped the disk drain and aborted it.
- An async progress callback was initially not awaited; its report list stayed
  empty and pytest reported an un-awaited coroutine.
- The frontend action-state test initially failed because the shared action
  helper did not exist.

## GREEN evidence

- `pytest -q tests/test_operation_attention.py` — **13 passed** (88.79s)
- `pytest -q tests/test_disk_import.py` — **8 passed** (86.18s)
- `pytest -q tests/test_tasks.py` — **6 passed** (32.88s)
- `node --no-warnings --experimental-strip-types tests/test-task-actions.mjs` — passed
- `npm run typecheck` — passed
- `python -m compileall -q app` and `git diff --check` — passed

Every backend run used a fresh disposable `autogallery_task6_test` database;
it was dropped after verification. No contracts were generated, deployed, or
published.
