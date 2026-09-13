# D39/D40 source readiness review

Date: 2026-09-13

Base: `cdce8dbe822693c9eb05cb928b27aa52f0ba1748`

Verdict: **SOURCE APPROVED; RUNTIME ACCEPTANCE PENDING.** I found no Critical, Important, or Minor source defect in the reviewed D39/D40 application changes. The source implements the requested identity, authorization, frontend-link, and rolling resource-admission behavior. It is ready for the retained frontend fixture rerun and root-owned immutable/runtime gates. This is not approval of an image, deployment, or whole-site acceptance.

I performed a read-only review of `task-5-d39-d40-report.md`, `d39-d40-uncommitted-review.diff`, the current scoped source/test files, and preserved logs. I did not run tests, start or stop a process, access a live API, or mutate containers. The concurrently added `backend/app/services/search_delivery.py` and `backend/tests/test_search_delivery.py` changes are outside this review.

## Spec compliance

### D39 — resolved principal and operation identities

- **Authenticated principal forwarding is correct.** `backend/app/api/reference.py:104-112` resolves `RequirePermission("subscriptions")` and passes that actual user to `get_admin_operation(..., user=user)`. The generic reader remains the authority: `backend/app/api/admin/data.py:194-240` resolves a canonical TaskRun from either its UUID or deterministic RQ ID, calls `require_admin_surface_access`, and applies the same legacy-operation permission check. `backend/app/services/operations.py:181-185` registers `danbooru-mapping-refresh` to `subscriptions`; `backend/app/services/task_actions.py:306-320` retains operation-owner permission and private-task visibility checks. The mapping route applies its existing operation-kind 404 only after that shared access path (`reference.py:112-114`). No permission or status transition was weakened.
- **Canonical and transport IDs are separated.** The mapping enqueue/status response models expose `task_id` and `rq_job_id` (`reference.py:34-53`). Enqueue keeps historical pollable `job_id` as the RQ transport alias and publishes the same value explicitly as `rq_job_id` (`reference.py:89-97`). Status preserves that endpoint's transport `job_id` while retaining the generic reader's canonical `task_id` (`reference.py:115-122`). The generic operation reader continues to accept both identities and, for a durable task, returns logical `job_id` plus explicit `rq_job_id` (`backend/app/api/admin/data.py:198-228`).
- **Frontend navigation uses the canonical task.** Import-from-disk and creator re-enrichment toast actions and notification state use `task_id`, while polling continues with `job_id` (`admin-web/src/app/admin/data-mgmt/page.tsx:119-146`). Danbooru mapping refresh does the same (`admin-web/src/app/admin/upload/danbooru/page.tsx:331-349`). The API client now exposes these fields and uses the generated accepted-operation contract for the two data-management calls (`admin-web/src/lib/api/index.ts:668-679`, `694-707`, `769-787`). OpenAPI/generated TypeScript include the mapping identities (`docs/api/openapi.json:2406-2548`; `admin-web/src/lib/api/types.generated.ts:5572-5622`).
- **Notification compatibility is safe.** `NotificationCenter.tsx:38-48`, `65-71`, and `328-347` carry and persist `taskId` separately from the polling ID. Restored state can learn a validated canonical UUID from status (`NotificationCenter.tsx:404-426`, `440-510`). Navigation only uses a UUID-shaped `taskId`; retained transport-only legacy state produces no invalid Jobs link (`NotificationCenter.tsx:118-124`, `589-593`, `697-703`). `danbooru-import-all` keeps its established Danbooru destination.
- **Focused test intent matches the contract.** `backend/tests/test_operations.py:695-801` checks enqueue serialization, exact user forwarding for an administrator and a subscriptions-only principal, mapping-kind 404, and propagation of 403/private 404. The browser fixture uses deliberately distinct canonical and RQ IDs, asserts polling remains on the RQ ID, follows the clicked canonical URL to the Task drawer, restores canonical identity from status, and refuses a transport-only legacy link (`admin-web/tests/e2e/d39-operation-task-links.spec.ts:5-6`, `78-81`, `84-202`).

### D40 — parent admission for six unsliced handlers

