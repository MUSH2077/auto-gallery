# Task 5 R3 frontend implementation report

**Status:** CANDIDATE_AND_FOCUSED_FIXTURE_GREEN; independent review, immutable build, and real whole-site acceptance remain pending
**Base:** `03651913203f3b6a8d18533de0e5b4b0280066ab`
**Date:** 2026-09-12
**Scope:** D31 backup route authorization and D32 search-reindex failure retention only

## Result

- The direct `/admin/settings/backup` route now renders an outer `PermissionGuard module="system"`. The existing hook-bearing page is `BackupContent`, so a denied principal never mounts its backup queries or controls. Principals with `system` and administrators retain the existing page and query behavior.
- A rejected search-reindex enqueue keeps the confirmation dialog open and exposes the API error for review and retry. The pending confirm remains disabled and suppresses duplicate submissions. A later accepted retry closes the dialog and reports that reindexing started.

No generic settings-route guard was introduced. This delta does not address or modify the separately diagnosed backend backup-archive issue.

## Verification

All commands ran in the assigned `ag-button-fullsite-webtools` container against `/workspace/admin-web`. The Playwright fixture intercepted every application API request, returned 501 for unexpected requests, and used one worker.

### Focused mounted callers

```text
docker exec -w /workspace/admin-web -e PLAYWRIGHT_MANAGE_SERVER=1 ag-button-fullsite-webtools ./node_modules/.bin/playwright test tests/e2e/task5-r3-regressions.spec.ts --project=chromium --workers=1
```

Result: **4 passed, 0 failed in 44.7 seconds**.

The caller coverage verifies:

- denied direct navigation renders the permission boundary, exposes no Create Backup control, and makes exactly zero `/api/v1/admin/backup*` requests;
- `system` and administrator principals render Backup & Restore and start the expected list/latest/estimate queries;
- the first reindex request is held pending while a forced second click remains suppressed;
- a 503 response keeps the dialog and concrete error visible, and a second 202 response closes it and renders the success message.

Log: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/frontend-task5-r3-20260912/playwright-focused.log`

SHA-256: `2cf5aa0c8f3751e61f502384637c687cec4678001b05c5ffb2cd3c3ede846912`

Expected isolated-harness output is limited to Next's slow-filesystem warning. The focused tests assert that no unexpected application route was used.

### Relevant static checks

```text
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run typecheck
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:i18n
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:admin-routes
```

All passed. TypeScript emitted no diagnostics, i18n validation checked 3,000 bilingual keys, and the admin route contract passed.

## Limits

This is focused fixture evidence for D31 and D32. It does not mark any row in the external 160-row matrix complete and does not claim real API, database, Redis, archive, file, or provider effects. The prior R1 and R2 evidence remains unchanged. Root owns independent review, immutable image build, and real whole-site acceptance.

## Self-review

- The permission boundary is structurally outside `BackupContent`, so denied access prevents hook mounting rather than merely hiding rendered controls.
- The reindex mutation still closes only on acceptance; its rejection path now preserves the same dialog state.
- `git diff --check` passed.
- The diff is confined to the two frontend callers, one focused Playwright file, and this report. No backend, dependency, generated contract, migration, runtime, production, or shared full-site fixture file changed.
