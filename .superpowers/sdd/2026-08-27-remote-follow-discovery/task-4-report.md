# Task 4 Report: Shared Scheduling, Private Download Auth, and Discovery Queue

## Outcome

Implemented Task 4 on `feat/remote-follow-discovery` from base `fbc63e4`.
Canonical creators, subscriptions, sources, jobs, works, and files remain
shared; user membership-source rows now own enablement, due time, and
credential health. No live provider call or real credential was used.

Commits:

- `fc8c69f feat: schedule shared sources by private demand`
- `ce6936d feat: run private auth and remote discovery queues`
- `e21fb1d fix: enforce private shared scheduling authority`
- `e8a3734 fix: close private auth leakage paths`

## Shared scheduling and aggregate cache

- Extended the existing sole aggregate,
  `recompute_subscription_membership_cache`; no competing aggregate was
  introduced.
- A canonical `SubscriptionSource` is enabled and auth-usable only when at
  least one active, sync-enabled member binding is enabled and usable. Its
  `next_sync_at` is the earliest eligible private binding due time.
- Canonical legacy subscription schedule fields are kept constraint-consistent
  when all private members disable sync or a later member re-enables it. They
  remain compatibility data, not the scheduling authority.
- The scheduler claims/locks the canonical due source, then locks and selects
  the earliest due eligible `UserSubscriptionSource`. A healthy personal
  account is required when `remote_account_id` is non-NULL; NULL retains the
  legacy/global gallery-dl config path.
- Automatic selection respects an inherited system `manual` mode. Accepted
  publication advances the selected member binding by its own schedule and
  recomputes the canonical due cache.
- The canonical source lock plus the existing running-job uniqueness boundary
  produces one shared `DownloadJob`. Only opaque
  `triggering_user_subscription_id` and `triggering_remote_account_id` are
  recorded.
- A successful shared download replans every active member binding using its
  own interval/calendar policy. Credential health is restored only when both
  opaque IDs match the selected private binding.
- Auth failure uses a fixed safe reason vocabulary and changes only the
  selected binding/account. Recomputing the canonical cache immediately lets a
  healthy peer take over. Non-auth failures do not change credential health.

## Execution-time personal authentication

- The download worker resolves the selected account from opaque IDs and
  verifies binding/account/source ownership before decrypting.
- Decryption occurs only at execution. The Task 2 adapter's
  `build_download_auth()` output is deep-merged over the non-secret effective
  gallery-dl config, so the selected account's auth override wins.
- The per-job config is created with `mkstemp`, explicitly forced to mode
  `0600`, and deleted on normal completion, subprocess failure, timeout,
  pause/cancel, configuration error, and unexpected exception paths.
- Credential jobs do not persist the effective config, private path, or
  private `--config` command argument. Subprocess output is redacted before it
  reaches progress buffers, Redis, TaskRun state, manifests, or logs.
- Unexpected-error text removes both secret values and the ephemeral config
  identity. Private exceptions are logged without a traceback that might
  reproduce sensitive arguments.
- Authentication-classified failures do not retry the same broken credential;
  they safely fail the job and make another eligible member available.

## Discovery queue and admission

- Added the independent RQ queue `discovery` and opaque worker callable
  `app.jobs.remote_discovery.run_remote_discovery_scan`.
- Due account admission uses PostgreSQL `FOR UPDATE SKIP LOCKED`, requires an
  enabled/healthy account with ciphertext, respects `next_scan_at`, and reuses
  the persistent scan TaskRun single-flight check.
- TaskRuns are committed before Redis publication and use deterministic
  attempt-specific RQ IDs plus bounded RQ retry intervals. Publication failure
  is compensated to a safe failed TaskRun and a five-minute account retry
  horizon.
- Manual scan API admission now follows the same durable prepare/commit/publish
  boundary. Discovery page commits update the TaskRun heartbeat.
- The recurring subscription scheduler admits due remote accounts, and
  `worker-operations` listens to `discovery` through `WORKER_EXTRA_QUEUES`.
  Compose/docs/feature-flag polish remains intentionally available to Task 6.

## TDD evidence

Focused RED/GREEN cycles were executed in an isolated copy of this worktree in
the running backend container, against the real PostgreSQL test database.

Observed REDs included:

- the canonical source cache selected an earlier unhealthy account;
- scheduler enqueue created a job without private trigger IDs;
- shared success left private member timestamps unchanged;
- the worker lacked execution-time personal auth materialization;
- auth failure had no private-account/binding update path;
- due remote accounts had no scheduled discovery admission;
- disabling all memberships violated the canonical subscription
  schedule/sync check constraint;
