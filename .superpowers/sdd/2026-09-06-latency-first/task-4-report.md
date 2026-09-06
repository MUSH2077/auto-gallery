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
