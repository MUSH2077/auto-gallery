# Latency-first pipeline implementation plan

Approved by the user on 2026-09-06 after the production diagnosis and latency-priority decision.

**Goal:** remove avoidable minute-scale download/import latency before the Cordis + Python plugin migration.

**Baseline:** deployed source snapshot `eedb6c0`, copied from the complete working tree of `remote-follow-discovery`; the 303 deployed Python files matched that tree. Preserve that source tree and its uncommitted work. Work only in the `perf/latency-first` isolated worktree.

## Global Constraints

- Prefer import latency; preserve durable domain/outbox transactions, execution fencing, conflict detection, and crash recovery.
- Keep gallery-dl extractor/archive/path compatibility and the existing staging manifest format.
- Retain memory/disk/Redis hard-risk admission and exclusive maintenance; do not modify host services, swap, or other projects.
- Use isolated test PostgreSQL, Redis, Meilisearch, and filesystem roots. Never run destructive tests against production services.
- Add behavioral regression tests before each correctness change. Do not substitute source-string assertions for runtime behavior.
- Do not run full media transcodes or wait for indexing/deduplication to complete an import.
- Use existing public task statuses. Additive optional progress/metrics fields are allowed.
- Track remote Meili writes until confirmed; a timeout does not cancel a remote task or permit acknowledging undelivered outbox versions.
- No production restart, migration, or deployment until the candidate and rollback evidence are concrete and reviewed.

## Task 1: Download liveness and fenced task resource reporting

**Own files:** `backend/app/jobs/download.py`, `backend/app/jobs/worker_control.py`, `backend/app/services/tasks.py`, and focused new tests for these changes. Do not edit `heavy_io.py`, `resource_pressure.py`, `import_runner.py`, or staging code.

**Behavior:** after gallery-dl exits, detach its PID from process control but continue the same download heartbeat with the worker PID until promotion, metadata/ledger registration, and durable import handoff finish. Emit an immediate heartbeat when starting/transferring supervision; never signal a reaped or reused child PID. Pause/cancel during finalization must use task checkpoints, not child-process signals. Preserve failure/finally cleanup and avoid a liveness gap when supervision changes. Test finalization lasting beyond the 90-second heartbeat TTL using controlled time.

The importer currently reports resource ownership as `import_uuid:execution_token`. Bridge this to the domain UUID only after verifying the execution token still owns the ImportJob. Reject malformed/stale tokens; keep plain UUID behavior for other jobs. Root will subsequently make UUID/token transmission explicit when modifying the resource API, so encapsulate ownership resolution for that integration. Verify actual TaskRun state updates, including stale-execution rejection.

**Tests:** behavior for post-download heartbeat, immediate first ping, safe PID transfer, final cleanup, pause/cancel checkpoint semantics, valid composite owner, stale/malformed owner, and existing plain UUID callers. Use the isolated runner described in `.superpowers/run-tests`.

## Task 2: Linear staging promotion with durable batch checkpoints

**Own files:** `backend/app/services/download_staging.py` and `backend/tests/test_download_staging.py` (additional staging-specific tests are allowed). Do not edit download job/control or resource services.

Replace per-file full-manifest writes/fsync with a durable recovery plan before mutations, retained staging copies during canonical linking/replacement, directory synchronization once per affected directory, a durable whole-batch promoted checkpoint, then staging cleanup. Keep manifest v1 readable and recoverable, including existing partially promoted v1 jobs. Normal promotion must serialize O(N) metadata in total, not O(N²). Preserve collision rechecks at the mutation boundary, no overwrite of unrelated files, source/creator validation, metadata update auditing, incomplete-file retention, and registered cleanup. Metadata replacement must retain an independently recoverable staged copy until the batch checkpoint is durable.

**Tests:** crash after link/replace, before/after each durable checkpoint, during cleanup, recovery after accepted existing identical files, conflicting targets appearing after preflight, existing old-format jobs, normal new files, metadata updates, incomplete files. Measure fsync/full-manifest write counts on actual temporary-file promotion for 10/100/1000 files; verify output/recovery correctness as well as linear growth. No tests that merely grep implementation text.

## Task 3: Import-first resource scheduling and useful stage measurements

**Own files:** resource governance/worker services, `import_runner.py`, stage metrics/logging, task progress support, their tests; integrate Task 1 ownership resolution.

