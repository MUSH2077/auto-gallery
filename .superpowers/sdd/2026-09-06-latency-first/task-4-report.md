# Task 4 delivery report

## Task 4A — ordinary durable delivery (complete; 4B rebuild continuation remains)

Implemented frozen payload/version receipts with an active-singleton index, 30-second execution leases, a 20-second local scheduling deadline, remaining-budget SQL statement/lock timeouts, and async HTTP calls capped at five seconds. One job submits one settings/document task or polls once; initial polling is delayed two seconds and doubles to a 15-second cap. Pending/network-read failures never increment outbox attempts. Terminal success atomically CAS-acknowledges the exact saved versions and commits the receipt; terminal remote failures retain outbox work with bounded backoff. Payloads are dropped at terminal completion while identity/version evidence remains.

Selection is capped at 500 rows before authoritative hydration, restricted to one index/action, and frozen wire payloads are capped at 4 MiB. Missing hydrated entities become deletions. All five projection builders remain in use. Settings initialization is its own durable task, never an implicit blocking SDK wait. Exact index count checkpoints happen in an idle, caught-up pass with generation CAS and a 30-second debounce. The 15-second scheduler now uses existence queries; diagnostic APIs retain exact counts. RQ parent heavy admission is bypassed for the search coordinator; its child applies admission only to new writes.

A fsynced marker on the existing shared downloads lock volume precedes every remote submission and includes receipt ownership. A returned Meili task identity is fsynced before the SQL response checkpoint. Terminal SQL commit precedes marker removal, so receipt and marker recovery cover either side of each crash. Marker operations finish before caller cancellation releases local locks. Every Redis admission restores a no-expiry remote reservation under a short independent marker flock. Background and maintenance also inspect the marker after taking their local locks. Import/network reservations remain available subject to the aggregate memory floor. Redis loss cannot erase remote workload accounting.

An unknowable POST outcome stays explicitly ambiguous. Root approved fail-closed ambiguity: no guessed time/index/count matching and no blind replay. Health includes the receipt identity and recovery command. `python -m app.services.search_delivery reconcile --receipt <uuid> --task-uid <verified_uid>` attaches an operator-verified task after checking index/type and execution lease; subsequent failed/canceled remote tasks automatically follow ordinary retry. This requires deliberate verification from remote task/request history. No generic unsafe "assume canceled" override is provided.

The rebuild entry currently rejects unresolved ordinary receipts and serializes its active stack against ordinary writers. This is an intermediate 4A boundary, NOT the final rebuild recovery solution. Per root ruling, 4B will replace the long-lived rebuild stack with durable continuations and remove rebuild worker cooldown sleeps before the candidate is complete.

## Verification / TDD evidence

All pytest commands used `.superpowers/run-tests`, serialized inside `ag-latency-runner`; no production tests, services, or data were used.

RED examples (each followed by implementation and a focused GREEN rerun):

- Initial `-q tests/test_search_delivery.py --disable-warnings --maxfail=1`: missing delivery service import (expected missing persistence API).
- Resource restart/fencing test: `assert not maintenance.try_acquire()` failed because maintenance ignored remote work.
- Settings-task test: first write incorrectly ended in `/documents/delete-batch`, expected durable `/settings` task first.
- Crash after accepted response: recovered status was `ambiguous`, expected known task recovery via durable identity checkpoint.
- Operator reconciliation test: missing `reconcile_task` API.
- Caught-up checkpoint test: zero stats requests after completion, expected one deferred checkpoint.
- RQ parent test: heavy-pressure admission was invoked for the poll coordinator.
- Fsync cancellation and terminal payload tests: absent cancellation-safe durable call and retained terminal payload.
- Initial rebuild admission regression could wait indefinitely; the isolated pytest child was stopped inside its container (SIGINT then SIGTERM), then an explicit preflight rejection and bounded regression test were added. No orphan pytest was left running.

GREEN final focused integration command:

