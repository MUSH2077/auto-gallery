# Task 5 D34 permission guard implementation report

## Status

Candidate implementation is complete at backend/source base `d6ba0340ece0a336aed2b214e761e5bb3e594572`. It is ready for independent review and the root-owned immutable frontend build. This report does not claim a final build or real-system acceptance result.

The bounded source findings in `task-5-final-permission-source-audit.md` (SHA-256 `995c69e872ab16f3b94971b69258fa87ec3b1b1865642236164191d306919aca`) were verified against their frontend callers and backend route dependencies before implementation.

## Behavior implemented

- `PermissionGuard` now supports an explicit `adminOnly` policy. Users list and detail pages place that guard outside the components that create user queries and mutations, so system, library, and unprivileged principals start zero `/users` requests. Administrator navigation retains list/detail and CRUD controls.
- `usePermissions` exposes the `/me` query error and refetch operation. `PermissionGuard` renders the actual permission-load error with Retry before evaluating permission denial. The existing API client's 401 redirect behavior is unchanged.
- Creator mapping and duplicate pages use an outer `library` guard. Library-only users retain read content but do not receive Add, Approve, Unverify, Retry repository setup, duplicate-selection, or Merge controls.
- Mapping actions use `curation`. Pixiv/Iwara verification and retained repository-setup retries additionally require `subscriptions`; a curation principal without that permission sees a bilingual explicit reason, while pure metadata link verification remains available.
- Repository Sync remains available to `library`, while Enable/Disable now uses the page's existing `subscriptions` capability.
- Gitllery settings remain readable by `system`; Queue verify is mounted only for an administrator, matching the backend's admin-only endpoint.

No backend application, dependency, schema, runtime, production, API, database, or Redis state was changed.

## Files

- `admin-web/src/lib/usePermissions.ts`
- `admin-web/src/components/PermissionGuard.tsx`
- `admin-web/src/app/admin/settings/users/page.tsx`
- `admin-web/src/app/admin/settings/users/[id]/page.tsx`
- `admin-web/src/app/admin/creators/[id]/mapping/page.tsx`
- `admin-web/src/app/admin/creators/duplicates/page.tsx`
- `admin-web/src/app/admin/subscriptions/repositories/[id]/page.tsx`
- `admin-web/src/app/admin/settings/gitllery/page.tsx`
- `admin-web/src/lib/i18n.tsx`
- `admin-web/tests/e2e/user-admin-boundary.spec.ts`
- `admin-web/tests/e2e/permission-action-boundaries.spec.ts`

## Test evidence

All browser requests in these fixture tests were intercepted at `/api/v1/**`; unhandled application requests fail the tests. Playwright ran with one worker in the owned `ag-button-fullsite-webtools` container.

RED evidence:

- `permission-actions-red.log`, SHA-256 `6c75a8f692cb45a828347490467ae479def278ab1217ecbd01d21df8eac3e8f1`: four pre-implementation caller tests failed. It directly demonstrated absent creator guards, enabled remote-link verification without subscriptions, and system-visible Gitllery Verify. Its repository assertion initially stopped at a capitalization mismatch, so it is not used as repository RED proof.
- `repository-red-behavior.log`, SHA-256 `04ca299e12ece6a13155997e024e0bf679068978ad2acd79d938b27570e12cc4`: after correcting only the Sync label literal, the library-only repository caller failed because Disable was still mounted.
- `users-red-behavior-warm.log`, SHA-256 `9154fb5499050117b8c59f14a7ae36d5a1ee2d17c4e47158195ad8fa83020490`: pre-implementation users navigation lacked the administrator boundary and permission lookup errors lacked Retry. The same run also exposed an unrelated missing background-request fixture, corrected before GREEN.
- Earlier server-start timeout and wrong Chromium-path attempts remain preserved as harness failures in `users-red.log` and `users-red-app.log`; neither is treated as behavioral evidence.

Final focused browser command:

```text
docker exec -w /workspace/admin-web -e PLAYWRIGHT_CHROMIUM_EXECUTABLE=/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell ag-button-fullsite-webtools ./node_modules/.bin/playwright test tests/e2e/permission-action-boundaries.spec.ts tests/e2e/user-admin-boundary.spec.ts tests/e2e/task5-r3-regressions.spec.ts tests/e2e/creator-action-semantics.spec.ts --project=chromium --workers=1 --grep 'creator read pages|curation actions remain|creator merge and repository enable|Gitllery verify|permission lookup failure|direct backup route|creator merge keeps|two-link setup failure'
```

Result: **10 passed in 2.2 minutes**. This covers all four new PA-01..04 caller tests, permission-load failure/retry, both existing creator action semantics, and denied/system/admin module-guard behavior. Log: `final-focused-browser.log`, SHA-256 `a5addea58940a193d283da67807381da9407f0b068941f07667c01d1987d8002`.

Users role command:

```text
docker exec -w /workspace/admin-web -e PLAYWRIGHT_CHROMIUM_EXECUTABLE=/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell ag-button-fullsite-webtools ./node_modules/.bin/playwright test tests/e2e/user-admin-boundary.spec.ts --project=chromium --workers=1 --grep 'system, library|administrator still'
```

Result: **2 passed in 1.1 minutes**. It covers system, library, and denied direct list/detail navigation with zero `/users` calls, plus retained administrator list/detail controls. Log: `users-role-green.log`, SHA-256 `18e76a63f90c96b48391b88396b56464a98d3eecf86e8bfe01aedf4365cbfc9d`.

Static checks:

```text
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run typecheck
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:i18n
git diff --check
```

Results: TypeScript passed; i18n passed with 3,001 bilingual keys checked; diff whitespace check passed. Logs: `typecheck.log` SHA-256 `cf0c31198780c7e7ceeab1648c164d564de3c7ebc74efed5067e7b7ec6a63b40`, `i18n-check.log` SHA-256 `cdc0635146bb45a828347490467ae479def278ab1217ecbd01d21df8eac3e8f1`.

## Boundaries and limitations

- The focused tests use deterministic English desktop fixtures. They prove mounted caller behavior and exact protected-request absence/presence; they do not replace the pending whole-site locale/viewport matrix or real-system acceptance.
- No frontend production build was run in this implementation slot. Root owns the exact-source candidate build and actual acceptance.
- The audited backend authorization dependencies were not changed. Frontend guards remain UX boundaries in addition to backend enforcement.
- The temporary fixture Next/Playwright processes and port 13000 listener were stopped; the owned webtools container is released.
