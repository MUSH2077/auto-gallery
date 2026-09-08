# Task 5 frontend action semantics report

## Status and scope

Implementation checkpoint based on `d21950c80369a8fa9178fc889c97dc11abbdc218`. This wave changes frontend application code, response-fixture tests, and the generated OpenAPI client only. It does not change backend application code, dependencies, lockfiles, the Task 6 runtime, production, or acceptance data.

The implementation is ready for independent source review and a frozen production Next build. Static and executable library contracts pass. Several response-fixture browser cases pass; the final capability/repeat fixtures remain a **production-bundle browser gate** because the isolated Turbopack development server repeatedly aborted navigation before an application/API request. This report does not claim Task 6, full-site, or production acceptance.

## Inputs read

- `admin-web/AGENTS.md`.
- Installed Next 16.3 guides: `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/05-server-and-client-components.md`, `06-fetching-data.md`, `10-error-handling.md`, `/workspace/admin-web/node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-router.md`, and `use-search-params.md`. The captured combined guide hash is `6965b2...` (temporary working copy only).
- `task-4-brief.md`, final `task-4-report.md`, and reviewed Task 4 contracts at `d21950c`.
- `frontend-audit/action-inventory.md` (160 families), `route-matrix.md` (52 routes), `defects-and-test-gaps.md`, `root-followup-hypotheses.md`, and the full-site fixture plan/map/coverage inputs named in `task-5-brief.md`.
- Root's actual prechange evidence: creator cross-group selection at `fullsite-runtime/reports/creator-selection-baseline-r1.json`; A124 upload source/canonical mismatch at `fullsite-runtime/reports/upload-identity-baseline-r1.json` and `final-persistence-r1.json`; scheduler hotfix D21 actual evidence retained by root.

## Contract generation

The reviewed Task 4 contract was exported from `ag-button-runner`, then the frontend types were regenerated:

```sh
docker exec ag-button-runner sh -lc 'cd /workspace/backend && python scripts/export_api_contracts.py'
docker exec ag-button-fullsite-webtools sh -lc 'cd /workspace/admin-web && npm run generate:api-types'
```

The resulting SHA-256 values are:

- `docs/api/openapi.json`: `a81c57726e3327ab6ec89378db62994d81c52ea784542f0aafbe4ecd989b6d6`
- `admin-web/src/lib/api/types.generated.ts`: `8ed7da64074d944cec7df1f4374050034a4a3af1729b2a5575250813144dd7f2`

Raw output is in `frontend-task5/openapi-export.txt`, `api-types-generate.txt`, and `generated-hashes.txt`.

## Implemented behavior

- Shared task action helpers now consume actor-filtered `available_actions` and `disabled_reasons`. Jobs, task/job drawers, dashboard activity, and task/repository attention use the returned capabilities. Repository navigation remains distinct from task execution.
- Single and bulk task controls confirm destructive/repeat actions, submit only eligible rows, consume typed per-item outcomes, retain failed/ineligible selections and reasons, and invalidate committed domains after partial outcomes. Canceling a confirmation sends no mutation.
- Completed downloads use durable repeat-sync intent IDs across retry/reload, validate the full accepted identity, distinguish admission conflict from request identity conflict, and navigate only to returned identities. The dashboard now exposes repeat only when advertised.
- Creator duplicate selection is restricted to one group, excludes its explicit target, shows names in confirmation, and retains per-source HTTP-200 business failures. Link verification and optional repository setup are staged so a committed verification is never reissued blindly after later failure.
- Tags use bounded server paging with global totals and URL state. Scheduler decisions use separate bounded plan/attention pages and server summary fields. Subscription detail follows bounded decision pages with an explicit load-more state rather than silently omitting tail sources.
- Backup deletion retains its dialog/error while pending or failed. Backup bytes use the shared authenticated client, preserve `Content-Disposition`, and use/revoke an object URL.
- All four clipboard consumers await one shared helper, retain source text on failure, surface missing/rejected APIs, and block conflicting clicks while pending. The remaining dedup intent uses the HTTP-origin-safe secure UUID helper.
- Self deletion is hidden while the current principal is unresolved and for the signed-in user, with a stable explanation. Reset-password output stays visible when copying fails.
- A030 cleanup 202 responses are described only as accepted and link to their task. No receipt count is treated as final; generic task detail renders terminal numeric `result_data.removed` as the actual count.
- A037 credential submission keeps the controlled password input during a business/transport failure, while request closures remain outside React Query caches and their temporary credential objects are erased. Success and cancel clear the input. The existing privacy fixture now asserts failure retention and cancel/reopen clearing.
- A124 no longer routes to the upload source UUID. For library-authorized users, “View work” performs the exact public query `q=pid:manual/<source UUID>` with `offset=0&limit=2` and opens a canonical work only when exactly one row is returned. Zero rows use neutral unavailable copy; multiple rows report an identity inconsistency. The upload row/toast says accepted and explicitly says import is unconfirmed. Task-authorized users can open the accepted import/download task.

