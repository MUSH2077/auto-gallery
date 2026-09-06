# Full backend regression fixture corrections

Root's isolated full backend regression recorded **8 failed, 1804 passed, 9 skipped in 2385.10s**, with all failure stacks in `full-backend-regression.log`. This package changes only six test files; application code, migrations, configuration, NAS services, and production data remain unchanged.

The four migration failures expected obsolete head `d0f2a4c6e8b1`. Their reviewed single-head expectations now use `f9e1a3b5c7d9`; model nullability, fresh database upgrade, ownership, and downgrade safety checks remain intact.

The two NSFW unit tests called the public search entrypoint with `db=None`, which failed in exact creator alias lookup before reaching filter composition. That alias lookup and parsed Meili seam already exist in baseline `eedb6c0` (verified with `git show`). The tests now pass a real parsed works query to `_search_meili`, preserving assertions on the filter actually emitted to the fake SDK: default visible-only filtering, plus `is_nsfw = false` when requested. Existing HTTP/DB visibility integration cases in the same file remain unchanged.

The Danbooru cache invalidation test now isolates alias backfill at its service boundary, alongside its existing projection isolation. Its small fake DB remains focused on import creation; creator/subscription IDs, link/source counts, success, and cache invalidation assertions are unchanged. `danbooru_import.py` and `creator_aliases.py` have no diff from baseline `eedb6c0`.

The private-auth canary test's control/heartbeat fake now supports child-PID detachment and heartbeat transfer. Detachment fails for an unexpected PID and clears the expected child. An added assertion checks the production caller detaches that child before transferring heartbeat to the worker PID. All original failed-job, durable/Redis/log redaction, private-config removal, and public-config cleanup assertions remain intact.

The recorded full-suite failures provide RED evidence. An intermediate affected-file run also reproduced the original cache fake failure (35 passed, 1 failed) before its replacement fixture was loaded. Final verification uses only the six affected files through the serialized `.superpowers/run-tests`; the full suite is not repeated. The original 1804 passing tests remain root's unchanged-application evidence.

Final GREEN command and exact output:

```
.superpowers/run-tests -q tests/test_download_source_nullable_migration.py tests/test_durable_task_ownership.py tests/test_subscription_source_uniqueness_migration.py tests/test_nsfw_filter.py tests/test_operations.py tests/test_shared_remote_scheduling.py --disable-warnings --maxfail=1
81 passed in 47.30s
```

Ruff `--no-cache` on all six changed test files: **All checks passed!** `git diff --check` passed. The rerun transcript is `full-suite-fixture-rerun.log`. Status: **Complete**. No application bytes changed; root can combine this affected-file result with the prior full-suite passing coverage and independently review the test-only diff.
