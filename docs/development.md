# Development Guide

Read [CONTRIBUTING.md](../CONTRIBUTING.md) first for the contribution workflow,
required checks, privacy rules, and pull request expectations.

## Repository map

```text
auto-gallery/
  backend/
    app/
      api/            FastAPI route modules
      models/         SQLAlchemy models
      repositories/   database access
      schemas/        API contracts
      services/       business and orchestration logic
      providers/      source-specific URL and metadata adapters
      jobs/           background job entry points
    alembic/          database migrations
    tests/
  admin-web/
    src/
      app/admin/      App Router pages and layouts
      components/     shared UI primitives and domain components
      lib/api/        typed API client, generated and hand-written types
      lib/            auth, i18n, navigation, theme, preferences
    tests/e2e/        Playwright browser coverage
  docs/
  scripts/
  docker-compose.yaml
```

The live Docker Compose services are:

- `postgres` and `redis`
- `backend`
- `worker-download`, `worker-import`, and `worker-operations`
- `scheduler`
- `admin-web`

The API and Python workers share the backend image. Only the download worker
executes gallery-dl.

`MEDIA_WORKER_CONCURRENCY` configures both the operations consumer count and
image execution slots (1 through 8, default 1). Apply the same value to the API
and all worker roles and recreate the application containers after changing it.
Ordinary and large images share these slots. Video, indexing, deduplication,
maintenance, remote indexing fences, and import priority retain their existing
resource rules. In parallel mode each image reserves at least 768 MiB; the
available memory can therefore allow fewer images than the configured maximum.
Increasing the value does not configure upload, download, or import workers.
Use the same total operations database connection budget when comparing counts,
and limit VIPS/OpenBLAS/OMP/MKL threads to avoid competing nested thread pools.

Select a value using the device's CPU, available memory and measured workload;
the default is not a hardware recommendation. The regression suites
`test_parallel_media_slots.py`, `test_parallel_media_wakes.py` and
`test_parallel_media_rq.py` cover resource admission, wake ownership and real RQ
execution. Run them against disposable PostgreSQL and Redis instances.

## Backend

### API worker concurrency

The Compose backend uses `backend/api_entrypoint.py`, which starts Uvicorn's
native worker supervisor. `API_WORKER_CONCURRENCY` defaults to 1 and accepts
1 through 4; invalid values fail startup. Before the application is imported,
the launcher divides the total API budgets by floor: database 8 steady plus 4
overflow connections become `1 = 8 + 4`, `2 = 4 + 2`, or `4 = 2 + 1` per
process, while total `MEILI_SEARCH_CONCURRENCY=4` is divided per process with
at least one slot. Direct multi-worker `uvicorn` bypasses this setup and is not
an approved deployment command.

`API_THREAD_POOL_SIZE` defaults to a total of 16 blocking-thread slots and is divided by floor (1 = 16, 2 = 8, 4 = 4) before Settings is imported. The per-process cap applies separately to the asyncio executor and the AnyIO thread limiter; these are independent pools, so 16 is not their combined thread limit. API1 keeps the 16-slot cap even though it bypasses the Redis maintenance lease.

Every process may serve foreground HTTP and WebSocket traffic. A Redis lease
elects one maintenance owner and fences ownership changes; shared health and
resource snapshots are read by the other processes. Foreground metrics are
aggregated at low frequency, with no Redis operation on the normal request
path. Keep the default at one unless the target device's CPU, memory and
connection budgets have been selected deliberately.

Increasing API worker count does not increase the container CPU quota. Select the worker count together with its CPU and memory limits, then verify it under the target workload.

### Download concurrency

Download worker startup, child admission and health use
`services/download_concurrency.py`. The existing database setting stays
restart-applied and bounded by deployment and device limits; the optional
`DOWNLOAD_DEVICE_CONCURRENCY_LIMIT` overrides downloads only. The internal
`DOWNLOAD_EXECUTION_CONCURRENCY` value is supplied by the supervisor rather
than configured independently. Health distinguishes desired and running
consumer counts; resource lease accounting separately reports active network
reservations.