```
.superpowers/run-tests -q tests/test_search_delivery.py tests/test_resource_pressure.py tests/test_resource_aware_worker.py tests/test_search_algorithms.py tests/test_outbox_successor_contract.py --disable-warnings --maxfail=1
165 passed in 22.46s
```

This includes real isolated PostgreSQL and Meilisearch tag upsert/deletion/settings initialization, additive migration upgrade/downgrade/upgrade and active-receipt downgrade refusal, same-version/newer-version acknowledgment, duplicate worker race, 500-row hydration/4 MiB wire limits, pending/error/restart paths, explicit task attachment, and an eight-poll controlled 120-second pending timeline with completing ingest-lane SQL mutations. Root is adding the actual complete small-import benchmark during a controlled pending task; the test here does not claim a complete importer execution.

Real Meili initially failed because the isolated server OOM-exited under its initial 512 MiB limit. Root recreated only that isolated service with the actual production limits (1 GiB, 0.4 CPU, 320Mb internal indexing memory, one indexing thread), after which the real integration passed. Production was unchanged.

`ruff check --no-cache` on new receipt/model/marker/job/migration/tests passed. `git diff --check` passed. The old outbox successor source-string assertion was replaced with a runtime delayed-poll contract; an obsolete rebuild source-string test was removed in favor of runtime writer exclusion and upcoming 4B keyset tests. No full backend suite has run (reserved for Task 5).

## Migration/recovery caveats

- Apply `f8d0e2a4b6c8` after `d0f2a4c6e8b1` before enabling this code. Downgrade refuses active receipts. The migration is additive.
- All relevant processes must continue sharing the actual `HEAVY_IO_LOCK_PATH` / downloads lock directory. Independently deleting/restoring this directory while remote work exists is outside supported recovery; restore receipt/marker evidence together.
- Unresolvable request ambiguity intentionally blocks background/maintenance until a verified identity is supplied. Ordinary imports continue within memory limits.
- The 20-second scheduling bound governs SQL/HTTP/local work scheduling; an already-running filesystem fsync is cooperatively completed before releasing the lock, since cancelling an asyncio wrapper cannot cancel storage I/O.
- Full rebuild durable continuation is still pending in 4B; this intermediate commit is not a deployable final candidate.

## Files

Receipt model/registration and additive migration; `search_delivery.py`; `remote_search_flight.py`; ordinary search RQ/service routing; heavy resource and worker integration; outbox coordinator/health and main existence-probe call; focused delivery, resource compatibility, and successor tests.

## Task 4B — durable rebuild continuations and review corrections

The 4A rebuild limitation above is superseded. Rebuild now persists an active singleton workflow with settings, bounded UUID keyset build, bounded mutation replay, live-index creation, one multi-index swap, version-CAS acknowledgment, and old-index cleanup phases. Every remote command uses the ordinary receipt/marker engine. Its frozen continuation advances only in the terminal-success transaction. Replay versions are accumulated in a separate table with bulk upserts and acknowledged in chunks of 500 only after confirmed swap success; newer versions remain pending. Ordinary writes wait behind the active rebuild across successor processes. An accepted swap recovered from its fsynced identity is polled rather than submitted twice.

The old synchronous rebuild stack and its worker cooldown sleeps were removed. `SearchService.reindex` and `refresh_works` return `pending` with a build identity; callers inspect persisted status. Registered admin operations persist a delayed fenced handoff and return the RQ slot. The existing due-dispatch reconciler publishes that handoff when due: calling `publish_admin_operation` immediately would discard its delay. The search outbox supplies regular remote polls. Legacy task owners are completed by the final durable phase. Owner cancellation stops new commands after any already-accepted task has been settled. Oversize documents and replay-limit errors fail the build durably; old-index cleanup errors retain a warning while preserving a successful swap.

The three Important 4A review findings are fixed:

