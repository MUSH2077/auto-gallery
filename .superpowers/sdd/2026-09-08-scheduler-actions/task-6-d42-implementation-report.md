# Task 6 D42 durable lock-wait implementation report

Date: 2026-09-13

Source implementation commit: `c032d8013dd9a04e898fba3371a7b5ae744acc47` (`fix: persist native admission wait reasons`), based on `59596f8c3e3dbe23acf2c109803ba4414d11599e`.

## Correction

`ResourceAwareWorker._profile_admission()` now sends the existing durable TaskRun bridge the same waiting reason that it records in RQ metadata for the two omitted denial paths:

- a pending maintenance barrier publishes `waiting/maintenance_pending`;
- a denied native profile lock, including the `OSError` fail-closed path, publishes `waiting/profile_lock_busy`.

Both calls pass the resolved owner, workload, and captured `publisher_attempt`. They occur after the RQ metadata update and before the cooperative wait. The native-lock branch still releases its acquired Redis lease before either projection and before waiting. No queue, concurrency, resource-cap, attempt-fencing, lease, lock, retry, or admission policy changed.

Committed source hashes:

- `backend/app/services/resource_aware_worker.py`: `6b1a30fe87ce46d047bba936b93390152a1a57ad50f792312994aa67d9293977`
- `backend/tests/test_resource_aware_worker.py`: `6e7e6dbe7f2bb0df4aa3c4040fdf13b4b29f1878cad8782534de02778fca8ce6`

The focused tests drive the real admission loop with a registered admin delivery, so owner and publisher-attempt extraction remain real. The maintenance case clears its barrier on the second loop. The native-lock cases acquire a controlled Redis lease, deny once by false return or `OSError`, grant the next lock, and assert the observed order `lease release -> durable waiting projection -> cooperative wait`. The RQ metadata projection is also retained and ordered before the durable projection.

## RED/GREEN and regression evidence

Evidence directory: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/fullsite-acceptance/d42-tests`.

Focused selection:

```text
python -B -m pytest -p no:cacheprovider tests/test_resource_aware_worker.py::test_profile_admission_persists_maintenance_pending_before_wait tests/test_resource_aware_worker.py::test_profile_admission_releases_lease_then_persists_lock_wait_before_wait
```

RED against the pre-fix source: **3 failed in 10.64s**, exit 1. Each case reached the expected admission path and failed because the waiting durable tuple was absent; no infrastructure error occurred.

- `d42-red.log`: `f712d39c02748b5c34968b94b1d6b30b88e7282c33a1899fd8f4f258f7133de7`
- `d42-red.xml`: `05a2ee40cd19d6ce567dd6a758ff935531655346b40b2539f4cc958ccb5c3331`
- `d42-red-result.json`: `5c823dc99e87097a9cf28c2eac3113556122a2088ca2c09452298819e51d4a30`

GREEN against the corrected source: **3 passed in 1.55s**, exit 0.

- `d42-green.log`: `079d6939c4e1e9b391d45e162e5faf786111c2fa15f9e2e0d015ecce43cb8bd4`
- `d42-green.xml`: `a75f5f9b190fad4db97615ea4270d9d79869e69646167a98bb0d3ea077928258`
- `d42-green-result.json`: `b4c2055f201ea6da1f782896414c8a856eaaf0bc2e8fdcaa0f5f30a630b11793`

Focused regression selection covered all of `tests/test_resource_aware_worker.py` and `tests/test_task_resource_owner_fencing.py`: **50 passed in 105.27s**, no skips, exit 0. This includes the existing database-backed stale/current publisher-attempt fences.

- `d42-regression.log`: `787a4477cf912f5e8f9320c716414cd7b1e8cc80a3126e120f4d86ea689d0614`
- `d42-regression.xml`: `565a33e75b806ce717e8325f4f64ff12fd16c64d5dce1f4acd1bfd28059da227`
- `d42-regression-result.json`: `c6cb84837a91398905460288abfe121bf275f2cb7858131c539331081f79c5c4`

Root also ran Ruff on the two changed Python files with exit 0. Host `python3 -m py_compile` and `git diff --check` passed. Pytest emitted the runner's existing `Unknown config option: cache_dir` warning after the cache provider was disabled; it caused no skip or failure.

## Self-review and remaining gates

Self-review found the diff limited to the two diagnosed branches and their behavioral tests. The already-correct memory-reservation, Redis-lease, running-state, release, and attempt-fencing paths are unchanged. The pre-existing dirty root plan `docs/superpowers/plans/2026-09-08-scheduler-actions.md` was neither staged nor committed. No Docker, API, database, Redis, browser, lock, or application runtime action was performed by the implementation agent.

D42 unit and focused regression coverage does not complete native D40 acceptance. Root must freeze and review a new exact-source backend image, perform the reviewed idle same-schema replacement, and run both native tracks. Track A still requires A1 estimate contention through durable `waiting/profile_lock_busy`, same-attempt completion, and terminal release, plus A2 real validation failure with observed exclusive ownership and exception-path release. Track B still requires the disposable old-RQ-metadata rolling-compatibility case. The earlier Track A R2 stopped after the A1 mismatch and created no A2; it must remain preserved as failed evidence rather than repaired in place.