- inherited system-manual memberships remained automatically eligible;
- raw auth reason text could persist a canary;
- successful finalization could heal an account that did not match the opaque
  membership provenance;
- no helper removed secret values and the private temp path from unexpected
  worker errors.

The final focused suite covers earliest healthy selection, no eligible
fallback, NULL-account compatibility, two concurrent users/one canonical job,
schedule fan-out, auth-failure peer takeover, personal config merge/mode and
cleanup, canary absence from DB/TaskRun/Redis/manifest/log capture, and
concurrent discovery admission/queue routing.

Final focused outcome: `14 passed in 5.51s`.

## Final verification

Final code commit (`e8a3734`) combined relevant regression command:

```text
PYTHONPATH=/tmp/ag-task4-red pytest -q \
  tests/test_shared_remote_scheduling.py \
  tests/test_remote_membership_services.py \
  tests/test_scheduler_contract.py \
  tests/test_scheduler_manual_all_enabled.py \
  tests/test_subscription_reliable_schedule.py \
  tests/test_subscription_schedule_consistency.py \
  tests/test_subscription_scheduler_persistence.py \
  tests/test_subscription_overview.py \
  tests/test_queue_routing_scheduler.py \
  tests/test_queue_admission.py \
  tests/test_download_dispatch.py \
  tests/test_download_finalization.py \
  tests/test_download_job_context.py \
  tests/test_download_outcome.py \
  tests/test_download_process_lifecycle.py \
  tests/test_download_staging.py \
  tests/test_worker_concurrency.py \
  tests/test_resource_aware_worker.py \
  tests/test_remote_account_services.py \
  tests/test_remote_credentials.py \
  tests/test_remote_discovery_adapters.py \
  tests/test_remote_discovery_contract.py \
  tests/test_remote_discovery_persistence.py \
  tests/test_remote_discovery_provider_contracts.py \
  tests/test_remote_discovery_services.py -x
```

Outcome: `242 passed in 228.47s`.

Additional final checks:

- `python -m compileall -q app tests/test_shared_remote_scheduling.py` — passed.
- Ruff over every changed Python production/test file — `All checks passed!`.
- `docker compose config --quiet` — exit 0; only expected warnings for unset
  local secret environment variables.
- `git diff --check fbc63e4..HEAD` — no output.

## Self-review and risks

- Inspected `git diff fbc63e4..HEAD`; changes are scoped to shared scheduling,
  download execution/finalization, discovery admission/worker routing, tests,
  and the necessary worker environment/queue inventory.
- Verified plaintext/canary values are absent from DownloadJob, TaskRun,
  manifest, Redis progress capture, and captured logs in the full worker test.
- Verified real PostgreSQL row locking for both concurrent shared enqueue and
  concurrent discovery admission.
- The discovery queue currently shares the operations worker process via an
  independent queue name; Task 6 may choose a dedicated service/container when
  finalizing rollout controls and operational documentation.
- Temp-file unlink is intentionally best-effort in the cleanup helper. The
  tests prove deletion on exercised exits; an underlying filesystem failure is
  still an operational alert/host-hardening concern rather than a database or
  queue fallback.
- The pre-existing untracked `admin-web/node_modules` symlink was not touched
  or added.

## Review Fix Round 1 (2026-08-28)

Review fixes were implemented from base `fb79d6b` without live provider calls.
Additional commits:

- `6e894cd fix: redact derived download credentials`
- `28e5660 fix: recover private credential health safely`
- `29fe56f fix: preserve manual download ownership`
- `c9f6210 fix: make private due scheduling authoritative`
- `851a515 test: isolate inherited member replan coverage`
- `c1cba50 fix: treat every auth override leaf as secret`
- `38a850a fix: keep disabled member demand unscheduled`

### Security and credential recovery

- Secret collection now covers every string leaf in both decrypted credential
  material and the adapter-produced auth fragment. This includes raw X Cookie,
  derived `auth_token`/`ct0`, flat provider-specific keys, custom headers, and
  structured/list variants.
- The full worker failure canary verifies raw and derived values are absent
  from DownloadJob error/progress/manifest/config provenance, TaskRun,
  isolated Redis progress, captured logs, exception text, and leftover temp
  files.