Run `test_download_concurrency_config.py`, `test_worker_concurrency.py`,
`test_parallel_download_slots.py` and `test_download_archive_identity.py`
against disposable PostgreSQL and Redis. They cover defaults, caps, shared
archives, memory admission, key loss, pool decreases, cancellation and process
exit. Existing download lifecycle/finalization regressions continue verifying
process termination and reliable import handoff. See
[deployment budgets](deployment-profiles.md#download-worker-concurrency--下载并发).

### Import concurrency

`IMPORT_WORKER_CONCURRENCY` configures 1–4 import consumers, defaulting to one.
The supervisor uses the environment value when no explicit `imports N` argument
is supplied and reports queue-specific target and live counts. Maintenance is
an independent consumer; restart and scheduler ownership remain queue-specific.
The parameter changes consumer count rather than the shared ingest slice limit.

Run `test_import_concurrency_config.py`, `test_worker_concurrency.py` and
`test_parallel_import_execution.py` against disposable PostgreSQL and Redis.
The parallel execution tests use independent processes and database sessions to
verify duplicate claims, shared work identities and stale execution ownership.
Retain the existing lifecycle, finalization, resource-lane and after-commit wake
regressions. Compare job completion separately from media and search readiness,
with the same per-process connection pool and container resource limits. See
[import deployment budgets](deployment-profiles.md#import-worker-concurrency--导入并发).

### Search delivery confirmation

After a search write, delivery persists both the remote task UID and its pending
SQL receipt before checking completion. Delayed polling releases the local
worker for slow tasks; bounded early confirmation trades a short local wait and
extra status reads for earlier completion of fast tasks. A new task has a 250 ms confirmation
window, with 50 ms between status reads and the existing three-second reserve
for SQL acknowledgement and durable fence cleanup. Fast tasks can finish in the
same delivery slice. Slow tasks or failed confirmation reads retain the known
UID and use the ordinary delayed polling path; subsequent pending polls keep
the 2/4/8/15-second backoff. HTTP reads never hold a SQL transaction.

Settings completion advances to the document phase without acknowledging the
source mutation or starting another write in the same admission. Unknown
submission outcomes remain fenced, and acknowledgement still matches the
submitted outbox versions. `test_search_delivery.py` and
`test_search_delivery_recovery.py` cover fast completion, pending tasks, read
failures, cancellation and recovery without duplicate submission.

### Stack

Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, PostgreSQL 16, Redis/RQ,
PostgreSQL search, and gallery-dl.

### Local iteration

Start data services, then run the API from a virtual environment:

```bash
docker compose up -d postgres redis

cd backend
python -m venv venv
source venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

The mapped Compose backend is `http://localhost:8818`; a locally started
Uvicorn process uses `http://localhost:8000`.

### Tests and lint

The container path is the most reproducible:

```bash
docker build --target test -t auto-gallery-backend:test ./backend
BACKEND_IMAGE=auto-gallery-backend:test docker compose run --rm --no-deps \
  --volume "$PWD:/workspace:ro" --workdir /workspace/backend \
  --env PYTHONPATH=/workspace/backend backend python -m pytest
BACKEND_IMAGE=auto-gallery-backend:test docker compose run --rm --no-deps \
  --volume "$PWD:/workspace:ro" --workdir /workspace/backend \
  --env PYTHONPATH=/workspace/backend backend ruff check app tests
```

For one test file, append its path after `pytest`. Local virtualenv runs use the
same commands without `docker compose run ... backend`.

### Dependencies

`requirements.in` / `requirements.txt` contain production dependencies. `requirements-dev.in` / `requirements-dev.txt` include the same production versions plus pytest, Ruff, typing/coverage tools and the pHash reference implementation. `requirements-build.in` / `requirements-build.txt` are only for compiling the pyvips native API extension and packaging Gitllery. NumPy is a direct production dependency; ImageHash/SciPy/PyWavelets remain development-only, and the retired search SDK is removed. Provider-sensitive packages such as gallery-dl stay pinned.

Install the development lock for local development. Normal Compose builds select `production`; tests use the `test` target in the same Dockerfile. `scripts/build-backend-candidate.sh` builds both targets instead of layering source onto old dependencies. Services and performance measurements use the production target.

```bash
cd backend
python -m pip install pip-tools
for scope in '' '-dev' '-build'; do
  pip-compile --allow-unsafe --generate-hashes \
    --no-emit-index-url --no-strip-extras \
    --output-file "requirements${scope}.txt" "requirements${scope}.in"
done
```

Run provider tests and the Docker Compose smoke path after dependency updates.
Dependabot groups minor and patch updates, but keeps each major update in a
separate pull request so migration and compatibility work remains reviewable.

### Database migrations

```bash
docker compose run --rm backend \
  alembic revision --autogenerate -m "description"
docker compose run --rm backend alembic upgrade head
docker compose run --rm backend alembic downgrade -1
```

Review autogenerated migrations. Column renames often appear as destructive
drop/add operations. Test upgrade and downgrade and document backfills,
reindexing, long locks, and data-loss risks in the pull request.

### Module boundaries

- Routes authenticate, validate, and delegate.
- Services contain business rules and orchestration.
- Repositories own database access.
- Providers validate and normalize URLs and parse source metadata; they do not
  access the database.
- Import and maintenance operations are idempotent.
- Background state transitions belong to the defined job state machine.

## Admin web

### Stack and structure

The frontend uses Next.js 16.3.6, React 19.2.8, TypeScript, Tailwind CSS, TanStack Query,
and Playwright. Major product areas live under `src/app/admin`; shared page
shells, navigation, feedback, object lists, overflow handling, and interaction
primitives live under `src/components`.

API access is split across `src/lib/api/client.ts`, `types.ts`,
`types.generated.ts`, and `index.ts`. Export the backend OpenAPI contract to `docs/api/openapi.json`, then regenerate
the frontend types from that file:

```bash
cd admin-web
npm run generate:api-types
```

### Local iteration and checks

```bash
cd admin-web
npm ci
npm run dev
```

Before a pull request:

```bash
npm run typecheck
npm run build
npm run test:e2e
```

Install the Playwright browser if your environment does not already provide it:

```bash
npx playwright install chromium
```

### Frontend expectations

- Add every user-facing string in both Chinese and English.
- Reuse navigation metadata instead of defining page hierarchy in each page.
- Verify narrow screens and long text without relying on fixed pixel widths.
- Preserve keyboard focus, touch targets, semantic names, and reduced motion.
- Treat loading, empty, error, partial, and permission-denied states as
  first-class UI.
- Update sanitized docs assets when a major workflow no longer resembles the
  published image.

## Repository checks

CI runs Node tests matching `tests/*.test.mjs`, `tests/*-contract.mjs` and
`tests/*.contract.mjs`, plus the explicitly listed Jobs and task-action tests.
`helpers.mjs` and `typescript-loader.mjs` support these tests; standalone browser
scripts (`danbooru-import-flow`, `test-client-nav`, `test-processing-nav`,
`test-result-restore`) are manual tools and are not Node test-runner inputs.
New behavior regressions belong in these groups or existing backend/Playwright
shards, not a separate frozen coverage list.

Test Cookie paths through configuration reads, saves and effective download
config construction, with non-sensitive fixtures for traversal and symlink
escapes. For uploads, test the route's real receive stream and multipart parser
as well as concurrent PostgreSQL saves. Supported bounds are 20 files, 500 MiB
per file, 1 GiB per batch and 2 MiB multipart overhead; final atomic charging
still decides acceptance after preflight checks. Inject read, cancellation,
move and commit failures and verify temporary files and usage are compensated.

For request scheduling regressions, use isolated Redis pause, refused and silent
connections and verify that unrelated requests and the event loop still advance.
Include the full workbench refresh path and inject the pause at its health
probe so preceding queue reads cannot consume the fault.
Use real bcrypt for login scheduling checks and verify the two-worker limit
separately. Subscription summaries must retain one membership batch for 1, 10
and 50 IDs, including foreign, duplicated and inactive memberships. Browser
checks cover mutations after 10, 50 and 100 list pages, selection restoration,
bidirectional dialog focus, field labels and asset failure/retry/empty states.
For selection restoration, evict a selected page, reload, finish loading the
retained pages and revisit the original row. Also cover confirmed deletion
after its owner page reloads and legacy sessions without ownership metadata.
Dialog checks must include opening a creator form, opening the command palette
above it and navigating to jobs. After navigation, verify original overflow and
inert states, working sidebar/topbar controls and actual wheel scrolling.
Command palette checks must follow focus through Tab/Shift+Tab and arrow keys
to the final Enter target. Include the clear button, composition events, real
search suggestions, query-only navigation, permission filtering and reopening.

Account regressions use formal HTTP login and real PostgreSQL transactions.
Cover old Bearer credentials after reset, both password-change entries,
disablement and username reuse, including WebSocket tickets and event delivery.
Force overlapping administrator removals and retain one active administrator.
Test upload/clear coordination with the real POSIX lock and auxiliary Redis
lease, including copying, pending imports, imported works, permission failures
and retries. Failed clear must retain its billing/ownership facts and must not
publish cancellation before a successful commit. These cases belong to the
existing account, upload, operation and worker CI shards.

`scripts/verify-runtime.sh` issues current ID/version credentials from an active
administrator, preferring one who has completed password change. On first
deployment with only a forced-change administrator, it verifies that docs and
business access return 403 and reports the authenticated docs check as pending.
Complete password change through login and rerun the script. No active admin is
a failure; verification does not modify account security state.

Thumbnail GET and conditional GET require session/Bearer authentication or an
asset-and-purpose-scoped ticket before file access or derivative requests.
Same-origin browser images send the session cookie; retain private cache and
ETag behavior, and verify revoked/anonymous requests separately.

Run rollback tests with command substitutes, never against a live deployment.
Stop command failure, timeout or active writer status must leave switch actions
and foreground restart blocked. Inspect `journal.json` or
`rollback-receipt.env`, resolve the stop failure, then rerun the original rollback
command. Host-test substitutes must use the same Python version as the tests;
put the test virtualenv's `bin` directory first in PATH.

```bash
docker compose --env-file .env.ci config --quiet
bash scripts/privacy-scan.sh
bash scripts/package-release.sh ci
```

The CI workflow adds the full backend, frontend, repository, and Compose smoke
checks. See [.github/workflows/ci.yml](../.github/workflows/ci.yml).
