# Task 5 R1 frontend implementation report

**Status:** IMPLEMENTED_AND_FOCUSED_FIXTURE_GREEN; independent review, production build, and real acceptance remain pending
**Base:** `eeb46347fe595a0a344a4922a9c0ffff1863e2e7`
**Date:** 2026-09-12
**Scope:** frontend, generated OpenAPI TypeScript, focused tests, bilingual copy, and this report only

## Result

The nine Important and five Minor review findings plus D25, D26, D28, and D29 were addressed in the owned frontend scope. The existing layout, route set, action families, and scheduler 495 intent/409/reload/cancel/loaded-item behavior remain in place.

The implementation now:

- clamps scheduler pages only after a current successful query, while preserving cold, disabled, pending, and failed URLs;
- reconciles selected batches from an immutable submission snapshot and removes only uniquely identified, count-consistent confirmed successes; local refusals, server refusals, duplicate/malformed responses, and transport-uncertain outcomes remain selected with reasons;
- uses one actor/original-download/repeat action intent format across download rows/drawers, task rows/drawers, and dashboard, validates every accepted identity/action/status field, retains recoverable context through reload/compaction, adopts the returned job, and distinguishes existing-job and identity-conflict recovery;
- disables conflicting task/job/attention actions while pending, renders structured backend reasons, and invalidates affected task, download, import, workbench, and attention queries on settlement;
- uses the authoritative scoped blocked summary, marks due/next-due values as loaded-only until complete, and shows an explicit unloaded decision state on source cards;
- persists creator setup failures by actor/creator/link, keeps one link's recovery through another link's success and reload, and retries only repository setup without re-PATCHing the committed verification;
- rejects HTTP-200 JSON backup diagnostics before creating an object URL and preserves archive response filename/type for valid downloads;
- mounts selected/current-filter/all-scope job utilities in the existing jobs layout with tasks/system permission boundaries, confirmation, partial reasons, and cap/error feedback;
- requires successful current-principal resolution before exposing self-delete, marks retained tag data while fetching, aligns a newly created tag's category filter, serializes Gitllery clipboard writes, localizes operations clipboard failure, retains actual mixed creator failures in confirmation, and gates cleanup counts by operation and terminal state;
- preserves valid numeric zero for both retry defaults and Pixiv `sleep_request`;
- serializes works URL updates through the latest optimistic parameter snapshot, so a settling search debounce cannot restore an earlier view/page;
- fixes presence-mounted Modal and command-palette focus lifecycle, long mobile tag containment, and stale search-assist consumption. Command keyboard activation and pointer activation now share the selected Next route path and destination tree.

The authorized OpenAPI export changed only the cleanup endpoint description from removing all metadata to queuing evidence-based cleanup of successfully imported gallery-dl sidecars. Generated TypeScript was regenerated from that exact export.

## Contract and source identity

- Authorized OpenAPI SHA-256: `15a9bfa6f37229562191b1d0c9df722da261a069d7eda52e48334148842fd698`
- Generated `types.generated.ts` SHA-256: `a4abe7fe868ae969f40704d65bd057a8e4728490e03845320d083e4242f86073`
- Assigned tool container: `ag-button-fullsite-webtools`, ID `a00f6a06cfdfd5fcd6d794a2fc92ec53c79e67bb9eda11a9e9db8075a3061f36`
- Toolchain: Node `24.18.1`, Next `16.3.0`, Playwright `1.62.1`, TypeScript `5.7.2`

Installed Next documentation read before implementation:

- `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/04-linking-and-navigating.md`
- `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/05-server-and-client-components.md`
- `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/06-fetching-data.md`
- `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/10-error-handling.md`
- `/workspace/admin-web/node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-router.md`
- `/workspace/admin-web/node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-pathname.md`
- `/workspace/admin-web/node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-search-params.md`

Exact document hashes are recorded in `task-5-r1-resume-preflight.md`.

## Verification

All commands ran in the assigned container against the shared checkout. Browser fixtures intercepted application HTTP API requests and returned 501 for unexpected routes; Playwright used exactly one worker.