## Tests and evidence

All evidence below is under `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/frontend-task5/`.

Prechange RED evidence was preserved rather than recreated against mutable acceptance systems:

- Root's real D01 browser/API/PG run proved two cross-group selections and target inclusion before this change, with zero writes.
- Root's real A124 run proved upload source UUID and canonical work UUID differ; the historical source-UUID link returned 404 after the real import completed.
- Root's real A037 run at `fullsite-runtime/reports/account-failure-retention-baseline-r1.json` proved a same-owner duplicate-account 400 left the dialog open but erased the submitted Pixiv credential; the refused operation made no account change and emitted no page error.
- `creator-red-behavior.txt` and earlier Task 5 fixture attempts record the exact frontend defects. Labels containing `green1` are attempt names only where the file itself is not all passing.

Executable shared-contract command:

```sh
docker exec -i ag-button-fullsite-webtools sh -lc \
  'cd /workspace/admin-web && node --test tests/frontend-safety-primitives.test.mjs tests/test-task-actions.mjs tests/api-client-contract.mjs'
```

Result: **12 passed, 0 failed** (`node-contracts-final.txt`). Coverage includes HTTP-origin UUID v4 generation, exact clipboard success/rejection/unavailable behavior, capability partition/reasons, merged JWT/custom headers, structured and plain errors, protected structured 401 cleanup, network/204 handling, and authenticated blob metadata.

Final static command (run after all application edits; dev server stopped to avoid compiler contention):

```sh
docker exec -i ag-button-fullsite-webtools sh -lc \
  'cd /workspace/admin-web && npm run typecheck && npm run check:i18n && npm run check:api-types && npm run check:admin-routes && npm run check:charts'
git diff --check
```

Result: TypeScript passed; i18n passed with **2987 bilingual keys**; generated API types matched OpenAPI; admin routes and chart contracts passed; `git diff --check` passed (`final-static.txt`, `git-diff-check.txt`).

Focused response-fixture browser results from the released isolated tool container:

- Backup failure retention + authenticated binary download: **1/1 passed** (`focused-browser-final1.txt`).
- Bounded tag tail/global total (row 501, request limit 100): **1/1 passed** (`focused-browser-final1.txt`).
- Shared clipboard rejection/no unhandled error: **1/1 passed** (`focused-browser-final1.txt`).
- Creator merge group/target/partial retention: **1/1 passed** after narrowing two ambiguous Playwright selectors (`creator-merge-final4.txt`).
- Verified link + failed repository stage + exact-stage retry without re-PATCH: **1/1 passed** (`creator-browser-final3.txt`).
- Earlier scheduler lifecycle fixture suite remained **5/5 passing** at the hotfix baseline; this wave's paging regression was added but the later dev run was interrupted after navigation stalls, so it remains part of the frozen-build gate.

The combined 21-test attempt was stopped after the server/browser session accumulated navigation failures. It had passed its first three tests, then browser sessions closed or stayed on the loading shell. A fresh capability run after source stabilization and route prewarming still failed both tests at `page.goto` with `net::ERR_ABORTED; maybe frame was detached`, before any application/API request or behavior assertion (`task-browser-final3.txt`, `browser-results/*/trace.zip`). Container evidence at failure: `OOMKilled=false`, PID limit 384, `pids.events max 0`, and memory `oom/oom_kill 0`; `pids.current` returned from 146 to 55 after owned browser processes were stopped. The cause is not proven. These are harness failures, not behavior passes or application failures.

Frozen production-bundle gates for root are therefore: both list/dashboard repeat-sync fixtures, the new scheduler >500/global-summary fixture, A037 failed-input retention, the remaining user/clipboard/dedup route fixtures, and the broader 160-family Task 6 matrix. Response fixtures never update `acceptance_passed`.

## Known backend/acceptance blockers

- A124 remains incomplete for an upload-only principal: such an actor can accept an upload but lacks both task-read and library-read access, so the current public task/canonical resolver cannot expose its durable outcome. This frontend keeps truthful accepted state and does not fabricate a work ID. A backend owner-readable status/identity contract remains required if that role must later open the work.
- A030 backend cleanup safety is unresolved in this wave: `cleanup_metadata_jsons` recursively targets `*.json` beneath the download root. Root owns the serialized backend correction and real active-input/file-preservation test. The frontend correction only removes false completion/count claims.
- Actual Task 6 API/DB/file/browser verification, all roles/languages/viewports, and the 160 action-family acceptance map remain root-owned and pending.

## Files

Scoped changes include the generated OpenAPI/types, shared API/action/clipboard primitives, the creator mapping/duplicate, jobs/dashboard/attention/drawers, scheduler/subscription/tags, backup/user/upload/dedup/data-management/discovery callers, bilingual strings, and focused Node/Playwright fixtures. No dependency or lock file changed.
