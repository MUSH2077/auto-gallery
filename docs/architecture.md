# Architecture

## Overview

auto-gallery is a layered Docker Compose application that downloads media from multiple sources, imports metadata into a canonical PostgreSQL database, and serves it through a FastAPI backend and Next.js admin interface.

## Layer Diagram

```
┌─────────────────────────────────────────────────┐
│  Admin Web (Next.js 16)                          │
│  TypeScript · Tailwind CSS · TanStack Query      │
│  Port 13000 (host) ← 3000 (container)            │
└──────────────────┬──────────────────────────────┘
                   │ HTTP REST
┌──────────────────▼──────────────────────────────┐
│  Backend (FastAPI)                               │
│  Routes → Services → Repositories → SQLAlchemy   │
│  Providers: Pixiv · X · Iwara · Danbooru ·      │
│   Weibo · Bilibili · Pinterest · LOFTER          │
│  Port 8818 (host) ← 8000 (container)             │
│  Redis browser sessions + Bearer JWT             │
└──────┬────────────┬──────────────┬──────────────┘
       │            │              │
       │   ┌────────▼────────────┐ │
       │   │  Workers (RQ)       │ │
       │   │  worker-download    │ │  gallery-dl subprocess
       │   │  worker-import      │ │  metadata → DB pipeline
       │   │  worker-operations  │ │  backup / reindex / disk-import
       │   │  scheduler          │ │  subscription sync loop
       │   └────────┬────────────┘ │
       │            │              │
┌──────▼────────────▼──────────────▼──────────────┐
│  Data Layer                                      │
│  PostgreSQL ── Redis ── Meilisearch              │
│  ┌──────────┐ ┌──────┐ ┌─────────────┐          │
│  │ /downloads│ │/lib │ │ /gallerydl  │          │
│  │           │ │rary │ │ -config     │          │
│  └──────────┘ └──────┘ └─────────────┘          │
└─────────────────────────────────────────────────┘
```

## Maintained Module Boundaries