### Node contracts

Command:

```text
docker exec -w /workspace/admin-web ag-button-fullsite-webtools node --no-warnings --test tests/test-task-actions.mjs tests/frontend-safety-primitives.test.mjs tests/api-client-contract.mjs tests/task-action-reconciliation.test.mjs
```

Result: **22 passed, 0 failed**. It covers request/auth error preservation, HTTP-200 backup diagnostics, secure HTTP-origin UUID generation, clipboard settlement, capability partitioning, unique fail-closed batch reconciliation, actor-scoped repeat recovery, complete repeat acceptance validation, conflict classification, finite zero, and terminal cleanup presentation.

Log: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/frontend-task5-r1-20260912/node-contracts.log`
SHA-256: `86182c8db73dda492c129c0332ff15c43e5633423b76b66b7086b00099e39fdf`

### Static frontend checks

Commands and results:

```text
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run typecheck
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:i18n
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:api-types
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:admin-routes
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:charts
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:media
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:search-contract
```

All passed. The i18n check reconciled 2,997 bilingual keys; generated types match the authorized OpenAPI export; nine core media surfaces, route contracts, chart contracts, and the search contract passed.

| Log | SHA-256 |
| --- | --- |
| `typecheck.log` | `cf0c31198780c7e7ceeab1648c164d564de3c7ebc74efed5067e7b7ec6a63b40` |
| `check-i18n.log` | `095f9eaa0ae48b56745d11f84122a54b5bba0187f0b7d0a8d8aa93a58f32ca22` |
| `check-api-types.log` | `f963d1d81e151f349a656375709260173d0611eea5a3bb62a2f7d8e4aa8a4f3f` |
| `check-admin-routes.log` | `f24273782a2614f846711dc5d819923ce7cef26d158e12f251b1178e79e58971` |
| `check-charts.log` | `913108cc39bd71c5062ead147b9cd3f575162fd5087d372433e81ebea6be37f8` |
| `check-media.log` | `1b1e9551af2e452fb2fe7daecd37af7d3f0ff76f674bfd534dbdfa6f45538ee9` |
| `check-search-contract.log` | `b3e9e6b7b865d504a9bb47c52b7ff123739350e5af863ccc7f3b8b652bb3edc0` |

The logs are under `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/frontend-task5-r1-20260912/`.

### Focused browser callers

Command:

```text
docker exec -w /workspace/admin-web -e PLAYWRIGHT_MANAGE_SERVER=1 ag-button-fullsite-webtools ./node_modules/.bin/playwright test tests/e2e/task5-r1-regressions.spec.ts tests/e2e/clipboard-action-semantics.spec.ts tests/e2e/backup-action-semantics.spec.ts tests/e2e/creator-action-semantics.spec.ts tests/e2e/task-action-capabilities.spec.ts tests/e2e/dedup-action-semantics.spec.ts tests/e2e/scheduler-actions.spec.ts --project=chromium --workers=1
```

Result: **27 passed, 0 failed in 5.2 minutes**.

Meaningful application scenarios include:

- valid backup bytes/server filename and bearer header, non-2xx delete retention, and an HTTP-200 JSON diagnostic with zero object URLs;
- exact Gitllery clipboard text, serialized pending writes, and localized rejection;
- actual mixed creator merge retaining only the failed remainder, and two-link setup recovery surviving unrelated success plus reload with zero repeated verification PATCH;
- an actual dedup decision caller submitting the selected representative and HTTP-origin UUID;
- scheduler stable intent, fallback UUID, durable reload, real 409 mode adoption, cancel cleanup, loaded-item terminal refresh, tasks-only denial, and cold page 21 preservation with bounded 809-item decisions;
- download-list and dashboard repeat adoption, plus actual jobs batch success/local-refusal/server-refusal reconciliation and authoritative reason retention;
- zero-value save, Chinese mobile long-tag containment and shared Modal focus/Escape/scroll restoration, current-principal failure, filtered tag creation, and rapid works search/view settlement;
- command-palette autofocus, selected Tags keyboard navigation to mounted Tags content, Works pointer navigation to mounted Works content, and Escape focus restoration under delayed assist.

Log: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/frontend-task5-r1-20260912/playwright-focused.log`
SHA-256: `8be5b0dab703cd0ae768abc1a1919431ac6eb361ce83bb5b0ef1689ce72c1e58`

