# Task 5 D36 membership-removal semantics report

## Scope and status

D36 is implemented as a scoped frontend candidate on base `d8b47b090991b12e08d0d7bbcbecafdd3b222078`. It corrects subscription and source-removal wording and behavior in the subscription list, subscription detail, and repository detail callers. It does not add a permanent-delete feature or change backend, generated contracts, dependencies, runtime, API, database, Redis, or production state. Root owns independent review, image build, and real-system subscription acceptance.

## Confirmed contract and root cause

The reviewed backend source has one removal contract for administrators and members:

- `backend/app/api/subscriptions.py:281-328` returns a subscription preview with `mode="soft"` and `can_delete_files=False`, removes the requesting user's membership, returns `delete_files=False`, and rejects `delete_files=true` even for an administrator.
- `backend/app/api/subscriptions.py:386-417` applies the same contract to a source binding in a subscription.
- `backend/app/api/repositories.py:288-332` applies the same contract to the repository-detail source-binding route.

The frontend instead selected permanent-delete or disable/archive copy from `isAdmin`. That described shared-data deletion to administrators even though the server only removes their private membership/binding. It also hid Restore from an administrator's inactive subscription.

## Implementation

- Subscription list, subscription detail, and repository detail now use “Remove from my subscriptions” or “Remove from my sources” for both administrator and member principals.
- Confirmation copy states that the private membership or binding is removed while shared data, works, and files remain. The dialogs continue to render the server preview, including related/shared counts, but never offer file permanent-delete confirmation or file deletion for this contract.
- Success and queued-operation labels use the same membership-removal language.
- Inactive subscriptions retain the existing Restore flow for both roles. Existing role-specific button styling is preserved where it existed; role no longer changes the operation's meaning.
- `HierarchyDeletionDialog` accepts an optional caller message. Existing creator/archive callers retain their original generic permanent/soft message.
- Chinese and English keys were added together. The requested entity labels are `从我的订阅中移除` / `Remove from my subscriptions` and `从我的来源中移除` / `Remove from my sources`.

## Regression coverage

`admin-web/tests/e2e/membership-removal-semantics.spec.ts` mounts the actual three page callers for administrator and member principals. Every internal `/api/v1/**` request is intercepted; an unhandled request returns `501` and fails the case. The seven cases assert:

- list single and batch controls use membership-removal wording for both roles;
- single and batch dialogs show real soft-preview counts and shared-data preservation copy, with no file checkbox, confirmation textbox, or permanent/disable/archive claim;
- Cancel sends no business write;
- confirmed subscription and source removals use the exact private endpoints with `delete_files=false`;
- subscription-detail and repository-detail source binding removals have the same semantics for both roles;
- an inactive administrator subscription still exposes Restore and sends the existing exact activation patch.

The pre-change mounted detail assertion failed because the administrator caller exposed the old permanent-delete title instead of “Remove from my subscriptions.” After the app change, an initial seven-case pass was green. Two subsequent failed artifacts are retained: R3 used an ambiguous text locator for a nested preview label, and R5 added the batch dialog without its POST preview fixture. Both were test-fixture issues; they caused no product-source change. R6 includes exact preview-tile matching and the real batch-preview route shape.

## Verification

Fresh focused command in the owned `ag-button-fullsite-webtools` fixture, using one Playwright worker:

```text
PLAYWRIGHT_BASE_URL=http://127.0.0.1:13000 PLAYWRIGHT_CHROMIUM_EXECUTABLE=/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell ./node_modules/.bin/playwright test tests/e2e/membership-removal-semantics.spec.ts --project=chromium --workers=1 --reporter=line
```

Result: `7 passed (1.4m)`. Log: `fullsite-acceptance/d36-membership-removal-focused-r6.log`, SHA-256 `c31c7ca8b54b80d908d7e10827761ec9f83ea2b624c40013e5b8b4725618588d`.

Fresh static commands:

```text
npm run typecheck
npm run check:i18n
```

Both passed; i18n checked 3009 bilingual keys. Log: `fullsite-acceptance/d36-membership-removal-static-r2.log`, SHA-256 `5bd1c60db2143489b69b3e8041dbbe887bbb91eb7e9653fb8186fa40fb1b6b18`.

Installed Next 16.3 documentation read before editing:

- `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/05-server-and-client-components.md`
- `/workspace/admin-web/node_modules/next/dist/docs/01-app/03-api-reference/01-directives/use-client.md`

## Limits

The focused fixtures establish mounted caller semantics and request shapes. They do not accept the new frontend image, exercise the frozen real backend, or mark any external action-coverage row complete.