- `SearchService` remains the backend search interface.
  `search_language.py` parses queries; `search_pagination.py` owns stable
  cursors and sort rules; `search_filters.py` compiles Meilisearch filters;
  `search_projection_fields.py` builds deterministic index fields.
  `search_index_definitions.py` owns index identities/settings and
  `search_projection.py` owns document hydration and the shared projection
  registry consumed by incremental delivery and rebuild. `SearchService`
  retains compatibility methods while query execution uses the existing SQL
  and Meilisearch adapters. `subscription_schedule.py` owns pure calendar and
  interval policy; scheduler search and the dashboard use `scheduler_decisions.py`
  for the same source eligibility and credential-blocking interpretation.
  Deployments and search CI pin Meilisearch 1.54.3, which includes upstream
  allocation and document-operation cleanup fixes. Application projections,
  durable delivery receipts and rebuilds define index consistency. See
  [search-engine upgrades](deployment-profiles.md#meilisearch-upgrades--搜索服务升级)
  for migrating existing indexes and restoring the previous version.
- `search_sql_filters.py` isolates SQL work-projection eligibility and qualifier
  compilation from the SearchService execution path.
- `search_work_page.py` owns the reusable SQLAlchemy statement for page asset
  hydration. Each request binds its own work IDs; assets shared by several
  source links are counted once, and ordered preview IDs remain bounded to ten
  per work. Single-index text searches reuse one Meilisearch client for that
  execution.
- `api/media.py` resolves the requested asset, its representative and the final
  storage state with one query. Authentication, signed URLs, ETag/304 responses,
  quarantined-file handling and purged-file rejection retain their existing
  behavior.
- `import_runner.py` processes work slices, while `import_execution.py`
  owns the claim, lease and completion workflow. `import_lifecycle.py` locks
  and rereads parent, child and TaskRun before token arbitration, control
  commands and parent aggregation. Artifact locks, when required, come first.
  `import_intent.py` owns artifact assignment and durable publication intent:
  prepare never commits; publication follows the caller's commit. Download,
  upload, disk import and recovery share this implementation; old worker
  function names remain forwarding adapters for persisted RQ records.
- `danbooru_import.persist_danbooru_artist` owns resolved artist persistence for
  single, Pixiv-ID batch and URL batch imports. Sources are unique by URL;
  resolvable identities, aliases and search intents are applied in the caller's
  transaction. Entrypoints fence the attempt before commit and invalidate caches
  afterward; batch confidence and per-item result policies stay at the edges.
- `download_lifecycle.py` owns download state application and explicit TaskRun,
  receipt and search projections. Control, worker failure/retry, stale and invalid
  dispatch paths reuse it after their existing eligibility checks and locks.
  Workers fence writes with the RQ ID and TaskRun attempt captured at claim;
  repositories no longer create or update TaskRun implicitly. Callers commit
  before publishing durable status notifications. `download_finalization.py`
  shares parent outcome mapping across import completion, publication close
  and recovery, using the existing bounded-child aggregation.
  Shared subscription and membership search documents exclude private job and
  TaskRun state. Nonterminal private transitions therefore update the job and
  TaskRun without registering an unchanged shared search projection. Public
  transitions and every terminal transition retain search registration and the
  existing receipt policy; source, work and asset mutations keep their own
  projection registration.
  Cancellation application (`TaskEngine.apply_cancel_download/import`) does not
  commit or notify, including download-to-import delegation. Standalone control
  endpoints commit and then notify; scheduler cleanup stages both cancellation
  and its batch item, fences the cleanup attempt, commits once and then notifies.
- `operations.py` coordinates admin task publication and compensation;
  `admin_dispatch_recovery.py` selects bounded due PostgreSQL dispatch
  intents for recovery without trusting Redis queue state as the authority.
- Each admin operation definition links its handler, normalized result policy
  and resource admission profile. `admin_operation_handlers.py` implements
  business execution, while the RQ worker owns attempt fencing and heartbeat.
  `backup.py`, `integrity.py` and `connectivity.py` under services provide the
  capabilities independently of HTTP routers. File publication still requires
  the durable attempt fence before exposing a backup or restore candidate.
- HTTP and CLI favorite/tag changes use the same curation command transaction
  for domain rows, audit history and search outbox. Tag input uses the existing
  CLI casefold rule; historical tag rows are not rewritten. Purge takes the
  curation ordering lock before row locks and rereads eligibility before file
  deletion, so a committed restore cannot be purged from a stale ORM snapshot.
  HTTP and CLI trash/restore share eligibility, mutation and audit construction
  after the ordering lock and state reread. Entry policies preserve CLI noop
  auditing and HTTP duplicate/missing-ID handling; purge keeps its own file flow.
- `gitllery/payload.py` shares segment commit format mapping across outbox and
  direct projection/rebuild. Global and repository parents remain distinct;
  callers still read the repository parent under the projection lock.
- `resource_pressure.py` coordinates sampling, state and shared snapshots
  delegated to `resource_pressure_sampling.py`,
  `resource_pressure_state.py` and `resource_pressure_snapshot.py`.
- `resource_descriptors.py` describes child-owned admission and bounded outbox
  slices. New enqueue producers attach an explicit descriptor; legacy RQ
  names are interpreted only by the compatibility adapter. Registered admin
  profiles never bind task-ID/attempt arguments. Parent admission, child
  leases, pressure thresholds and worker exit behavior remain separate.
- `api/client.ts` normalizes response errors for both fetch and XHR uploads.
  Upload progress stays with XHR; CSRF and request-session checks protect 401
  handling, and network/timeout failures retain a transport-error category.
- `i18n.tsx` is the sole translation Context and Provider, for alias and relative
  imports alike. Build scripts read the data-only `i18n-catalogs.ts` source;
  the runtime loads generated locale JSON separately and fences delayed loads.
- The Jobs page keeps URL state in `jobsRoute.ts` and
  `useJobsRouteState.ts`, and pure task-tree grouping in `jobTree.ts`.
  Rows, drawers and batches use `taskCommands.ts` through `useTaskCommand.ts`
  for action dispatch, repeat-sync intent handling and cache invalidation.
  Generated OpenAPI task types are
  refined only for dictionary fields used by views. `operationMonitor.ts`
  owns ordinary admin-operation recovery and polling, `operationRefresh.ts`
  owns resource invalidation, and NotificationCenter consumes the resulting
  state for display. When a page and the global monitor show the same operation,
  both observe `admin-operation-task` keyed by TaskRun identity; the page disables
  its polling and completion callback while the global monitor owns them.
  Untracked page operations retain their page owner. Missing operations stop on
  404/410. Retry acceptance reactivates this monitor using known task context
  and the existing minimal response, fences late queries and resets completion
  handling for the new attempt without changing the TaskRun identity.
  `batchImportMonitor.ts` adapts batch responses to the same monitor and owns
  recovery; the Danbooru page only consumes normalized state. Legacy records
  retain their storage key and two-hour recovery window. A missing final result
  is confirmed by bounded query retries and then ends with an error.
  Creators and subscriptions share `useReferenceListNavigation.ts` hooks for
  navigation, sessions, loaded-row selection and legacy pagination recovery;
  each page retains its filters, data requests and business commands.

## Request Scheduling and Interactive Lists

HTTP queue statistics, creator-count caching, workbench Redis health, proxy status and rebuild status
run synchronous Redis operations in threads through `redis_budget.py`.
HTTP operations use a 0.4-second Redis command deadline and a 0.6-second caller
wait limit; unavailable optional reads return their existing fallback shape.
Workbench executable discovery also runs in a thread so slow PATH lookups do
not block the event loop.
Background dequeue timeouts remain unchanged. Password hashing and verification
use at most two dedicated threads per API process, retaining bcrypt cost,
rate limits, account locks and credential-version updates.

Reference lists keep at most 32 observed pages and enable queries only for the
current virtual window and nearby prefetch/restoration pages. Evicted page
queries are garbage-collected; their disappearance does not erase selections.
Only confirmed page data can remove a selected ID belonging to that page.
Sessions retain known selected-ID page offsets independently of the cached
page window; selections from legacy sessions without an owner remain pending.
Subscription summaries use one membership batch for visibility and private
state overlays. Dialogs and the command palette share focus containment and
background isolation; asset fetch failures expose retry and retain existing
media instead of presenting an empty result.
Scroll locks and inert background elements retain their original state until
the last dialog owner releases them, regardless of cleanup order on navigation.

## Domain Model

All models use source-agnostic naming. Shared data vs user-scoped data:

```
(shared across users)
creator ──< source_creator
creator ──< creator_link
creator ──< subscription ──< subscription_source
work ──< work_source
work ──< work_tag
work_source ──< work_source_tag
asset ──< asset_source
tag

(user-scoped intent)
user ──< user_subscription >── subscription
user_subscription ──< user_subscription_source >── subscription_source
user ──< remote_account ──< discovery_candidate
user ──< album ──< album_work ── work

(operational / cross-cutting)
task_run ──< task_event        # unified task envelope — see "Unified Task System"
storage_artifact               # download → import ledger — see "Job Queue"
```

### Key relationships

- **user**: An active admin or regular account with module permissions.
- **creator**: A canonical local identity (e.g., "Artist A") — shared across all users
- **source_creator**: A platform-specific account (e.g., Pixiv user 123456)
- **creator_link**: URLs linking a creator to external profiles
- **work**: A canonical local work (may have multiple work_sources)
- **work_source**: A source-specific work record with original metadata
- **asset**: A local file
- **asset_source**: A source-specific file record
- **subscription**: The canonical, globally shared repository for one creator
- **subscription_source**: A globally shared source identity whose enabled/due
  fields are aggregate compatibility caches
- **user_subscription**: One user's private name, enabled state, and schedule
  for a canonical subscription
- **user_subscription_source**: One user's private source enablement, due time,
  authentication health, and optional remote-account binding
- **remote_account** / **discovery_candidate**: Account-private discovery
  configuration and scan results
- **album**: A user-created collection of works (Phase 6+)
- **album_work**: Many-to-many join between album and work

### Uniqueness constraints

- `(source, source_creator_id)` on source_creators
- `(source, source_work_id)` on work_sources
- `(source, source_asset_id)` on asset_sources
- `(normalized_name)` on tags
- `(creator_id)` on subscriptions
- `(subscription_id, source_url)` on subscription_sources
- `(user_id, subscription_id)` on user_subscriptions
- `(user_subscription_id, subscription_source_id)` on user_subscription_sources
- `(user_id, source)` on remote_accounts
- `(remote_account_id, source_creator_id)` on discovery_candidates
- `(username)` on users
- `(album_id, work_id)` on album_works

## Provider System

Source-specific behavior is encapsulated in provider modules under `backend/app/providers/`.

Every provider implements:
- URL normalization and validation
- gallery-dl config generation (if downloadable)
- Metadata parsing (source_creator, work_source, assets, tags)

Providers never access the database. They receive raw metadata dicts and return data dicts. The service layer handles persistence.

## Job Queue (RQ)

auto-gallery uses RQ for downloads, imports and discovery, with an operations queue for administration. PostgreSQL job, lease and artifact records own recovery; Redis carries execution and live notifications. Key design choices:

- **Why RQ**: Simpler than Celery, uses Redis already in the stack. `download_job`/`import_job` database tables are the source of truth; the queue backend is replaceable.
- **Per-source download queues**: Each source has its own RQ queue (`downloads:pixiv`, `downloads:danbooru`, etc.) for isolation — one slow source never blocks another. The `worker-download` container listens on all source queues.
- **Remote discovery queue**: `worker-discovery` exclusively supervises the independent
  `discovery` child queue. The scheduler only admits due remote accounts;
  provider pagination and cursor checkpoints run in the child worker. Jobs
  carry opaque task/account IDs and never credential material.
- **Durable RQ imports**: Download artifacts are recorded in PostgreSQL. Bounded RQ import consumers claim attempts with execution tokens and claim work identities with artifact leases, enabling restart-safe recovery without duplicate ownership. Consumers share bounded ingest slices rather than holding one lock across the whole job.
- **Job timeout**: `job_timeout=7200` (2 hours) on all enqueue calls to prevent RQ from killing long-running downloads (default is 180s).
- **State machine** (canonical source: `backend/app/models/task_state.py`): `enqueued → downloading → downloaded → importing → complete`, with `paused`, `cancelled`, `failed`, and `stale` reachable from the non-terminal states. Old status strings (`pending` → `enqueued`) are accepted via a backward-compat map. A `downloaded → complete` short-circuit exists for jobs whose download produced no new metadata (all content already in the archive), so they never get stuck waiting on an import. Paused jobs skip execution; `cancelled` is terminal but keeps the DB record for audit. Stale detection uses Redis heartbeat keys (`task:{job_id}:heartbeat_ts` with 90s TTL) — if the key is absent and the job is past its retry grace period, it's marked stale. Partial import recovery on timeout/failure. `task_state.py` defines legal transitions; `TaskEngine` coordinates controls and import commands use locked arbitration and aggregation in `import_lifecycle.py`.
- **Adaptive timeouts**: Retry count increments the stall and deadline timeouts (1.0x → 1.5x → 2.0x) to give later attempts more headroom.
- **Proxy pre-flight**: Before launching gallery-dl, the worker runs DNS resolution + proxy connectivity checks (non-blocking diagnostics).

## Unified Task System (`task_runs`)

Downloads, imports, and admin operations (backup, reindex, disk import) are all surfaced through one user-facing task envelope so the admin **Tasks** page (`/admin/jobs`) has a single queue/progress/history view.

- **`task_run`** (`models/task_run.py`): one row per trackable unit of work. `kind` (`download` / `import` / `admin`) and `subject_type` + `subject_id` loosely reference the authoritative domain row (`download_job`, `import_job`, …) — there is no FK, so a task can outlive or precede its domain record. Carries display fields (`title`, `source`, `source_url`), a progress snapshot (`progress_current`/`progress_total`/`progress_data`), `priority`, `attempts`, and `last_heartbeat_at` for stale detection. `parent_task_id` groups batch/child runs.
- **`task_event`**: append-only audit trail of status transitions for a run; cascade-deleted with its parent (`ON DELETE CASCADE`).
- **`TaskService`** (`services/tasks.py`): the write path — creates runs, records events, normalizes statuses, and merges download/import domain rows into the unified list.
- **`TaskEngine`** (`services/task_engine.py`): the control-request entrypoint (pause / resume / cancel / retry / priority / batch-by-filter), delegating import commands to the lifecycle module, publishing changes over Redis pub/sub for live WebSocket updates.
- **Authoritative payloads stay in domain tables**: `task_run` is the envelope; `download_jobs` / `import_jobs` remain the source of truth for their own fields. Clearing tasks (Data Management) deletes `task_runs` **and** the domain job tables together.

## Data Flow

Private intent is stored in `user_subscriptions`,
`user_subscription_sources`, `remote_accounts`, and `discovery_candidates`.
Canonical `subscription`/`subscription_source`, creators, works, assets, and
files remain globally shared and deduplicated. A canonical source's aggregate
enabled/due fields are compatibility caches derived from its private members.

### Download Flow
```
Scheduler (timezone-aware, configurable interval or daily schedule)
  → Check each subscription_source: is sync due?
    → create download_job (pending)
      → Enqueue to per-source queue (downloads:{source})
      → Worker picks up job (downloading)
        → Proxy/DNS pre-flight checks
        → Provider.build_gallerydl_config()
        → subprocess.Popen(["gallery-dl", ...], shell=False)
            with stall detection polling loop
        → Files land in DOWNLOAD_ROOT/{source}/{creator_name}/{source_work_id}/
        → Job status = downloaded
        → PostgreSQL storage_artifacts → RQ import job
```

Key download details:
- `--write-metadata`: Required flag — without it, import runner has no JSON metadata to parse.
- `--range`: Limits download to the most recent N posts for incremental syncs. Configurable per subscription.
- **Scheduler**: Timezone-aware via the `TIMEZONE` env var (defaults to UTC). Supports interval mode (sync every N hours) or scheduled mode (sync at specific times daily).
- **Stall detection**: Polling loop checks progress every 4s; kills gallery-dl if no file output for `stall_timeout` seconds (default 120s, after 30s grace period). Adaptive timeout on retries.
- **Three-layer timeout**: (1) gallery-dl internal `--retries`/`--abort`, (2) stall detection kills frozen subprocess, (3) overall deadline timeout.

### Import Flow

Download execution records files in the PostgreSQL `storage_artifacts` ledger.
`import_intent.py` creates and publishes durable RQ import intent; `worker-import`
claims the current attempt and processes bounded work groups under a lease.
Provider metadata is mapped to creators, work sources, assets and tags, and
ledger states record progress for recovery. Media derivative requests are
persisted for background processing; import completion does not synchronously
generate every thumbnail. Files remain in DOWNLOAD_ROOT, while metadata and
derivatives live in LIBRARY_ROOT. JSON cleanup follows successful import.

Creator refresh collects subscriptions and repositories with one LEFT JOIN and combines identity and normalized-URL work dependencies with UNION; subscriptions without repositories remain in scope. Membership fanout combines subscription, creator and repository conditions, using UUID array parameters and 500-row keyset pages so overlapping members discovered through those scopes are enqueued once per action. Independent requests still advance outbox versions, deletes retain precedence, and dependencies captured before creator deletion still refresh surviving works and delete reference documents.

Bulk import writes media, import-curation and search outbox rows in the same
transaction as the imported records. It registers one wake intent per outbox
kind; only the outer transaction's successful commit publishes those wakes.
Rollback, including savepoint rollback, does not publish the rolled-back
intent. Notification failure leaves the committed outbox rows available for
recovery. Import completion does not imply thumbnail or search readiness; each
background result must be observed separately.

Danbooru reference batches are separate administrative operations. Their
single, Pixiv-ID and URL entries use `persist_danbooru_artist`, and validate the
current operation attempt before committing each entry. They do not consume a
Redis Stream or bypass the media-import lease.

### Danbooru Reference Flow
```
Admin triggers Danbooru artist import
  → Fetch Danbooru artist page
  → Parse artist tags, related URLs
  → Match resolvable identities, artist IDs and active source links
  → Reject conflicting ownership or apply shared persistence rules
  → Validate the current operation attempt, commit, then invalidate caches
```

## Reliability & Recovery

Because gallery-dl writes directly to disk and the download → import handoff spans two queues, several recovery paths keep the database in sync with what is actually on disk. All are idempotent and never delete or re-download.

- **Import from disk** (`services/disk_import.py`, `POST /api/v1/admin/library/import-from-disk`): scans `DOWNLOAD_ROOT/{source}/` and imports files that exist on disk but were never recorded (ledger state ≠ `done`), under a synthetic `recovery` download_job. `services/disk_identity.py` first provisions a real `creator → subscription → subscription_source → source_creator` chain from local metadata (Danbooru enrichment is best-effort, with a metadata-only fallback) so recovered works attach to the right creator. Runs on the `operations` queue; a Redis lock (`library:disk-import:active`) prevents concurrent runs.
- **Import pipeline recovery** (`services/import_recovery.py`): conservative auto-repair of safe gaps — e.g. re-enqueues an import for `downloaded` jobs that already have pending metadata. Read-only health is reported by `services/pipeline_health.py`.
- **Creator auto-relink** (`services/creator_reconcile.py`): each subscription-sync scan links orphaned `source_creators` (`creator_id IS NULL`) to their subscription's creator by identity match. It mirrors `import_runner`'s auto-link rules, so an ambiguous bookmark/repost feed is never mislinked.
- **Worker startup orphan reconciliation**: on startup, jobs stuck in `downloading`/`importing` are marked `stale`, and on-disk directories with no matching job are picked up for import.

A completed disk-import administrator task means scanning and child publication have finished; its publication count is not a work count. Actual results require checking all child imports and their download parents. Global and source-scoped replays share the same classification: unchanged existing works are successful idempotent results, so zero new works does not mean total failure. Counts distinguish new or updated works, unchanged existing works and work groups skipped due to processing errors. Terminal progress counts all three as processed groups. A below-threshold skip rate completes with a skip notice; all-failed runs and rates at or above the configured threshold still fail. Both classified terminal outcomes retain statistics and total groups, while artifact errors remain available. Bounded parent results retain the child statuses and aggregate counts.

Platform downloads and disk imports share image extensions, including AVIF artifacts classified as `image` with asset MIME `image/avif`. Originals are retained and previews use WebP. Static AVIF uses SHA-256 and versioned pHash fingerprints. AVIF sequences preview the first frame and use whole-file SHA-256 for exact deduplication, without a first-frame pHash. Thumbnails prefer libvips, with a Pillow first-frame fallback when libvips cannot open AVIF. Application Pillow AVIF decoding uses one thread and preserves media admission and atomic thumbnail writes. Disk import can register previously omitted files; existing works use the append-asset/idempotent update path.

Visual fingerprints use a pure-NumPy real-FFT implementation of the DCT-II on a 32×32 Pillow LANCZOS grayscale sample. There is no SciPy compatibility fallback, and bit-for-bit equality with earlier pHash values is not promised; the version boundary is explicit so production records can be recomputed. Extremely elongated, single-frame, non-interlaced 8-bit RGB/RGBA PNGs that cannot use the proportional work image retain sequential grayscale sampling to bound memory. Other PNG layouts and image formats retain their original path, including Pillow decompression-bomb checks. Without a shared work image, thumbnails wider than 400 pixels use the libvips file entry point so JPEG decoding can shrink on load; dimensions, orientation, alpha and atomic writes retain their existing policy. SSIM applies EXIF orientation in place and reuses already-RGB pixels while preserving the 256×256 resize, score formula and decision thresholds.

### Search and Asset Deduplication Completion

After delivery receives a remote taskUid, it persists the task identity in the
remote resource marker and commits a pending SQL receipt before confirmation.
It then gives the task at most 250 ms to finish, with 50 ms between status
reads, reserving the existing three seconds for SQL acknowledgement and durable
marker cleanup. HTTP confirmation holds no SQL transaction. A completed settings
task advances to the document phase without acknowledging the source mutation
or submitting another write under the same admission.

If this short window ends, its status read fails, or the completion reserve
leaves no confirmation budget, the known task remains pending and the first
successor is scheduled after one second. Subsequent unfinished-task polls back
off through 2, 4, 8 and 15 seconds; network errors in that ordinary polling path
still wait 15 seconds. Durable receipts, version-matched acknowledgement,
remote resource markers and ambiguous-write recovery retain their existing
rules. Remote completion, local acknowledgement and the next submission remain
separate milestones.

Delayed confirmation releases the local worker and limits polling for slow
remote tasks. The short window trades a bounded local wait and extra status
reads for lower latency when the remote task finishes quickly. This is a
performance optimization of that scheduling policy.

Asset candidates must pass the SHA/visual gate before loading creator and publication metadata; visually rejected candidates skip those scoring queries. Accepted pairs load both assets in one source-relationship query, partition rows by asset ID, and retain the existing scoring. Candidate source filtering and the locked scope recheck remain independent. Representative selection, work/source relationships and original-file checks before hardlinking or quarantine retain their existing rules. A future quarantine-retention purge is separate from the current batch's immediate completion.

The dedup outbox loads dependencies on the paths that use them: scan administration in scan entry points, curation while applying a group, and case media links and commit response schemas in their presentation paths. RQ function identities, curation audit and response structures retain their existing definitions. After models register, the actual RQ parent configures ORM mappings before its first workhorse fork. Children establish independent database connections; startup preparation and resident-memory costs are measured separately.

`worker_entrypoint.py` launches RQ consumers in independent interpreters. The actual consumer registers the TaskRun resource-state bridge in `ResourceAwareWorker` before workhorse forks. The supervisor does not repeat that registration or load business services and ORM solely for it. Before download consumers start, one short-lived interpreter validates the credential tmpfs, removes abandoned authentication configurations, reads database concurrency and resolves deployment/device limits. It returns only startup values, then exits so configuration and ORM dependencies do not stay in the supervisor. Credential-storage validation failure stops startup. A failed database read or a five-second query timeout retains the CLI/default fallback; the supervisor reaps the helper on its fifteen-second deadline or an interrupt. Scheduled-supervisor control-plane work and consumer mapper preparation, admission and durable state publication retain their existing behavior.

After configuring mappings, the real RQ parent collects temporary initialization garbage and freezes the initialized object graph before scheduler and workhorse forks. Cyclic GC then skips those long-lived objects, preserving shared pages; automatic collection remains enabled for subsequently created task objects. Freezing changes GC tracking, not object mutability or reference-count deallocation. Task payloads and results are created after this startup step. Mapper preparation still creates no database connection, and workhorses establish their own connections.

Subscription scanning, Pixiv ranking, heat expiry and heat recomputation publishers enqueue stable RQ function paths; task execution resolves the handlers. Job identities, arguments, retries and dependency handling retain their existing behavior.

Resource-aware workers use `ResourceAwareScheduler` for due-task promotion. It delegates promotion and locking to RQ, then sends a best-effort hint through the existing queue work channel for each due queue. Worker timeout checks remain the recovery path when a hint is lost, and queue records remain authoritative. Remote-task confirmation/backoff, resource admission and completion recovery retain their existing rules.

### Background Outbox Recovery

An empty queue and a resource-denied task are different outcomes. Media and
asset-dedup runners report resource deferral so the coordinator can record a
waiting token without marking the durable work complete. PostgreSQL outbox
rows remain authoritative; Redis wake owners and waiting tokens are recovery
hints.

After a worker releases its resources, its parent checks deferred outboxes.
The scheduled supervisor also consumes `resource:control` notifications while
workers are idle. Its startup and reconnection checks cover waiting work that
predates the current connection. Media selection and recovery use the same
memory requirements and resource checks as execution; recovery never bypasses
memory protection, locks, delayed availability or an active remote-search
reservation.

Recovery inspection and publication have a bounded time budget. A timeout
retains waiting work, and a failed notification does not turn an already
completed domain job into a failure. The existing approximately 60-second
fallback scan remains available when notifications or Redis waiting hints are
lost. These mechanisms reduce unnecessary waiting after resources recover;
they do not guarantee execution while resources remain unavailable. See the
[operational recovery guide](RUNBOOK.md#background-outbox-recovery).

## Gitllery — On-Disk Curation History

PostgreSQL curation commits and changes remain authoritative. Curation writes
durable projection intent in the same transaction; `gitllery_outbox.py` plans
repository targets and projects them asynchronously in bounded slices. Disk
failure leaves retryable intent and does not undo the committed domain change.
Outbox leases, attempt checks and projection locks protect replay and ordering.

The active Gitllery v1 format stores a `manifest.json`, `config.json` and
compressed, content-addressed `segments/` under each repository's `.gitllery`.
Publishing writes segments before atomically advancing the manifest. Global
commit parents and repository parents have distinct meanings; payload mapping
is shared by incremental projection and builds. Older object/index formats
remain compatibility readers rather than the current write protocol.

`services/gitllery/builds.py` manages rebuild, verification and publication;
`services/gitllery/rebuild.py` reads history for reconstruction. Maintenance
commands under `/api/v1/curation/gitllery/` use existing administrative
operations and resource admission. The admin Gitllery panel displays status
and initiates these commands; API requests do not synchronously write every
repository or treat disk as a replacement for the live database.

## Authentication

Admin Web uses a revocable random browser session stored in Redis. The session
cookie is host-only, HttpOnly, SameSite=Lax, and scoped to /; HTTPS browser
origins receive Secure cookies. JavaScript reads only a session-bound CSRF
value. Browser writes require that value and an exact allowed Origin. One-use
WebSocket tickets are bound to the browser session and the handshake Origin is
checked. Logout revokes the session, password changes rotate it, and Redis
failure returns a service-unavailable response instead of admitting a stale
session.

Script/API clients continue to use JWT Bearer tokens from
`POST /api/v1/auth/login`. An explicit Bearer header takes precedence over a
cookie and an invalid Bearer token never falls back to a browser session.
Disabled users, forced password change, and module permissions apply to both.
`ACCESS_TOKEN_EXPIRE_MINUTES` governs both login durations: the application
default is 10080 minutes, while the Compose default is 1440 minutes. Browser
users sign in again when migrating from legacy `ag_token` storage.

Bearer credentials carry the immutable user ID (`uid`) and credential version
(`cv`); the username is an actor label, never the authorization lookup key.
Password reset and both password-change endpoints increment the version in the
same transaction as the password hash. Authentication checks the current active
and forced-password-change state. Deploy the additive `0159bc20de46` migration
before the application; previous username-only Bearer tokens require a new
login. WebSocket tickets and event forwarding retain the account ID/version,
so an existing connection cannot inherit a replacement account's access.
Admin demotion, disabling and deletion serialize the last-active-admin check
and mutation with one transaction-scoped advisory lock.

LAN HTTP remains supported for local access. Its cookies are exposed to anyone
who can observe that connection; use the HTTPS reverse-proxy entry for
transport protection.

## API process runtime

The backend starts through `backend/api_entrypoint.py`, which launches the
native Uvicorn worker supervisor. `API_WORKER_CONCURRENCY` defaults to one and
accepts values from one through four. The launcher divides the total API
budgets for one backend container before importing the application: the database budget is 8 steady plus
4 overflow connections in total, divided per process by floor (`1 = 8 + 4`,
`2 = 4 + 2`, `4 = 2 + 1`), and the total Meilisearch search concurrency is 4,
divided per process with a minimum of one. Direct multi-worker Uvicorn startup
bypasses this allocation and is not a supported deployment path.

`API_THREAD_POOL_SIZE` defaults to a total of 16 blocking-thread slots and is divided by floor (1 = 16, 2 = 8, 4 = 4). The cap is applied separately to the asyncio default executor and the AnyIO thread limiter; these are independent pools, so 16 is not their combined thread limit. API1 still applies its 16-slot cap while bypassing the Redis lease.

Each API process can run the foreground request and WebSocket paths, while one
Redis lease owner runs the singleton maintenance group. Recovery, resource
state and health are published through shared snapshots with owner fencing;
other processes read the shared health state. Foreground latency samples are
aggregated at a low frequency, and ordinary request handling performs no Redis
I/O for this aggregation.

## Backup System

The system includes automated backup capabilities:

- **Trigger**: POST `/api/v1/admin/backup` creates a full backup (pg_dump + config tarball + download archive listing).
- **Format**: Compressed tar.gz archive stored under `DOWNLOAD_ROOT/.backups/`.
- **Download**: GET `/api/v1/admin/backup/download` serves the backup file.
- **Listing**: GET `/api/v1/admin/backup` lists available backups.
- **Restore**: POST `/api/v1/admin/backup/restore` restores from a backup archive.
- **Scheduling**: POST `/api/v1/admin/backup/schedule` enables recurring auto-backup via RQ scheduler at configurable intervals. Old backups are pruned (last 5 kept).

## Integrity Check

An integrity verification system scans for data consistency issues:

- **Endpoint**: GET `/api/v1/admin/integrity-check`
- **Checks performed**:
  - Orphaned files in `downloads/` with no corresponding DB records
  - Missing thumbnails for assets that should have them
  - Orphaned DB records pointing to non-existent files
  - Work source / asset source consistency

## Container Paths

Application code uses only these env-var-driven paths:

| Variable | Container path | Purpose |
|---|---|---|
| `DOWNLOAD_ROOT` | `/downloads` | Original Media Store: long-term original files, organized by source/creator/work |
| `LIBRARY_ROOT` | `/library` | Library Index: per-work metadata + thumbnails |
| `GALLERYDL_CONFIG_ROOT` | `/gallerydl-config` | Durable gallery-dl configs/cookies and non-secret per-job configs |
| `APP_CONFIG_ROOT` | `/app-config` | Application runtime config |
| `PERSONAL_AUTH_TMP_ROOT` | `/run/auto-gallery-secrets` | `worker-download`-only tmpfs for mode-0600 personal auth overlays; never backed up |

NAS host paths are mapped in `docker-compose.yaml` only.

## NAS Storage Structure

```
/volume1/auto-gallery/
├── downloads/                          # DOWNLOAD_ROOT — Original Media Store
│   └── {source}/                       # e.g. pixiv, iwara, x
│       └── {creator_name}/             # e.g. ASK, 1980643
│           └── {source_work_id}/        # e.g. 38362603 (Pixiv artwork ID)
│               ├── 38362603_p0.jpg     # Page 0
│               └── 38362603_p1.jpg     # Page 1 (if multi-page)
│
├── library/                            # LIBRARY_ROOT — Library Index
│   └── {source}/                       # e.g. pixiv, iwara, x
│       └── {creator_name}/             # e.g. ASK, 1980643
│           └── {source_work_id}/        # Same ID as downloads — links the two
│               ├── metadata.json       # Work metadata export
│               └── thumbnail.webp      # 400px WebP thumbnail (pyvips)
│
├── config/
│   ├── gallery-dl/                     # GALLERYDL_CONFIG_ROOT
│   │   ├── config.json                 # gallery-dl base config
│   │   ├── cookies/                    # Auth cookies per source
│   │   └── jobs/                       # Non-secret per-job configs; excluded from backup
│   └── app/                            # APP_CONFIG_ROOT — runtime configs
│
├── docker/                             # Persistent volumes
│   ├── postgres/                       # PostgreSQL data
│   ├── redis/                          # Redis data
│   └── meilisearch/                    # Meilisearch data
│
└── backup/                             # Backups
    ├── db/                             # PostgreSQL dumps
    ├── config/                         # Config backups
    └── metadata/                       # metadata.json copies
```

Personal account authentication overlays are not part of the NAS tree. The
download worker writes them under `/run/auto-gallery-secrets`, a dedicated
nonpersistent tmpfs, and validates that boundary before use.

### Storage rules

- **downloads/**: Original Media Store. Original image/video files are organized by `{source}/{creator_name}/{source_work_id}/` and kept long-term. Files are moved here from the flat gallery-dl output during import. No JSON metadata files kept — they are deleted after processing. No job_id layer in the path.
- **library/**: Library Index. Per-work metadata and derived images only. Same `{source}/{creator_name}/{source_work_id}/` structure as downloads, linked by source_work_id. No original media.
- **gallery-dl output**: Short-lived worker output before import. During import, files are reorganized into the long-term Original Media Store and JSON files are deleted.
- **Thumbnail and poster**: Images receive a 400px WebP thumbnail through pyvips. MP4/WebM assets are inspected with ffprobe and receive a 400px WebP thumbnail plus a poster no larger than 1280px. Derived files live under LIBRARY_ROOT; originals are never modified.
- **Preview/Original**: Served directly from DOWNLOAD_ROOT via `/media/preview/{asset_id}` and `/media/original/{asset_id}`. Preview requires a short-lived signed URL; original accepts either admin Bearer auth or a short-lived signed URL.
- **Video playback**: An authorized asset-scoped ticket grants two-hour access to `/media/stream/{asset_id}`. The endpoint serves the original MP4/WebM with HTTP Range support; no transcoding, HLS, audio-only playback, or ugoira ZIP playback is performed.
- **metadata.json**: Written per-work during import. Contains work_id, source, source_work_id, title, posted_at, creator, assets array.
- **source_work_id**: The platform-specific work ID (e.g. Pixiv artwork ID "38362603"). Used as the directory name in both downloads/ and library/. Links the two storage trees.

## File, Upload and Rollback Boundaries

Cookie file configuration uses `services/cookie_paths.py`. Relative paths are
rooted at `GALLERYDL_CONFIG_ROOT`; absolute paths inside that root, historical
`/gallerydl-config/...` container paths and `/cookies/...` shorthand remain
supported. Resolved symlinks must stay inside the root. Configuration reads,
writes and effective download configs reject escapes with a diagnostic error,
rather than starting an unconfigured download.

Manual uploads accept at most 20 files, 500 MiB per file and 1 GiB of file data,
with a separate 2 MiB multipart allowance. Web uploads stream through the
dedicated `/api/upload` API handler to the existing backend endpoint.
Multipart parsing is disabled, and Node streams preserve backpressure. This
avoids Next.js external rewrite body cloning and its default 10 MiB truncation;
the frontend `/api/v1/upload` URL also routes internally to that handler.
Authentication, Origin, CSRF, backend status and Retry-After are preserved,
and client disconnects cancel upstream transfers. File, batch, total request
and account quota failures have distinct 413 codes. The page checks combined
uploads against the 20-file and 1 GiB budgets and only stops subsequent files
on a quota failure.

`UploadRoute` counts received bytes
before multipart parsing, including absent or misleading Content-Length.
Copying enforces file, batch and remaining quota budgets. Final usage is charged
by a quota-conditioned database UPDATE in the same transaction as jobs and
artifacts; concurrent uploads and purge releases do not use stale accounting.
Failed copying owns temporary cleanup; failed moves or saves roll back usage
and remove the current work directory. Committed files/jobs retain their charge
while durable import intent handles later processing.

Personal uploads without an explicit target creator reuse their database identity
when the creator, subscription, manual source and aliases are complete and
consistent. The service verifies those records on each upload and refreshes
alias observation timestamps in one upsert; it does not cache identity state
across requests. Incomplete or changed identities, and uploads targeting an
explicit creator, follow the general identity-provisioning path. This reduces
repeated identity-maintenance queries while retaining the atomic user quota
update. `save_upload` owns and commits this save transaction; only after that
commit does it call `enqueue_import` to publish the durable import intent. That
UPDATE uses RETURNING to obtain and refresh the current user without a second
query while holding the quota lock. Identity resolution and file metadata use
the returned account, including when authentication supplies an object from
another session. Quota, identity, job, artifact and projection writes retain
their transaction and lock order.
For a complete personal identity with an enabled source, alias upserts refresh
observation times and compare returned row IDs and semantic fields. Timestamp-only
observations do not repeat creator and dependent-work projection fanout. Concurrent
alias changes or replacements, source re-enablement, initial provisioning, repairs
and explicit creator uploads retain projection registration. Exact database alias
search still uses observation times; import-time work and asset projections remain.

Foreground upload copying, persistence and import publication hold the shared
POSIX maintenance barrier. Registered clear operations retain its exclusive
parent-side lock through the workhorse; an upload during maintenance returns
`503 / maintenance_in_progress` with Retry-After. Media clear preserves `.locks`,
keeps ownership and charge facts until file deletion succeeds, cancels pending
pipeline jobs through the existing lifecycle, and releases only the removed
uploads' charges. File failure returns `status=partial` and a per-path report;
the registered task becomes failed. Database records and charges remain for
retry, although some files may already be gone. Retry the same clear after
fixing the filesystem error. File deletion cannot be undone if the later
database commit fails; retained database facts allow that failed clear to retry.

HTTP purge also holds the shared barrier from asset eligibility and quota
release through file removal, commit and result retrieval, and returns the same
503 during maintenance. Clear and purge cannot interleave to return one charge
twice.

Offline restore and deployment rollback stop writers and query Compose for
running, restarting or paused containers before switching files, databases,
Redis data or application images. Failed stop/inspection blocks compensation
and foreground restart. The offline journal records blocked components; the
deployment receipt records `paused`. Operators fix the stop failure and rerun
the existing rollback command, preserving its schema-forward policy.

## Environment Variables

Key environment variables used by the application:

| Variable | Default | Purpose |
|---|---|---|
| `BACKEND_PORT` | `8818` | Host port for backend API (maps to container 8000) |
| `ADMIN_WEB_PORT` | `13000` | Host port for admin web (maps to container 3000) |
| `CORS_ORIGINS` | `http://localhost:13000` | Allowed origins for cross-origin API clients |
| `BROWSER_SESSION_ORIGINS` | `http://localhost:13000` | Exact allowed HTTP/HTTPS browser origins for session, CSRF and WebSocket checks |
| `BACKEND_INTERNAL_URL` | `http://backend:8000` | Internal URL for admin-web SSR to reach backend |
| `MEDIA_PLAYBACK_TTL_SECONDS` | `7200` | Lifetime of an asset-scoped MP4/WebM playback ticket |
| `NEXT_PUBLIC_WS_URL` | current site `/api/v1/ws` | Public browser WebSocket URL for live job updates |
| `DATABASE_URL` | (required) | PostgreSQL connection string |
| `REDIS_URL` | (required) | Redis connection string |
| `SECRET_KEY` | (required) | Bearer JWT signing secret |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Compose: `1440` | Bearer token and browser session lifetime in minutes |
| `TIMEZONE` | `UTC` | Timezone for scheduler and display |
| `DOWNLOAD_ROOT` | `/downloads` | Original file storage path |
| `LIBRARY_ROOT` | `/library` | Metadata + thumbnail storage path |
| `GALLERYDL_CONFIG_ROOT` | `/gallerydl-config` | gallery-dl configuration path |
| `APP_CONFIG_ROOT` | `/app-config` | Application config path |
| `PERSONAL_AUTH_TMP_ROOT` | `/run/auto-gallery-secrets` | worker-download tmpfs for personal authentication overlays |

## API Route Groups

All routes are prefixed with `/api/v1` unless otherwise noted.
This section is an architectural map rather than an exhaustive endpoint list.
The generated [OpenAPI and AsyncAPI contracts](api/README.md) are the normative
machine-readable interface and are checked against the running application in
CI.

### `/api/v1/system`
Health, queue stats, storage stats, clear failed jobs, log buffer.

Endpoints:
- `GET /health` and `GET /ready` — public liveness and dependency readiness
- `GET /storage` — logical library storage stats
- `GET /workbench` — dashboard status and recent activity
- `GET /queue-stats` — pending/running/failed job counts
- `GET /scheduler-decisions` — current repository scheduling decisions
- `POST /clear-failed-jobs` — clear all failed download/import jobs
- `GET /logs` — tail buffered log output

### `/api/v1/admin`
Settings, gallery-dl config, system info, integrity, backup, import progress, proxy testing, scheduler control, dedup, merge candidates, reindex, data cleanup.

Endpoints include:
- `GET /settings` / `PUT /settings` — system settings CRUD
- `POST /reset-settings` — reset settings to defaults
- `GET /gallerydl-config` / `PUT /gallerydl-config` — per-source gallery-dl extractor config
- `GET /system-info` — system overview (CPU, memory, disk, database size, archive sizes)
- `GET /integrity-check` — scan for orphaned files, missing thumbnails, orphaned records
- `POST /backup` — create full system backup (pg_dump + configs + archives)
- `GET /backup` — list available backups
- `GET /backup/download` — download a backup file
- `POST /backup/restore` — restore from a backup archive
- `POST /backup/schedule` — enable/disable recurring auto-backup
- `POST /cleanup-metadata-jsons` — remove stale JSON metadata files
- `GET /import-progress` — real-time import progress polling
- `POST /proxy/test` — test proxy connectivity with gallery-dl
- `POST /scheduler/sync-now` — trigger immediate subscription sync
- `POST /library/import-from-disk` — idempotently import on-disk download files into the DB without re-downloading (operations queue)
- `POST /clear/{entity}` — clear all records of a given entity type (`jobs`/`all` also clear `task_runs`)
- `GET /dedup` / `PUT /dedup` — deduplication settings
- `GET /merge-candidates` — deprecated compatibility alias returning pending asset-reconciliation cases; new clients use `/api/v1/admin/dedup/cases`
- `POST /reindex` — trigger full Meilisearch reindex

### `/api/v1/creators`
Creator CRUD, count, timeline, duplicate detection, merge, favorite, source-account binding, creator links.

Endpoints include:
- `GET /` — list creators (paginated, searchable)
- `POST /` — create creator
- `GET /count` — total creator count
- `GET /{id}` — creator detail with source accounts and links
- `PUT /{id}` — update creator
- `DELETE /{id}` — delete creator
- `GET /{id}/timeline` — publishing activity timeline
- `GET /duplicates` — detect potential duplicate creators
- `POST /merge` — merge two creators into one
- `POST /{id}/favorite` / `DELETE /{id}/favorite` — toggle favorite
- `POST /{id}/source-accounts` — bind a source_creator to this creator
- `DELETE /{id}/source-accounts/{sc_id}` — unbind a source_creator
- `GET /{id}/links` / `POST /{id}/links` — manage creator_url links
- `PUT /links/{link_id}` — update link (approve/reject)

### `/api/v1/subscriptions`
Subscription CRUD, source toggling, batch operations, sync triggers.

Endpoints include:
- `GET /` — list subscriptions
- `POST /` — create subscription
- `GET /count` — total subscription count
- `GET /{id}` — subscription detail with sources
- `PUT /{id}` — update subscription
- `DELETE /{id}` — delete subscription
- `POST /batch-delete` — delete multiple subscriptions
- `POST /batch-toggle-sync` — toggle sync for multiple subscriptions
- `POST /{id}/sync-now` — trigger immediate sync for this subscription
- `GET /{id}/sources` — list sources for a subscription
- `PUT /{id}/sources/{source}` — enable/disable/toggle a subscription source

### `/api/v1/download-jobs`
Download job queue management with batch operations and per-job control.

Endpoints include:
- `GET /` — list download jobs (paginated, filterable by status)
- `POST /` — create download job
- `GET /{id}` — job detail
- `DELETE /{id}` — delete job
- `POST /clear` — clear all completed/failed jobs
- `POST /kill-stuck` — kill all stuck jobs (stale detection)
- `POST /retry-all` — retry all failed jobs
- `POST /batch-delete` — batch delete jobs
- `POST /{id}/pause` — pause a job
- `POST /{id}/resume` — resume a paused job
- `POST /{id}/retry` — retry a specific failed job
- `POST /{id}/cancel` — cancel a job (terminal, keeps record) — TaskEngine
- `POST /{id}/priority` — change queue priority — TaskEngine
- `POST /batch-by-filter` — apply an action to all jobs matching a filter — TaskEngine
- `GET /{id}/progress` — live progress snapshot
- `GET /{id}/pipeline` — per-stage pipeline view

### `/api/v1/import-jobs`
Import job queue and post-import scanning. List entries are enriched with parent download-job context (source, url, subscription/creator) so they align with the download list.

Endpoints include:
- `GET /` — list import jobs (enriched with source/subscription/creator)
- `GET /{id}` — import job detail with errors
- `POST /{id}/retry` — retry a failed import
- `POST /{id}/cancel` — cancel an import — TaskEngine
- `POST /{id}/priority` — change queue priority — TaskEngine
- `POST /batch-by-filter` — apply an action to all imports matching a filter — TaskEngine
- `GET /{id}/progress` — live progress snapshot
- `POST /scan` — scan for unimported downloads and create import jobs

### `/api/v1/works`
Work browser with batch operations, tagging, and source detail.

Endpoints include:
- `GET /` — list works (paginated, filterable)
- `GET /{id}` — work detail
- `PUT /{id}` — update work
- `DELETE /{id}` — delete work
- `POST /batch-delete` — batch delete works
- `POST /batch-tag` — batch add/remove tags
- `GET /{id}/assets` — list assets for a work
- `GET /{id}/sources` — list work_sources for a work
- `GET /{id}/tags` — list tags for a work
- `POST /{id}/favorite` / `DELETE /{id}/favorite` — toggle favorite

### `/api/v1/tags`
Tag management with alias and merge support.

Endpoints include:
- `GET /` — list tags
- `POST /` — create tag
- `GET /{id}` — tag detail
- `PUT /{id}` — update tag
- `DELETE /{id}` — delete tag
- `POST /{id}/alias` — set tag alias
- `POST /merge` — merge two tags into one

### `/api/v1/search`
Canonical compound search across Meilisearch-backed library entities and
SQL-backed operational collections.

Endpoints include:
- `GET /` — grouped works, creators, tags, repositories, subscriptions, tasks, or scheduler decisions
- `POST /assist` — server parsing, diagnostics, suggestions, and query composition

See [Compound search language](search.md). Full index rebuilds are queued from
the data-management API.

### `/api/v1/sources`
Source provider listing and capabilities.

Endpoints:
- `GET /` — list all registered source providers with capabilities
- `GET /{source}` — specific provider capabilities

### `/api/v1/reference`
Danbooru reference provider operations for creator identity mapping.

Endpoints include:
- `POST /danbooru/preview` — preview Danbooru artist tag results
- `POST /danbooru/import` — import a single Danbooru artist reference
- `POST /danbooru/batch-import` — batch import from Danbooru

### `/api/v1/auth`
Authentication endpoints.

Endpoints:
- `POST /login` — script-client login, returns a Bearer JWT
- `POST /browser/login`, `POST /browser/logout` — create or revoke a Redis browser session
- `POST /browser/change-password` — rotate the browser session without returning a JWT
- `POST /ws-ticket` — issue a one-use WebSocket ticket
- `GET /me` — current authenticated user info
- `POST /change-password` — Bearer-only password change


### `/media` (unversioned)
Controlled media file serving. Not prefixed with `/api/v1`.

Endpoints:
- `GET /thumb/{asset_id}` — private 400px WebP thumbnail from LIBRARY_ROOT; active session/Bearer authentication or an asset-and-purpose-scoped signed ticket is required before reading or requesting derivatives
- `GET /preview/{asset_id}` — original file from DOWNLOAD_ROOT, requires a short-lived signed URL
- `GET /poster/{asset_id}` — large derived WebP video poster, requires a signed URL
- `GET /stream/{asset_id}` — original MP4/WebM with HTTP Range support, requires an asset-scoped playback ticket
- `GET /original/{asset_id}` — original file from DOWNLOAD_ROOT (for download)


### Bounded download execution

Download consumers and their execution slots share one concurrency resolver.
The persisted 1–5 setting remains restart-applied; deployment and device limits
cap the consumers. An explicit device download limit affects downloads only,
while the automatic profile continues sizing other queues and publication.
Each child inherits its supervisor's effective count, preventing a larger
consumer pool from accidentally sharing one execution slot.

Redis reserves each download's memory atomically against all resource profiles.
A short POSIX admission mutex and fixed slot locks retain the execution limit
when coordination keys disappear or the configured pool shrinks. The legacy
single network token conflicts with new slots while its Redis identity exists;
old-version processes must be stopped before the upgrade. Single-slot defaults
retain their profile memory reserve; parallel mode reserves at least 256 MiB.
Same-platform jobs retain shared-archive exclusivity, including local file locks.
Failure to determine archive identity defers the job before claim or subprocess
start. If cancellation arrives during Redis acquisition, the operation completes
and releases its owned keys before cancellation propagates. The existing
process-group cleanup precedes release of the source lock and slot. See
[deployment configuration](deployment-profiles.md#download-worker-concurrency--下载并发).

### Bounded import consumers

`IMPORT_WORKER_CONCURRENCY` selects 1–4 import consumers, with one as the default.
An explicit `worker_entrypoint.py imports N` argument overrides the environment;
changing the environment requires recreating the import service. The independent
maintenance consumer is counted separately in supervisor queue statistics.

Execution claims retain the DownloadJob → ImportJob → TaskRun lock order and a
current execution token. Work claims use a representative metadata artifact,
`FOR UPDATE SKIP LOCKED` and a lease for the source/work identity. Every bounded
claim and database phase still takes the shared ingest reservation and POSIX
locks; resource waits hold no SQL transaction. Concurrent consumers can overlap
job preparation and waiting, while the database slices remain serialized by
resource admission. Batch commits own domain rows and subsequent media, curation
and search outboxes together. Recovery and stale-token fencing remain independent
of the selected consumer count. See
[import deployment configuration](deployment-profiles.md#import-worker-concurrency--导入并发).

### Parallel image execution

`MEDIA_WORKER_CONCURRENCY` sets the image execution slot limit and the Compose
operations consumer count, from 1 through 8 with a default of 1. Ordinary and
large images share the same pool. Redis admits slot ownership and aggregate
memory reservations atomically; shared POSIX barriers and independent slot
locks retain the local limit when Redis coordination keys disappear. The
existing database claim and version checks prevent duplicate computation or
stale result submission for the same asset.

Parallel mode reserves at least 768 MiB per image. Memory pressure or another
active resource profile can admit fewer images than the configured limit.
Video, indexing, deduplication, maintenance and unfinished remote search writes
retain their exclusive barriers. Media wake jobs own distinct bounded slots;
shrinking the pool preserves live owners while preventing successors above the
new limit. Image algorithms, source verification and result lease checks
retain their existing versions and rules.

Apply the same setting to the API and Python worker roles. See
[deployment profiles](deployment-profiles.md#image-worker-concurrency--图片处理并行数)
for connection and thread budgets. Select a limit using the device's CPU,
available memory and measured workload rather than assuming one value suits
every deployment.

### Shared visual working pixels

Media derivation keeps the shared working-image path for eligible static images
of at least 16 million pixels requiring a new pHash. It materializes an image
with an edge no larger than 1024 pixels, applies EXIF orientation to the reduced
pixels, and reuses them for pHash, a 256×256 RGB analysis sample, and a missing
card thumbnail when sufficient pixels are available. The process-wide libvips
cache ceiling remains 64 MiB. Existing first-card thumbnails are reused and
elongated images retain the existing thumbnail policy. Images over a 4:1 aspect ratio and unsupported static-image layouts keep direct sampling.

Only when the same media task needs a pHash and is missing a card or dimensions,
a single-frame RGB JPEG or RGB/RGBA PNG without PNG color-key transparency may
share its full-size Pillow image within that task, provided the original width
is over 400 pixels and the area is at most 8 million pixels. This avoids a second
decode and loading libvips solely for that card. The Pillow card preserves its
embedded ICC profile when encoding WebP. Images with an existing first
card, larger images, CMYK JPEGs and other layouts keep their existing paths.
This bounded working image preserves direct pHash sampling and is closed when
the task's derivation returns or raises; it does not eagerly create an SSIM sample. A
`WorkingImage` that is already RGB is sampled without another RGB copy; direct
source sampling releases the decoded image before loading the numeric library.
Analysis samples for other images remain on-demand SSIM work using the existing
direct resize.

The fingerprint version is `numpy-phash-oriented-adaptive-v3` and the media
version is `media-visual-v3`. Reuse requires matching fingerprint and source
versions; earlier pHash values must be recomputed and different pHash versions
are never compared. Visual candidates use oriented display aspect ratios;
stored source dimensions, exact SHA256 deduplication and representative
selection remain unchanged.

Analysis samples remain lossless RGB bytes in 1024 fixed slots under
`library/.visual-samples`, totaling approximately 193 MiB. Headers bind version,
asset ID, source size/mtime, existing SHA256 and display dimensions. Collisions,
missing files and stale headers are cache misses; replacement is atomic.
The 1024 working image is never persisted. Each reconciliation keeps at most
16 MiB of sample bytes; float arrays are temporary. SSIM keeps its existing
256×256 sample, formula, thresholds and cache behavior. Asset deletion cleans up
its owned slot. Quarantine still verifies original SHA256 and regenerates
fresh samples from originals; the cache cannot replace this check.

For an existing library, stop scheduler and pipeline workers during upgrade,
then run a temporary import-worker container with the new code and its media budget:

```sh
docker compose run --rm --no-deps worker-import python -m app.services.visual_fingerprint_rebuild --enqueue --process
```

It batches intents through the media outbox and processes one admitted asset
at a time; rerunning resumes an interrupted upgrade. With no flags it reports
status only. Resume consumers once `remaining_available` and `pending_fingerprints` are both zero and processing has no failures, then request
a global dedup scan through the existing administration entry point.
Unavailable purged/quarantined historical members retain old fingerprints and
groups and are counted separately as `unavailable_historical`; available
representatives are upgraded. Existing evidence is preserved; new evidence
uses `numpy-phash-oriented-adaptive-v3` and `media-visual-v3`. The upgrade does
not rebuild or delete existing groups.