1. Marker creation installs the nonexpiring Redis reservation under the marker guard. Ingest admission holds that same guard through reservation restoration and its memory-grant Lua, closing the paused-before-submission interleaving.
2. Durable `write_available_at` gates new writes independently of remote polls. Even a 45-second profile cooldown leaves polls within 2–15 seconds; successful settings tasks release remote accounting before waiting to submit documents.
3. Terminal remote failure computes backoff from the CAS-matched outbox's accumulated attempts, preserving 5/10/20-second progression across separate receipts. Pending polls remain non-failures.

Both parent worker pressure gates now exempt only the lightweight search coordinator. The child still admits new work under the resource profile; pending-task polling can release remote accounting while the host is under pressure.

Additional self-review fixed initial rebuild receipt leases (same 30 seconds as ordinary delivery), restored remaining SQL timeouts after the ensure-live HTTP transaction boundary, and refreshed an ORM server timestamp asynchronously before finalizing legacy owners.

### 4B RED/GREEN and integration evidence

All pytest still uses the isolated serialized wrapper. Behavioral RED cases included missing rebuild API / missing admin handoff; replay acknowledgment occurring before swap; missing bulk replay persistence; memory admission succeeding in the marker-transition race; a 45-second first remote poll; retry backoff resetting to 5 seconds; parent hard-pressure gating a coordinator; and absent initial lease / zero SQL statement timeout after rollback. The final combined run also exposed an expired timestamp causing `MissingGreenlet` on legacy-owner completion; the focused fix passed.

Focused GREEN runs before final consolidation: 180 resource/search tests passed in 86.10s; 27 existing search tests passed in 37.16s, including real asynchronous all-index rebuild and audit; 3 final lease/SQL-budget/legacy-owner regressions passed in 2.44s. Ruff passed for every touched Python path. The real PostgreSQL/Meili cases include ordinary upsert/deletion, a real staging rebuild and swap, all-five-index continuation, replay races, crash after accepted swap, admin pending/ambiguous handoffs, migration downgrade refusal, and restart recovery. The controlled 120-second pending case continues proving ingest-lane SQL progress; root owns the complete small-import acceptance run during remote pending in Task 5.

### 4B migration and recovery notes

Apply additive `f9e1a3b5c7d9` after `f8d0e2a4b6c8`: new build/replay tables and nullable receipt continuation/cooldown columns. Downgrade refuses active rebuilds or receipts. Fresh builds advance search generations so a caught-up checkpoint verifies the new live index. Downgrade/upgrade roundtrips were exercised in isolation.

Before candidate cutover, drain old synchronous writers and their outstanding Meili tasks. New durable receipts cannot infer pre-migration remote tasks. Retain the shared persistent lock directory and its marker alongside SQL recovery evidence. Unknown POST outcomes still require deliberate attachment of an operator-verified task UID; this is the approved fail-closed limitation, not permission to replay or expire accounting. Storage fsync completion may cooperatively outlast the local scheduling deadline. No deployment or full backend regression claim is made here; full-suite and workload acceptance remain Task 5.

Final consolidated command:

```
.superpowers/run-tests -q tests/test_search_rebuild_delivery.py tests/test_search_delivery.py tests/test_search.py tests/test_resource_pressure.py tests/test_resource_aware_worker.py tests/test_search_algorithms.py tests/test_outbox_successor_contract.py --disable-warnings --maxfail=1
210 passed in 146.00s (0:02:25)
```

One additional writer-contention regression then demonstrated that a busy local writer could bypass the registered admin handoff (`KeyError: _admin_handoff`). The admin handler now persists the same delayed handoff for this transient condition; it does not terminally fail a queued rebuild because a poll owns the writer lock.

Post-contention-fix verification: `.superpowers/run-tests -q tests/test_search_rebuild_delivery.py --disable-warnings --maxfail=1` — **16 passed in 50.16s**. Ruff on the last changed handler/test and `git diff --check` passed. Status: **Complete with concerns** (approved unresolved-POST ambiguity; full-import/120-second pending acceptance and full backend regression remain root-owned Task 5).