Separate ingest and background disk reservations, one active heavy operation each; network remains independent. Sum all reservations atomically under the memory floor and preserve POSIX maintenance exclusion. Ingest includes import DB work and first-card attempts; background includes search/media/dedup/Git projection. Prefer ready ingest; allow a waiting background batch at least every 60 seconds under sustained ingestion and noncritical capacity. Remote in-flight search work remains represented in the background budget.

Remove per-slice duty-cycle sleeping from the import critical path, retaining pressure-based bounded work counts. Use resource-release/control events to wake waiters, with a 2-second import fallback poll. Background cooldown belongs in delayed scheduling, never sleeping while owning a worker/transaction/permit. Single-file blocking work is cooperative at safe checkpoints; do not claim cancelling an outer async task terminates threads/subprocesses.

Record queue wait, admission wait, parse, media preparation, DB commit, promotion, and deferred work separately. Keep raw parsing timers inside resource acquisition/release boundaries. Expose job UUID and execution token separately and fence state updates. Keep primary image preparation best effort; defer full video rendering through the existing outbox.

**Tests:** simultaneous ingest/background access without memory overcommit, exclusive maintenance, event wake, missed-event fallback, no import cooldown at 10% scale, 60-second fairness, hard-risk denial, control responsiveness, token-fenced metrics, actual logger output and phase boundaries.

## Task 4: Recoverable nonblocking search delivery

**Own files:** search delivery services/jobs/models, one additive migration, outbox coordinator/health integration, and tests; integrate Task 3 resource APIs.

Persist a delivery receipt with index/action, outbox `(id, version)` snapshot, Meili task identity, state, poll availability, and execution lease. Separate submission, one-shot polling, and successful version-CAS acknowledgment. Normally at most one remote write is in flight. Submission ambiguity may replay the same logical batch idempotently; never advance newer outbox versions on an older acknowledgment. Recover receipts across process/RQ crashes. Pending remote work is not a failure and remains visible to resource/maintenance admission.

One submission covers one index/action and at most 500 documents or 4 MiB. Apply a 20-second local scheduling deadline and bounded remaining-time network/SQL calls. Poll via delayed successors (initially 2 seconds, capped at 15 seconds) without holding an import permit or SQL transaction. Do not release the remote workload's accounting merely because the Python job returns. Keep rebuild and ordinary projection writes mutually ordered.

Select a bounded batch before hydration. Replace scheduler hot-path exact counts with existence probes. Debounce exact index checkpoints until caught up instead of rebuilding counts after every write. Preserve all five index types, deletions, error retry/backoff, replay generation semantics, and search degradation behavior.

**Tests:** 120-second pending Meili response with a completing small import; submit/poll/finalize each bounded; crashes before/after HTTP acceptance and receipt persistence; newer mutation during flight; partial index actions, deletion, transient failures, restart recovery, no concurrent duplicate normal writer, maintenance/remote-flight accounting, outbox existence/wake and checkpoint behavior. Run real isolated PostgreSQL/Meili integration scenarios.

## Task 5: Integration, repeatable latency acceptance, candidate release

Create a repeatable benchmark using isolated services and fixtures matching the real task sizes and approximately 75k works/37k tags. Compare baseline/candidate on the same storage, resource caps, cache-read category, and new/update/skip counts. Use 20 valid repetitions per scenario and record first reads separately. Do not drop host caches or alter other projects.

Normal-resource target P95: task creation <=1s; import 9 works/27 media <=30s; 18/38 <=60s; 4/277 <=90s; promotion/registration of 554 files <=30s. Track active work versus resource/queue delay, CPU, I/O, SQL and durable writes for each module. Mixed-load browse P95 may degrade at most 20% against the equivalent baseline; background queues must progress and drain.

Run relevant behavior suites, migration checks, API checks, lint and full backend regression once the integrated change settles. Obtain independent code review, fix substantive issues, and build an immutable candidate with matching backend/worker source digests plus rollback evidence. Use the existing release workflow only after the candidate is concrete. A 24-hour production observation without new OOM/restarts/stale misclassification/data loss or runaway backlog is the gate before beginning Cordis migration; record this separately rather than claiming it has elapsed.

## Follow-on architecture

After this performance gate, retain the agreed Cordis plugin lifecycle/SDK + Python durable TaskEngine/Postgres/Outbox/RQ split, admin-installed plugins with user source configuration, gallery-dl-compatible external extractors, text/image RAG, tag provenance/normalization, and BYOK agent controls. Carry these benchmarks forward as plugin-migration regression gates.