- AES-GCM/key/AAD/tamper errors and invalid adapter auth fragments are terminal
  for the selected credential provenance and do not retry that credential.
  Filesystem/temp-storage failures remain retryable system failures and do not
  damage credential health.
- Credential replacement and successful account validation heal all bindings
  for that owned account and recompute every affected canonical cache. Failed
  validation safely marks only that account and its bindings unhealthy.
- Legacy canonical auth is poisoned only when both opaque trigger IDs are
  NULL. Stale/mismatched private provenance is a no-op, including account
  rebinding while a job is running.

### Manual provenance and authoritative scheduling

- Explicit sync-now may use an owned active/enabled/healthy manual membership
  even when automatic canonical cache state is disabled. Exact membership and
  account ownership are required; another user's earlier credential can never
  be substituted.
- DownloadOrchestrator validates authenticated caller provenance and forwards
  the exact opaque IDs for both canonical-source and generic URL jobs.
- Membership schedule mutations replan all locked private source bindings for
  interval, calendar, manual, and inherited policy, then call the existing sole
  canonical aggregate. System setting changes replan inherited private rows;
  canonical-only rows retain legacy compatibility.
- Whole-policy inheritance uses current system interval/calendar settings
  rather than stale fields copied during migration. Disabled private bindings
  retain shared receipt timestamps after peer success but remain unscheduled.
- Canonical aggregation preserves NULL when any eligible binding is unseen/
  immediately due; timestamps are minimized only when all eligible values are
  non-NULL.
- Enqueue durably claims the selected private binding in the same transaction
  as the outbox before Redis publication. It performs no stale ORM due write
  after publication, so fast success fan-out wins. Rejected publication uses a
  compare-and-swap restoration of the original logical demand.

### Fix-round TDD and verification

Focused REDs reproduced every reviewed gap, including derived Cookie child
leaks, stale account poisoning, failed account-health recovery, manual-owner
substitution, unchanged private due rows, mixed NULL/future aggregation, and
the deterministic fast-finalizer race. Additional self-review REDs covered
flat/custom auth leaves and disabled binding success scheduling.

Verification results:

- D-focused plus membership/scheduler/replan baselines: `51 passed in 60.81s`.
- Full review regression across 30 Task 4, membership/manual/scheduler,
  download/finalization/retry/task, discovery/provider, queue, and worker
  suites: `375 passed in 385.29s`.
- Post-hardening affected suites: `72 passed in 15.24s`.
- Flat/structured secret collection plus full worker Cookie canary:
  `2 passed in 1.80s`.
- Disabled-binding fan-out plus success/race baselines: `3 passed in 1.45s`.
- `python3 -m compileall -q backend/app ...` — passed.
- Ruff over every Python file changed from `fb79d6b` — `All checks passed!`.
- `docker compose -f docker-compose.yaml config --quiet` — exit 0 with only
  expected warnings for unset local secret variables.
- Production-code canary scan — no Task 4 canary literal found.
- `git diff --check fb79d6b..HEAD` — no output.

Exact `git diff fb79d6b..HEAD` was self-reviewed. Remaining operational risks
are unchanged: temp unlink is best-effort under filesystem failure, and the
independent discovery queue still shares the operations worker process until
Task 6 finalizes deployment controls. Test isolation forced the dedicated
`autogallery_test` PostgreSQL database and Redis DB 15. The pre-existing
untracked `admin-web/node_modules` symlink was not touched or added.

## Review Fix Round 2 (2026-08-28)

Round 2 started from `8dacb8e` and closes the account deletion/enablement
eligibility boundary without changing legacy migrated NULL bindings.

- Account deletion locks and captures every affected private binding before
  changing credentials or foreign keys. Each binding becomes effectively
  disabled through `auth_healthy=false`, safe `deleted` status/reason, and a
  NULL due time while retaining its private `is_enabled` preference.
- Hard deletion flushes that safe state and clears `remote_account_id` before
  deleting the account, satisfying the PostgreSQL `RESTRICT` FK without
  converting the binding into usable legacy/global authentication.
- Imported provenance keeps a credential-free, list-invisible tombstone and
  retains the binding account FK. Reconnecting revives the same account and
  remains ineligible until successful validation explicitly heals its related
  bindings and canonical caches.
- Account `is_enabled` changes transactionally recompute all affected
  canonical caches. Disable/re-enable never changes binding preference and
  never heals an auth-failed binding merely through a toggle.
- Legacy bindings whose account ID was NULL and whose health was valid from
  inception remain eligible for the global configuration path.