- **New deliveries are correctly classified.** `backend/app/services/operations.py:43-65` lists the six unsliced handlers in `ADMIN_PARENT_ADMISSION_OPERATION_TYPES` and removes them from `_ADMIN_INTERNAL_RESOURCE_PROFILES`. `_enqueue_admin_rq` therefore omits `registered_admin_internal_profile` for these operations while retaining it for real cooperative coordinators (`operations.py:1581-1609`).
- **Rolling stale metadata is handled.** `backend/app/services/resource_aware_worker.py:437-481` checks the registered operation before trusting old `registered_admin_internal_profile` metadata and returns `None` for exactly the six unsliced types. The maintenance-queue fallback then classifies them as maintenance (`resource_aware_worker.py:483-500`). This covers a new worker consuming an old queued RQ record. New records also remain safe if consumed by an old worker because their internal-profile metadata is absent and the generic registered dispatcher name is not in the legacy child-slice function-name list.
- **Both parent protections cover the workhorse.** Once classified as unsliced maintenance, `_profile_admission` obtains the existing Redis resource lease and native maintenance lock (`resource_aware_worker.py:1036-1116`). `execute_job` marks the inherited flock before the workhorse and releases the native lock and Redis lease in the outer `finally`, including handler exceptions (`resource_aware_worker.py:1118-1199`). The six dispatch branches call their plain helpers directly and contain no child maintenance slice (`backend/app/jobs/admin_operations.py:547-645`). Genuine coordinators remain in the internal-profile map and still bypass the parent lease (`operations.py:51-65`; `resource_aware_worker.py:1019-1034`). Concurrency, capacity, and source-admission settings are untouched.
- **Tests cover the changed branches.** `backend/tests/test_resource_aware_worker.py:733-760` parameterizes all six stale-metadata classifications; lines 763-810 cover new publication metadata and retain a true internal coordinator control; lines 813-868 prove parent-lock lifetime and success/failure release around the workhorse. That final unit uses a stand-in local lock and intentionally removes Redis lease keys, so actual Redis/native ownership remains the separate root runtime gate described in `task-6-d40-native-lock-runtime-recipe-r1.md:32-65`.

## Evidence assessment

The author-reported backend evidence is internally consistent and preserved under `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/d39-d40-source/`: focused GREEN 12/12, resource-worker/owner-permission regression 39/39, operations regression 18/18, API contracts 10/10, Ruff clean, OpenAPI check clean, and generated TypeScript check clean. I did not repeat these checks. `git diff --check` over the D39/D40 scoped tracked files is clean.

Frontend evidence is not yet a complete GREEN gate:

- `frontend-green-r1.log`, SHA-256 `5533b3ee378bab18aa1ac3f98b2dcd0ea80de3546a224c8fe59562054f476daf`, records 4/5 passing. Import, mapping refresh, restored notification, and legacy no-link passed. Re-enrichment reached the old 90-second deadline during teardown amid slow Next development compilation; the log does not show an assertion mismatch.
- The current fixture now sets 150 seconds at `admin-web/tests/e2e/d39-operation-task-links.spec.ts:3`. This exact revision has not completed. `frontend-green-reenrich-r2.log`, SHA-256 `9f032126e08c2eb02f74ca3f36f615b22075e9f266f56392235cf3e3634b8ac1`, contains only `Running 1 test using 1 worker`; Plan mode interrupted it, so it has no result.
- These browser calls are intercepted and unknown API calls fail with 501 (`d39-operation-task-links.spec.ts:45-71`). A future pass proves caller/link behavior, not a real backend or whole-site action.

## Retained frontend execution recipe

Do not overwrite the two preserved logs above. The exact prior dev-server launcher was not persisted. What is established is that the fixture-only Next development server used the same current `/workspace/admin-web` source and served `http://127.0.0.1:3000` in `ag-button-fullsite-webtools`. This is a deterministic equivalent launcher; run it under a root-owned session/background controller and wait for HTTP readiness:

```bash
docker exec -w /workspace/admin-web \
  ag-button-fullsite-webtools \
  npm run dev -- --hostname 127.0.0.1 --port 3000
```

Playwright must use that container and working directory, and `PLAYWRIGHT_MANAGE_SERVER` must remain unset so it does not create a second server. The Playwright invocations below are exact retained commands.

Full focused rerun:

```bash
docker exec -w /workspace/admin-web \
  -e PLAYWRIGHT_BASE_URL=http://127.0.0.1:3000 \
  ag-button-fullsite-webtools \
  ./node_modules/.bin/playwright test \
  tests/e2e/d39-operation-task-links.spec.ts \
  --project=chromium --workers=1
```

If the full run again leaves only the re-enrichment case unresolved, the exact retained targeted invocation is:

```bash
docker exec -w /workspace/admin-web \
  -e PLAYWRIGHT_BASE_URL=http://127.0.0.1:3000 \
  ag-button-fullsite-webtools \
  ./node_modules/.bin/playwright test \
  tests/e2e/d39-operation-task-links.spec.ts \
  --project=chromium --workers=1 \
  --grep 'Re-enrich creators'
```

Preserve output under a new R3 log name and record its SHA-256. The earlier R1/R2 commands were identical to the commands above; their host output targets were `frontend-green-r1.log` and `frontend-green-reenrich-r2.log` respectively.

## Remaining gates

1. Complete the current 150-second frontend fixture revision, preferably the full five-case run; a targeted re-enrichment pass can supplement but must not relabel the existing 4/5 log as 5/5.
2. Build the reviewed immutable frontend/backend candidates and bind their source/image hashes.
3. Execute the root-owned D40 runtime recipe: final-image estimate success, restore-validation failure with observed native exclusive interval, and isolated rolling old-RQ-metadata consumption. Require Redis lease and native-lock release evidence on both terminal paths.

With those gates left explicit, the D39/D40 source is ready to advance.
