# Task 5 R2 frontend implementation report

**Status:** CANDIDATE_AND_FOCUSED_FIXTURE_GREEN; independent review, immutable build, and real whole-site acceptance remain pending
**Base:** `225524f47d80e6644f06f9c3339c9d2716ed23f1`
**Date:** 2026-09-12
**Scope:** frontend application code, focused frontend tests, bilingual copy, and this report only

## Result

The four Important and one Minor findings in the independent R1 review are addressed. A later root source finding on the accepted-versus-completed data-clear flow is also addressed in the same bounded frontend scope.

- Backup downloads accept only the backend's `application/gzip` success media type. Missing, HTML, plain-text, octet-stream, alias-gzip, and JSON diagnostic responses fail before object URL creation; JSON diagnostics retain their useful message.
- Jobs selection is no longer pruned merely because a same-scope refetch removes a row. Transport-uncertain IDs and their reason remain selected. Explicit tab/filter/page scope changes and explicit batch-selection cancellation clear that retained context.
- The full task-compaction preview/apply surface is rendered only for principals with `system`; tasks-only and denied principals do not receive the controls.
- Clear and retry-all retain and render the typed committed totals (`total_matched`, `deleted`/`succeeded`, and `failed`) and structured/string failures. Cancellation submits nothing, pending buttons suppress duplicate submission, and ordinary rejection remains visible.
- Duplicate creator group identity now uses the normalized source association (`reason` plus `description`) instead of ordered full membership. A mixed merge remains associated after the committed source disappears and surviving members reorder, so the failed source stays selected and reviewable.
- An accepted asynchronous admin clear no longer zeros creator/subscription caches before terminal completion. `NotificationCenter` remains the terminal invalidation owner. A rejected enqueue keeps the confirmation dialog and typed phrase available for review/retry; acceptance closes it.

The R1 review report SHA-256 is `f8ebe506ad772aff18c459282b47fbaba321eef5817dff039da51c688827e93f`.

## Verification

All commands ran in the assigned `ag-button-fullsite-webtools` container against `/workspace/admin-web`. Browser HTTP application routes were intercepted, unexpected routes returned 501, and Playwright used one worker.

### Node contracts

```text
docker exec -w /workspace/admin-web ag-button-fullsite-webtools node --no-warnings --test tests/task-action-reconciliation.test.mjs tests/frontend-safety-primitives.test.mjs tests/api-client-contract.mjs tests/test-task-actions.mjs
```

Result: **23 passed, 0 failed**. This includes the exact gzip allowlist and fail-closed duplicate-error-ID batch reconciliation.

Log: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/frontend-task5-r2-20260912/node-contracts.log`
SHA-256: `2608b193cc65385b35360a28fe870fb1212da98853e20e96b920a58e759da1cf`

### Static frontend checks

```text
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run typecheck
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:i18n
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:api-types
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:admin-routes
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:charts
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:media
docker exec -w /workspace/admin-web ag-button-fullsite-webtools npm run check:search-contract
```

All passed. The i18n check reconciled 3,000 bilingual keys; generated API types still match the authorized OpenAPI export.

| Log | SHA-256 |
| --- | --- |
| `typecheck.log` | `cf0c31198780c7e7ceeab1648c164d564de3c7ebc74efed5067e7b7ec6a63b40` |
| `check-i18n.log` | `2504cb1f4b1c20042a68e3cbe37a16d513303d954f2f38cc7056662a20bdad08` |
| `check-api-types.log` | `f963d1d81e151f349a656375709260173d0611eea5a3bb62a2f7d8e4aa8a4f3f` |
| `check-admin-routes.log` | `f24273782a2614f846711dc5d819923ce7cef26d158e12f251b1178e79e58971` |
| `check-charts.log` | `913108cc39bd71c5062ead147b9cd3f575162fd5087d372433e81ebea6be37f8` |
| `check-media.log` | `1b1e9551af2e452fb2fe7daecd37af7d3f0ff76f674bfd534dbdfa6f45538ee9` |
| `check-search-contract.log` | `b3e9e6b7b865d504a9bb47c52b7ff123739350e5af863ccc7f3b8b652bb3edc0` |

### Focused browser callers

```text
docker exec -w /workspace/admin-web -e PLAYWRIGHT_MANAGE_SERVER=1 ag-button-fullsite-webtools ./node_modules/.bin/playwright test tests/e2e/task5-r2-regressions.spec.ts tests/e2e/creator-action-semantics.spec.ts tests/e2e/backup-action-semantics.spec.ts --project=chromium --workers=1
```

Result: **9 passed, 0 failed in 1.8 minutes**.

The focused caller set covers allowed and rejected backup media types with object-URL counts; a real filtered-row removal after a committed-but-lost retry response; explicit tab selection clearing; clear mixed counts/reasons/cancel/pending suppression; retry-all success/rejection; system/tasks/denied compaction roles; failed then accepted admin-clear submission with running and terminal data behavior; a realistic mixed creator refetch that removes the committed source and reorders survivors; and retained two-link setup recovery.

Log: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/frontend-task5-r2-20260912/playwright-focused.log`
SHA-256: `227ccdce6907ecf73ff363dd1b26902ca11bbcd94f79ac4e7a440533c737ac58`