TDD evidence on real PostgreSQL:

- RED: tombstone deletion cleared the provenance FK; hard deletion left the
  resulting NULL binding healthy/selectable; account disable left the
  canonical source enabled.
- Focused GREEN: `3 passed, 7 deselected in 1.50s`.
- Account/delete/discovery/membership/Task 4/scheduler regression:
  `103 passed in 113.07s`.
- Compileall, Ruff for both changed Python files, and `git diff --check` all
  passed. No live provider call or real credential was used.

## Review Fix Round 3 (2026-08-28)

Round 3 started from `af81385` and closes stale-outcome monotonicity and the
private credential lifecycle deadlock.

- Tombstoned and hard-deleted bindings are quarantined from shared success
  fan-out. A stale receipt may still replan healthy peer bindings, but it does
  not change the deleted binding's receipt, due time, health, safe reason, or
  authentication check timestamp.
- Success and authentication-failure outcomes lock and validate the opaque
  trigger account before locking member-source rows. Under that lock they
  require an enabled, non-deleted account with ciphertext and matching binding
  ownership before changing private health.
- The persisted `DownloadJob.created_at` is carried into success finalization
  as a non-secret credential-generation boundary. If the account was deleted,
  replaced, validated, or revived after the job was created, that older result
  cannot heal or damage the current same-ID credential generation.
- The deterministic lock order for all credential lifecycle and outcome paths
  is documented as `RemoteAccount` first, then
  `UserSubscriptionSource` ordered by ID, then the sole canonical aggregate.
  Canonical mutations were moved after member-source locks to prevent ORM
  autoflush from silently taking a source lock out of order. Discovery scan
  admission/execution only locks the account row and does not introduce a
  reverse member-source edge.
- Legacy migrated bindings that were healthy with `remote_account_id=NULL`
  remain supported. A stale non-NULL account ID that was hard-deleted cannot
  fall through into that legacy path or poison the canonical source.

Real PostgreSQL TDD evidence:

- Initial tombstone/reconnect RED: `3 failed, 10 deselected in 2.22s`.
  Stale success changed `deleted` to `healthy`; stale auth failure changed it
  to `unhealthy`; same-ID reconnect lacked an outcome-generation boundary.
- Deterministic two-session barrier RED: `2 failed, 29 deselected in 4.63s`.
  Both delete-vs-success and validate-vs-auth-failure raised PostgreSQL
  `DeadlockDetectedError` from the reversed account/member-source locks.
- Focused GREEN after the fix: `5 passed, 39 deselected in 3.62s`.
- Complete account and shared-scheduling files: `44 passed in 14.00s`.
- Broad 26-file Task 4/account/membership/scheduler/download/finalization/
  worker/discovery/task regression: `291 passed in 364.22s`.
- `python3 -m compileall -q backend/app ...` passed; Ruff reported
  `All checks passed!`; the production canary scan and
  `git diff --check af81385..HEAD` produced no findings.

Implementation commit: `5dd9219 fix: quarantine stale remote account
outcomes`. Exact `git diff af81385..HEAD` was self-reviewed. No schema or
public API changes were required, no live provider call or real credential was
used, and the pre-existing untracked `admin-web/node_modules` symlink remains
untouched. The generation boundary intentionally treats any intervening
account-row update as newer provenance; this is conservative for health
mutation while shared content fan-out still proceeds for eligible peers.

## Review Fix Round 4 (2026-08-28)

Round 4 started from `6a76c68` and replaces the temporary timestamp boundary
with durable, explicit credential identity while removing the two remaining
row-lock inversions.

Additional commits:

- `83e2225 feat: persist remote credential generations`
- `9072d2b fix: serialize credential provenance and lifecycle locks`
- `a8e0d26 test: preserve canonical unhealthy expectations`
- `a290617 fix: distinguish deleted private jobs from legacy`

### Explicit credential generation

- Alembic revision `f7c9e1a3b5d7` adds non-null
  `RemoteAccount.credential_generation` and nullable
  `DownloadJob.triggering_credential_generation`, with named non-negative
  constraints. Existing ciphertext-bearing accounts backfill to generation 1;
  credential-less accounts/tombstones remain generation 0; pre-existing jobs
  remain NULL and therefore have unknown private provenance.
- Credential generation increments only when `_encrypt` installs a new
  credential identity or account deletion revokes it. Creation, replacement,
  same-ID reconnection, tombstoning, and hard deletion all use that seam.
  Validation, health/status, selectors, scan policy, and remote identity
  updates do not increment it.
