# Task 5 D37/D38 guard, action semantics, and palette report

Date: 2026-09-13

Base: `bd522f44d00fcb28edafb4fa6921ab297b21742c`

Status: implementation candidate complete; independent review, image build, and real-system acceptance remain root-owned. This report makes no whole-site or 160-row acceptance claim.

## Result

- Moved request-owning content below hook-free `PermissionGuard` wrappers for 14 cold-navigation routes:
  - `system`: `/admin/data-mgmt`, `/admin/settings`, `/admin/settings/dedup`, `/admin/settings/download-defaults`, `/admin/settings/gallerydl`, `/admin/settings/gitllery`, `/admin/settings/proxy`, `/admin/settings/scheduler-defaults`, `/admin/settings/subscription-defaults`
  - `library`: `/admin/tags`
  - `tasks`: `/admin/notifications`
  - `curation`: `/admin/data-mgmt/dedup`
  - `library` or `subscriptions`: `/admin/search`
  - `subscriptions`: `/admin/upload/danbooru`
- Closed the related global Danbooru recovery leak. `NotificationProvider` waits for authenticated state, a resolved `subscriptions` permission, and matching auth/permission user IDs before recovering or polling a saved batch. Confirmed logout clears only its in-memory job; initial auth loading preserves the saved marker. Authorized recovery still resumes. `usePermissions` now accepts an optional `enabled` input with its existing default preserved, so the login page does not start an unauthenticated permission query.
- Corrected the data-management library rebuild UI to describe the actual endpoint behavior: regenerating `metadata.json`, thumbnails, and related search projections without clearing Meilisearch. The button now opens a bilingual confirmation; cancellation performs no request; failure retains the dialog and error; a 202 response is described as submitted. The response contract includes both IDs, while operation tracking and the Jobs action consistently use durable `task_id`, not the RQ transport `job_id`. The actionable submitted toast is persistent; its ordinary click remains usable after the former 3.5-second default and opens the matching TaskRun drawer.
- Added visible localized proxy-save failure feedback while retaining form edits. The focused caller test covers a network abort, a 422 response, retry, and success.
- Closed D38 from actual frozen-browser evidence (`37dc95ae5de5f986f1b80f3be29c4a5769a744f4842829235c1b0bc9650441d0`): while the palette is open, a document-level Escape owner closes it before deferred input focus transfers from the top-bar trigger. Dialog capture still owns ArrowUp/ArrowDown/Enter and prevents duplicate Escape handling.

## TDD evidence

Browser plugin was unavailable in this session; the task-authorized fallback used Playwright 1.62.1 in owned container `ag-button-fullsite-webtools` (`a00f6a06cfdfd5fcd6d794a2fc92ec53c79e67bb9eda11a9e9db8075a3061f36`), Node 24.18, one worker, loopback origin `http://127.0.0.1:13000`, and fail-closed API routing. No production/backend/API/DB/Redis writes were made.

RED logs:

- Initial eight inner-guard routes and rebuild wording: `task-5-d37-red.log`, SHA-256 `663089e3e719b33b2e3cd8550376d18f8d68b8677d4b97ef7ae662ef5b13e6bb`
- Unauthenticated provider query: `task-5-d37-login-red.log`, SHA-256 `76929a3c134a8435154cfee670a150e0ce9986cc6a9a99082a06f94c8c707296`
- Six sibling settings routes: `task-5-d37-settings-six-red.log`, SHA-256 `a73e76d88d7eafb05df4e6158a6378da11f58adabf95069e5db1ab3d87825fa9`
- Command Palette focus-transfer race: `task-5-d38-red.log`, SHA-256 `92053ae6bb4bc0f34ea89bfe2ac8277f5da36c9bf7c067038e8a60facb437baf`
- Proxy save failure feedback: `task-5-d37-proxy-save-red.log`, SHA-256 `e0e5bdb566919a6f4e4e2c554caaeee54ab88a50840db7f616367c8b74fe9069`
- Cached authorized permissions after account transition: `task-5-d37-account-transition-red2.log`, SHA-256 `e315229efc1d277119853187221ed7c91d037c822fa7eb7781c3fef37d540368`; the denied account caused one additional saved-batch status request.
- Distinct rebuild transport/durable IDs: `task-5-d37-final-gaps-red.log`, SHA-256 `cee532d57e45929bf2edf8af6494c0786c1b982553e8dd796b16a7370e19b5f6`; operation tracking received `admin-<uuid>-attempt-1` instead of the TaskRun UUID.

