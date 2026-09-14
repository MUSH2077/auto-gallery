# Task 6 D43 implementation report

Date: 2026-09-14

## Outcome

D43 adds a best-effort wake after accepted immediately runnable work for the
heavy RQ queue families. The producer and consumer now agree on these channels:

| Queue family | Worker idle profile and channel |
| --- | --- |
| `imports` | `import_db` / `resource:work:import_db` |
| `downloads`, `downloads:*` | `download_network` / `resource:work:download_network` |
| `operations`, `maintenance` | `light` / `resource:work:light` |

`scheduled`, `discovery`, the default queue, and delayed `enqueue_in` work do
not emit an immediate-work wake. Notification failure does not change an
accepted enqueue or recovered-existing result.

The worker binds its cached pub/sub client to the requested workload. A
workload transition closes the old client and creates one subscribed to only
`resource:control` and the current work channel. Close clears the workload
marker, and a failure during initial subscription closes the just-created
client instead of leaking it.

The responsiveness change exposed an import publication race. The exact-ID
ImportJob claim now waits for a publisher-held row lock and re-evaluates the
runnable-status predicate after release. It retains the parent -> ImportJob ->
TaskRun lock order, execution-token fence, and one-claim behavior. Existing
delivery notifications in async import/admin recovery paths run in
`asyncio.to_thread`, so synchronous status refresh and Redis publish cannot
block their event loops.

## Changed files and reviewed byte identities

| File | SHA-256 |
| --- | --- |
| `backend/app/jobs/import_runner.py` | `6a6860de6d80af091181fc77f68fd784b6b8e60960543d58e6e5e61956dbf2df` |
| `backend/app/services/backpressure.py` | `12e160e1fe25bd08cadc04e1f3e1cf57a4d605b5308711dd902876310b42794f` |
| `backend/app/services/import_dispatch.py` | `6e76cf73875721c785f031199d96966b54041c3ae5d58051fcc91900f9adf958` |
| `backend/app/services/operations.py` | `1ab2b33f8f54ce423bb42c9592db64250dcd201e800b26c9fabde2d2178742dd` |
| `backend/app/services/queue_admission.py` | `1c39280d973b725c56bbca231c0727ae85c721b1d5ac7721f3d8e2afd1e29260` |
| `backend/app/services/resource_aware_worker.py` | `2e274243a6f724400259d131bedb0710ca4c2e2677136299191caa7460fd7094` |
| `backend/tests/test_admin_operation_dispatch.py` | `592dbe8788258594c9561b46a672ce9ced0e7062dd2e3eb2fd8e6bc7ca2747b1` |
| `backend/tests/test_download_dispatch.py` | `da607881c5affd4d290bcf0750a079b4ac79b1c251c44d0b9c54b6e15b2a2a20` |
| `backend/tests/test_import_lifecycle_batches.py` | `c2b1c0ea67641d1269ed607c526f323eb829b573a5424986a80d3060cad683b1` |
| `backend/tests/test_queue_admission.py` | `5a420e717731ed3d062073c78682e8ccb017e7dddbebcc1cf33527c7702fe72c` |
| `backend/tests/test_resource_aware_worker.py` | `ebb312405f5a7fc29307fd40598812599526aaa80f174253a090c8b5376bcb41` |

The unrelated dirty file
`docs/superpowers/plans/2026-09-08-scheduler-actions.md` was preserved and is
excluded from D43.

## TDD and verification evidence

The implementation author performed static reads and diff checks only. Root
owned all pytest, PostgreSQL, Redis, Docker, API, and runtime actions. Evidence
is retained under
`/volume2/docker/auto-gallery-button-audit-artifacts/20260908/fullsite-acceptance/d43-tests`.

| Stage | Root-observed result | Evidence |
| --- | --- | --- |
| Producer RED | 17 failed, 6 passed for the focused producer behaviors | `d43-red-result.json`, `d43-red.log`, `d43-red.xml` |
| Producer GREEN | 23 passed | `d43-green-result.json`, `d43-green.log`, `d43-green.xml` |
| Cached-channel RED | 3 expected assertion failures | `d43-consumer-red-result.json`, `d43-consumer-red.log`, `d43-consumer-red.xml` |
| Cached-channel GREEN | 3 passed | `d43-consumer-green-result.json`; XML SHA-256 `03656d006dad385f6d68320098430dcdbeb914c6ab28a8ce33a15f31d2f781d7` |
| Claim/event-loop RED | 5 expected failures: two missing PostgreSQL lock waits and three blocked event loops | `d43-race-eventloop-red-result.json`; XML SHA-256 `b2255949771a10dd8c7deb68663a60f2a00795caff99f190cb8fc1af2bb91bb2` |
| First claim/event-loop GREEN | 4 passed; one test-only stale-dictionary assertion failed and was corrected to inspect `task.meta` | `d43-race-eventloop-green-result.json`; XML SHA-256 `dacb8450fa2330217de287d41964a17500ee75f06677b67f548e2163eb49ecf4` |
| Subscribe-cleanup RED | 1 expected failure because the new pub/sub client remained open | `d43-subscribe-cleanup-red-result.json`; XML SHA-256 `64cace8928dcacea04875503b7289a57c265bd860fc99cf4ab1af4184fedbd40` |
| Final five-module pytest | 188 passed, 0 skipped, 369.38 seconds | `d43-full-regression-result.json`; XML SHA-256 `d5fe07a49541dd22b330c4c558549173cae60aeb04d25d5ed93d9428830221ef`; log SHA-256 `fa8bab3f83670f047f927112cc20dbbc84ec904190082c105655f3b1be074df8` |
| Ruff on all 11 changed source/test files | passed | `d43-ruff-result.json`; result SHA-256 `b36bcdf566c219bdab5d5391666a028b1c0692ddc1fdebbb756a4d5d17ec0096` |

The real PostgreSQL regressions use separate publication and claim sessions,
observe the claimant in `pg_stat_activity` with `wait_event_type = 'Lock'`, and
then verify one claim with the unchanged dispatch attempt/ID or no claim after
cancellation. The event-loop regressions stall and fail the synchronous Redis
publish until another coroutine runs; they require both loop progress and the
preserved `existing` result/published metadata.

## Self-review

The final source review in `task-6-d43-final-source-review.md` reports SOURCE
PASS conditional on the final full pytest run. It found no remaining important
source issue. The review covered producer/consumer channel agreement, delayed
work, ambiguous/existing deterministic deliveries, publish-failure identity,
publication/claim lock ordering, async notification latency, cached workload
transitions, and failed-subscribe cleanup.

This change does not alter queue limits, Redis capacity rules, deterministic
job IDs, retry/claim fencing, lease ownership, resource profiles, worker
concurrency, adaptive polling caps, container limits, configuration, or
dependencies.

## Limitations

Unit and integration tests do not establish real enqueue-to-claim latency.
Redis pub/sub remains a best-effort responsiveness hint; the existing adaptive
timeout remains the recovery path. Scheduler promotion of delayed heavy work
still has no custom-channel notification and remains outside this immediate
enqueue correction. Controlled root runtime checks are required before making
any latency claim for the three distinct consumer channels.

## Root verification completed

Root verified all 188 JUnit cases have no failures, errors or skips. The exact
11 source/test file hashes match both the independent source review and Ruff
receipt. This satisfies the source review's final-test condition. The sole
pytest warning is the existing `cache_dir` setting while cacheprovider is
explicitly disabled. Real latency and whole-site acceptance remain pending.