- Shared-source enqueue snapshots the selected account generation into the
  canonical DownloadJob. Retry retains the same row and generation. Worker
  materialization rejects unknown or mismatched private generations before
  decrypting. Success and authentication-failure outcomes require the locked
  account to be enabled, non-deleted, credential-bearing, and an exact
  generation match before changing private account/binding health. Shared peer
  fan-out remains independent from trigger-health mutation.
- Generation is deliberately absent from RemoteAccount API schemas, TaskRun,
  Redis progress, manifest/progress payloads, logs, and command provenance. It
  is a non-secret integer and no credential hash/digest or reconstructable
  fragment is persisted.

The transaction-start inversion test begins a PostgreSQL replacement
transaction before enqueue, then installs/commits the replacement after the
job commit. PostgreSQL timestamps alone would order that replacement before
the job; explicit generations correctly leave the job at 1 and the account at
2, so its stale result cannot mutate the replacement. A separate policy-update
test proves an unrelated `updated_at` change leaves an exact generation valid.

### Deterministic lock order and CAS restoration

- Publication-failure restoration now acquires private rows in
  `RemoteAccount -> UserSubscriptionSource -> Subscription ->
  SubscriptionSource` order (legacy starts at the binding), matching lifecycle
  and outcome paths. It revalidates membership/account/generation provenance
  after locking and restores only the exact enqueue claim by CAS. A concurrent
  detach/disable edit wins without its policy or due value being overwritten.
- Account deletion now locks
  `RemoteAccount -> DiscoveryCandidate (sorted by id) ->
  UserSubscriptionSource (sorted by id) -> canonical aggregate`. Candidate
  import starts at the already-locked candidate and never acquires the account
  row, so it can finish before delete obtains that candidate rather than
  forming the former Candidate/USS cycle. Delete then re-reads the locked
  candidate state and produces either a clean hard deletion or an imported,
  credential-free tombstone. Resolve has the same candidate-first prefix and
  does not add a reverse edge.
- Real PostgreSQL barriers reproduce both former cycles and prove the fixed
  transactions finish coherently: publication restore concurrent with member
  detach, and pending candidate import concurrent with account deletion.

### TDD and final verification

Focused RED evidence:

- Schema/model RED: the model contract raised
  `AttributeError: credential_generation` (`1 failed, 1 deselected`).
- Generation behavior RED: new account generation remained 0 and scheduler
  jobs stored NULL instead of the selected generation (`2 failed in 0.83s`).
- PostgreSQL lock RED: publication restore/update_source and
  candidate-import/delete each raised `DeadlockDetectedError`
  (`2 failed in 3.79s`).
- Final provenance self-review RED: PostgreSQL `ON DELETE SET NULL` retained
  the old private job's non-NULL generation but removed its account ID, so the
  account-less path incorrectly treated it as a legacy job and marked a later
  healthy NULL binding unhealthy (`1 failed in 1.01s`). The account-less
  outcome path now accepts only generation-NULL jobs that were legacy from
  inception.

Fresh GREEN evidence after the final commits:

- Complete remote-account and shared-scheduling affected files:
  `50 passed in 14.87s` after the final provenance guard.
- Dedicated generation model plus isolated PostgreSQL migration
  upgrade/backfill/downgrade: `2 passed in 32.80s`.
- Existing migration-idempotency baseline: `10 passed in 65.31s`.
- Broad 27-file membership/scheduler/download/finalization/worker/discovery/
  task/provider regression: `303 passed in 371.82s`; the final one-predicate
  provenance guard was then covered by the complete affected-file rerun above.
- Ruff over every Python file changed from `6a76c68`: `All checks passed!`.
- `python3 -m compileall -q` over backend application, Alembic, and changed
  tests passed.
- `alembic heads` reports the single head `f7c9e1a3b5d7`.
- `docker compose --env-file /volume2/docker/auto-gallery/.env -f
  docker-compose.yaml config --quiet`, production canary/provenance scans, and
  `git diff --check 6a76c68..HEAD` all exited cleanly.

Exact `git diff 6a76c68..HEAD` was self-reviewed. No live provider request or
real credential was used. The only acknowledged residual is that the monotonic
generation uses a signed SQL integer; exhausting it would require more than
two billion credential rotations for one account. The pre-existing untracked
`admin-web/node_modules` symlink remains untouched.