Final browser evidence used a combined result because root prohibited another broad rerun after only increasing the cold-navigation test timeout:

```text
PLAYWRIGHT_BASE_URL=http://127.0.0.1:13000 PLAYWRIGHT_WORKERS=1 npx playwright test tests/e2e/d37-guard-and-data-action.spec.ts --project=chromium --workers=1 --reporter=list
```

R4 result: 19/20 passed in 10.8 minutes. The sole failed assertion timed out after 15 seconds while following the rebuild Jobs action; trace evidence showed the ordinary click issued the correct RSC request and the destination mounted just after the timeout. App source remained byte-identical afterward. Log SHA-256 `8de2f4642b72b03bfee7b5a7819bfaada3beb14ba92c2a5e81dd3dae0bebb1a0`.

```text
PLAYWRIGHT_BASE_URL=http://127.0.0.1:13000 PLAYWRIGHT_WORKERS=1 npx playwright test tests/e2e/d37-guard-and-data-action.spec.ts --project=chromium --workers=1 --grep "library rebuild" --reporter=list
```

R6 result: 1/1 passed in 57.5 seconds after raising only the assertion timeout to 30 seconds. The test waits four seconds before an ordinary pointer click, then asserts the canonical TaskRun URL, destination drawer content, task-detail request ID, and operation-poll ID. Log SHA-256 `4c6e78470d565cd93043665a8b11200f953f9cacea99a7d53fcf76d9c6413f52`.

The earlier R2 run (`task-5-d37-d38-focused-final-r2.log`) was 18/20 under overlapping fixture processes. R3 was intentionally terminated at 8/20 when the persistent action and delayed-click regression changed the source under test. Neither is used as final evidence.

Static checks:

```text
npm run typecheck
npm run check:i18n
npm run check:admin-routes
```

Results: TypeScript passed (`cf0c31198780c7e7ceeab1648c164d564de3c7ebc74efed5067e7b7ec6a63b40`); i18n passed with 3,012 bilingual keys (`3a8ef43e722916048afe76c498037de87118b387c0ae81b8e2237fbd48f215c5`); admin route contract passed (`f24273782a2614f846711dc5d819923ce7cef26d158e12f251b1178e79e58971`).

## Source evidence

- Final application diff: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/task-5-d37-d38-app-diff-final.patch`, SHA-256 `67a8b6454a81bcc73888ce62aef9b4d572f2042a4bd9026b7a978428383f42c6`
- Focused regression: `admin-web/tests/e2e/d37-guard-and-data-action.spec.ts`, SHA-256 `1d4970fb2b7471461c6953f03cabed2c21bccff2135a79ed9a9696b88091b111`
- Command Palette: SHA-256 `2041e07bb837243fd868fc8c002a4030088c20eede055ec84a74a88840501483`
- Notification provider: SHA-256 `2401dfc8bc9d28c125a8b2cfe9d4b71c83ad4f3c8ec5c8e31ff901b322dda80e`
- Permission hook: SHA-256 `83b730e69290ad1c1c1dab224082608ec1c743d4424ce887f40093035894acd7`
- Data management caller: SHA-256 `27e2038cf45e6679bc425c12419d2c922945a649b305900859951abf05b4f85c`
- Proxy caller: SHA-256 `a6e0ae5757917a176a98f91ee1044b1cea929abc412fdd51481fc1ff7967dd8b`
- API client contract: SHA-256 `156bdf8c60e529c3c0729b80dbcba01d8c690fc3bf064421f4758eb360b2542d`
- Bilingual catalog: SHA-256 `221646d6f5a15e6c717f4b9e813296fcc1612c767faf6d51ca5eed345653e6ec`

Installed Next 16.3 documentation consulted before editing:

- `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/05-server-and-client-components.md`
- `/workspace/admin-web/node_modules/next/dist/docs/01-app/03-api-reference/01-directives/use-client.md`

## Limits

- Fixture tests intercept all app API requests and fail unhandled calls; they do not substitute for root-owned immutable build or real permission/action acceptance.
- The provider change is limited to Danbooru batch recovery. D39/D40 and other historical task-link sites are separately queued and intentionally absent from this commit.
