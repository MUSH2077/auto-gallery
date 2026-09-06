# Latency acceptance method

The approved latency-first plan remains the acceptance contract. This document
defines how the isolated measurements are produced; it is not a passing report.

`python3 scripts/latency-acceptance.py setup --candidate <commit>` creates an
internal Docker network and NAS bind storage below the ignored, marked
`.superpowers/latency-acceptance` directory. It snapshots baseline `eedb6c0` and
the requested candidate commit. It never mounts production data, connects to
production services, changes host caches/swap, or invokes pytest.

PostgreSQL uses 0.45 CPU / 768 MiB and synchronous_commit=on. Meili uses 0.4 CPU /
1 GiB, 320Mb indexing memory, and one indexing thread. Both code variants use
the same one-CPU / 1536-MiB worker cap. These worker caps bound the experiment;
the originally deployed import worker had no explicit CPU/memory cap. Redis
uses an isolated persistent AOF directory. No host or other-project tuning is
part of this experiment.

An identical PostgreSQL template is cloned for each variant after migration,
seed, and ANALYZE. It has 75,000 works, 37,000 tags, 450,000 work-tag relations,
90,000 assets, and 330,000 registered artifacts. This synthetic scale fixture
does not claim to reproduce the production distribution of raw metadata,
curation history, or media formats. Separate behavior tests cover crash,
update, skip, concurrency, and video deferral cases.

`python3 scripts/latency-acceptance.py run --repetitions 20` alternates baseline /
candidate order across the 9/27, 18/38, and 4/277 work/media shapes. Repetition
zero is recorded separately as warm-up. Each subsequent repetition uses new
provider identities. Each asset has provider-compatible sidecar metadata, so
the 4/277 case has 554 files for promotion and registration. JPEG inputs are
deterministic textured 1300x1900 images, freshly written before application
reads. Host and PostgreSQL caches are allowed to behave naturally; the results
must not be described as a forced cold-cache benchmark.
All pages within a trial share the same deterministic image content; this
exercises import/page bookkeeping and image decoding, not media-diversity or
deduplication throughput.

The driver executes the real DownloadStage promotion, provider grouping,
ArtifactLedger transaction, durable _enqueue_import publication, and ordinary
run_import_job coroutine. It removes the published job from the isolated RQ
queue before invoking that coroutine. Creation time therefore includes the
durable intent and Redis publication, while import time starts at execution;
RQ process startup/queue backlog are not included. The task-creation API and RQ
recovery contracts have separate integration checks.

During imports the real authenticated /api/v1/works route is called at a
bounded rate in a separate API container (0.5 CPU / 512 MiB), including
permission, query, response serialization and internal HTTP work. This avoids
sharing the importer's Python event loop with API requests. A separate probe
process also keeps client requests and pressure sampling off that event loop;
it shares the bounded worker CPU allocation, so measured HTTP time can still
include client scheduling under CPU contention. The original
deployed API also had no explicit CPU/memory cap. Its first request is warmed
outside the timed import and recorded separately. Startup background loops
are disabled in this query fixture. The measurement excludes external
reverse-proxy/network/browser rendering latency. HTTP
errors invalidate a trial. Every trial verifies terminal completion and exact
work/asset counts. Timed phases include CPU, process I/O, SQL and commits, with
an independent flat SQL/commit observer. Every imported media file is checked
against the fixture SHA-256 after timing. Import timing stops before joining
the observers. The ordinary resource sampler runs at its configured 5-second
cadence from before promotion through the end of import; original sample
timestamps and phase boundaries are retained. Critical/paused intervals must be
reported separately, not used as normal-resource SLO observations.

A separate `pending` driver holds controlled HTTP task status in processing
for 120 real seconds while a complete small import runs against NAS PostgreSQL.
It checks that maintenance remains fenced, one remote write is submitted,
imports finish during the pending interval, poll calls return promptly, and
the exact outbox version is eventually acknowledged. This is a controlled
remote-duration test, not a claim that Meili consumed 120 seconds of real CPU;
real Meili delivery/rebuild and backlog draining are separate checks.
This case requires a fresh candidate database clone with no existing pending
outbox rows. It asserts the selected receipt contains the intended row/version
and every local delivery call returns within 20 seconds.

Each measurement is retained as JSON plus full logs, with immutable source
revisions in state.json. Final release evidence must name the tested final
revision, migration results, full regression result, candidate image/source
digests, rollback path, and the separately timed production observation.
The manifest pins the driver hash, orchestration hash, and a unique run ID.
Resumed measurements and summary inputs must all match that seal. After
preliminary smoke testing, `seal --candidate <commit>` archives preliminary
reports, data, candidate source, and database names, then creates fresh matched
database clones, data roots, Redis namespaces, and search index prefixes.
No final repetition can be combined with earlier smoke data or another
harness version.