Expected isolated-harness noise in this log is limited to Next's slow-filesystem/module-type warnings and refused WebSocket proxy attempts because the fixture tool container has no backend WebSocket peer. The tested HTTP application routes were intercepted; these messages did not fail a case.

### Retained RED and diagnostic history

Earlier failures were preserved during implementation:

- helper tests first failed because reconciliation and repeat/storage/cleanup primitives were absent;
- the D29 caller first reproduced inactive/stale keyboard ownership, then caught a URL-only native-history attempt because the Tags URL changed while dashboard content remained mounted; the final implementation uses one selected-link path and asserts destination content;
- the D25 caller reproduced `view=list` restoration after a rapid Grid change; the final optimistic current-parameter serialization snapshot passed after debounce settlement;
- a combined intermediate browser run was 23/25: the backup portal menu suffered an animation-time pointer race and the creator test became ambiguous after both page and retained-dialog alerts were correctly present. The backup scenario now uses the real menu keyboard path, and the creator assertion is scoped to the retained dialog. Both pass in the final 27/27 run;
- an expanded two-link fixture initially used an ambiguous Approve locator, and the first actual dedup fixture used stale button/dialog copy. These were fixture defects; corrected actual-caller tests pass.

## Acceptance inventory corrections for root

The external 160-row matrix remains `not_run`; none of these fixture results should mark a real acceptance row complete. Before real acceptance, update its descriptions/expected assertions as follows:

1. **Repeat sync:** identify one intent by actor + original download + `repeat_sync` across list, task row, task drawer, download drawer, and dashboard. Expect 202 acceptance fields `previous_job_id`, `request_id`, `action=repeat_sync`, `status=enqueued`, non-empty `task_id` and `job_id`; cover reload/compaction recovery, an existing-job admission 409, an identity 409, and explicit new intent.
2. **A030 cleanup:** describe `admin-cleanup-metadata-jsons` as evidence-based cleanup of successfully imported gallery-dl sidecars. Successful final counts require the matching operation and terminal completion. Partial failure must retain/render `removed`, `scanned`, `skipped`, `failed`, reasons/errors, and `metadata_cleanup_partial_failure`; running or other-operation result data must not appear as completed cleanup.
3. **Jobs scopes/utilities:** preserve distinct selected, current-filter, and all/status scopes. Include selected mixed/cancel/no-eligible/uncertain outcomes; current-filter cap refusal and partial errors; task-permitted clear-complete, clear-failed/stale, and retry-all; system-permitted kill-stuck; settlement invalidation and retained reasons. Confirmation counts must reflect actual eligibility.

## Limits and pending gates

- This is selected Chromium fixture evidence, not an all-160 or all-52-routes claim. The final run contains meaningful EN desktop, ZH mobile, tasks/system/denied-role, actual-caller, error, cancel, and partial cases, but it does not execute every consumer in every locale/viewport/role.
- Fixtures do not establish real API/DB/Redis/file behavior. The production frontend build, reviewed exact-source image, and real acceptance/mutation matrix belong to root.
- No production or fullsite backend state was written. No backend source, dependencies, migrations, runtime, or container configuration was changed.
- Operations attention and every one of the nine admin task families are wired through shared pending/error/invalidation behavior, but the focused browser set does not independently exercise every family; the 160-row real matrix remains the acceptance authority.
- No build was run in this role because root owns the reviewed-source frontend build.

## Self-review

- `git diff --check` passed.
- The tracked diff is confined to `admin-web/**` and the authorized `docs/api/openapi.json`; this report/preflight are ignored SDD artifacts added explicitly to the scoped commit.
- Backend application code, dependencies, migrations, runtime files, and production files are absent from the diff.
- Final generated-contract hashes match the authorized export and regeneration output stated above.
