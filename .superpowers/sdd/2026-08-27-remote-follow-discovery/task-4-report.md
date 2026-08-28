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
