# Task 5 D35 mobile row menu report

## Scope and status

D35 is implemented as a scoped frontend candidate on base `9fa931dc8e7f6517befc7d04b3b5c367dedd7216`. It changes the shared `RowActionMenu` scroll dismissal and adds a mounted backup-page regression. No backend, generated contract, dependency, runtime, API, database, Redis, or production state was changed. Root still owns independent review, image build, and the real backup acceptance rerun.

## Reproduced failure and diagnosis

The preserved real-system evidence was:

- `fullsite-acceptance/backup-r4-failed-run-9fa-20260913.json`, SHA-256 `1e77adf5ada1d51194883458f14ef22c9b405c688c6d473f66014cbf22774d2f`
- `fullsite-acceptance/backup-mobile-menu-diagnostic-public-9fa-20260913.json`, SHA-256 `e3917424b401e47fa8982cbfaacb89b84967e6af82dac6d091a5252144cf5bb5`

A new 390×844 `isMobile`/`hasTouch` fixture reproduced the same failure with a completed estimate, completed backup, existing backup row, ready restore handoff, and pending receipt. Before the component change, `getByRole("menuitem", { name: "删除" }).click()` timed out because no menu remained mounted.

Temporary event and portal instrumentation established the ordering:

1. Playwright brought the trigger into view at `scrollY=544`; its final rect was top `400`, bottom `444`.
2. Pointer down and click opened the menu.
3. About 3 ms after click, the browser delivered the already-queued document scroll event. Its `scrollY` and trigger rect were unchanged from the opening measurement, while `aria-expanded` was already true and the portal was not mounted yet.
4. The capture scroll listener set `open=false`. `usePresence` then briefly added the portal in its closing phase and removed it about 244 ms later.

This confirms the delayed opening scroll event as the close trigger. It was not a parent query refresh or unexplained `usePresence` remount.

## Implementation

`RowActionMenu` records the window scroll position and trigger rectangle whenever it measures the trigger for pointer or keyboard opening. Its scroll listener ignores a document/window scroll only when both values still exactly match that opening measurement. This covers the delayed event which performed no movement after opening.

All existing dismissal paths remain active:

- a document/window scroll with changed viewport or trigger geometry closes the menu;
- nested-container scroll closes it;
- resize closes it;
- outside pointer down closes it;
- Escape closes it and restores trigger focus.

The portal, enter/exit animation, fixed positioning, touch hit areas, item callbacks, and link behavior are unchanged.

## Regression coverage

`admin-web/tests/e2e/row-action-menu-mobile.spec.ts` mounts the actual backup page with every app request intercepted and all unhandled requests returned as `501`. It asserts:

- the mobile trigger scrolls into view, Delete receives an ordinary click, and the real caller opens the filename-specific confirmation dialog;
- cancellation sends no business write;
- outside pointer dismissal still works;
- a real 100 px window scroll still dismisses the menu;
- ArrowDown focuses the menu item and Escape restores trigger focus;
- viewport resize dismisses the menu;
- background WebSocket-ticket traffic is classified separately and every unexpected app request fails the test.

The existing desktop backup semantics test remains in the focused gate and exercises keyboard open, End, Enter, retained delete confirmation on `409`, retry, and native download handling.

## Verification

Fresh combined command:

```text
PLAYWRIGHT_MANAGE_SERVER=1 PLAYWRIGHT_CHROMIUM_EXECUTABLE=/ms-playwright/chromium-1234/chrome-linux64/chrome npx playwright test tests/e2e/row-action-menu-mobile.spec.ts tests/e2e/backup-action-semantics.spec.ts --workers=1 --reporter=line
```

Result: `3 passed (58.9s)`. Log: `fullsite-acceptance/d35-row-menu-regression-r2.log`, SHA-256 `618e717d17511d73ded17e8152f3c6be362a2731895d799c87dcc46316782dba`.

Fresh static commands:

```text
npm run typecheck
npm run check:i18n
```

Both passed; i18n checked 3001 bilingual keys. Log: `fullsite-acceptance/d35-row-menu-static-r2.log`, SHA-256 `c12798aaca698d9c30641f4bfb080505a61baa7866ac168d9ecc47c0c65b51ee`.

Installed Next 16.3 documentation read before editing:

- `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/05-server-and-client-components.md`
- `/workspace/admin-web/node_modules/next/dist/docs/01-app/03-api-reference/01-directives/use-client.md`

The fixture was `ag-button-fullsite-webtools`, container `a00f6a06cfdfd5fcd6d794a2fc92ec53c79e67bb9eda11a9e9db8075a3061f36`, with `/workspace` bound to this checkout. It is idle after verification.

## Limits

Fixture tests establish the caller and shared-menu regression only. They do not accept the preserved real backup run, its database/files, the candidate image, or any row in the external 160-action matrix.