Expected isolated-harness noise is limited to Next slow-filesystem/module-type warnings and the absent backend WebSocket peer. Every tested HTTP application route was intercepted and the tests assert that no unexpected route was used.

## Retained RED evidence

- The MIME contract test initially produced 7 passes and 1 expected failure because missing `Content-Type` was accepted.
- The realistic duplicate fixture initially failed because Gamma Source became unchecked after Beta Source was committed, removed, and the survivors reordered.
- The filtered-row fixture initially lost the `1 selected` state after the retry response was aborted and the refetch removed the row.
- The utility caller initially had no typed committed-count status after the mixed clear response.
- The admin-clear caller initially lost its dialog and typed phrase after the first 503 enqueue rejection.
- The independent R1 source review established the unguarded compaction surface. Its final system/tasks/denied caller now verifies both presence and absence at the actual page.

Intermediate fixture defects were corrected without weakening product assertions: custom route handlers now explicitly report handled requests; the clear button uses its actual label; hidden `<details>` content is opened or queried through its concrete subtree; and operation polling navigation uses mounted Next links so the global provider lifecycle is the behavior under test.

## Inventory corrections and limits

The external 160-row matrix remains `not_run`. None of the fixture cases should mark a real acceptance row complete. Preserve the R1 report's corrections for `repeat_sync`, A030 evidence-based imported-sidecar cleanup, and selected/current-filter/all jobs scopes with clear/retry-all/kill-stuck controls.

A065, A066, A067, A068, and A159 must be recorded as `not_applicable_unmounted`, with source evidence that `OperationsAttentionCenter` is defined but has no application import/mount. Equivalent Jobs row/drawer coverage is not those operations-center rows. No new feature was mounted to satisfy the inaccurate inventory.

This R2 run adds focused English desktop caller evidence on top of the reviewed R1 fixture baseline. It does not repeat or claim all 27 R1 cases, all 52 routes, every locale/viewport/role combination, or any of the 160 real rows. It makes no claim about real API, DB, Redis, or file effects. Root owns independent review, immutable image build, and real acceptance.

Root's later read-only diagnostic identified a separate candidate next defect: direct settings subroutes for backup, dedup, download defaults, gallery-dl, proxy, scheduler defaults, and subscription defaults lack page-level permission guards, and settings-hub reindex failure closes its confirmation. That finding is not silently treated as passing and is outside this completed R2/data-clear scope; it remains for the next reviewed change.

## Self-review

- `git diff --check` passed.
- The diff is confined to `admin-web/**` and this SDD report; no backend, dependency, migration, generated contract, runtime, production, or fullsite fixture file changed.
- The generated OpenAPI artifacts from R1 remain unchanged and `check:api-types` passed.
- The commit is a candidate pending independent review, immutable build, and real whole-site acceptance.
