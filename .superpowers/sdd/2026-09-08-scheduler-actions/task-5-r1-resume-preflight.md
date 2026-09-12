# Task 5 R1 Resume Preflight

**Status:** READY_FOR_IMPLEMENTATION_RELEASE
**Prepared:** 2026-09-12
**Mode:** Read-only preparation; implementation and test execution remain blocked until the root agent releases the sole application slot.

## Bound scope

- The binding inputs were read in order: `task-5-r1-brief.md`, `task-5-review.md`, `task-5-r1-preflight.md`, and the relevant portions of `task-5-brief.md`.
- The SDD implementer prompt and `admin-web/AGENTS.md` were read before application source inspection.
- No application, test, generated-contract, dependency, backend, runtime, or production changes were made. No tests, browsers, API calls, database calls, or application-runtime commands were run.
- Existing backend work belongs to another agent and was left untouched.

## Checkout and stable frontend baseline

- Checkout: `/volume2/docker/auto-gallery/.claude/worktrees/scheduler-actions`
- HEAD observed during this preflight: `1a0f5f0fa336b35577f8c7fbcadefbda0c67e8d5` (`fix: preserve download dispatch failure provenance`). This moving shared HEAD is not accepted as the final backend base.
- Reviewed frontend checkpoint: `e81388954bca5cf0f617859d980248bafbbbd8c9`.
- `HEAD:admin-web` and `e81388954bca5cf0f617859d980248bafbbbd8c9:admin-web` both resolved to tree `9f45fff97682b4d9c890341da0cc1b53102975d2`; the scoped frontend diff contained zero paths.
- Current generated-contract hashes remained:
  - `admin-web/openapi.json`: `a81c57726e3327ab6ec89378db62994d81c52ea784542f0aafbe4ecd989b6d6d`
  - `admin-web/src/generated/api/types.gen.ts`: `8ed7da64074d944cec7df1f4374050034a4a3af1729b2a5575250813144dd7f2`

The reviewed frontend defects remain present at the expected seams, including numeric fallback parsing, URL search-parameter updates, repeat-sync flows, presence-backed overlays, cleanup state, and scheduler effects. The 9 Important and 5 Minor findings, plus D25, D26, D28, and D29, therefore remain the complete implementation scope. No new frontend contract blocker was found.

## Assigned installed-document environment

The exact assigned container was inspected through non-secret metadata only:

- Name: `/ag-button-fullsite-webtools`
- Container ID: `a00f6a06cfdfd5fcd6d794a2fc92ec53c79e67bb9eda11a9e9db8075a3061f36`
- Image ID: `sha256:fee853fafa59550d162cef52bca02d907694b44ebf6ef9fb075bcc0c65d8dedb`
- State: running, not restarting, not OOM-killed; observed PID `1700188`, start time `2026-09-08T12:39:48.860348505Z`
- Limits: `0.75` CPU and `2 GiB` memory
- Workspace mount: current checkout to `/workspace` (read-write)
- Installed dependency mount: the isolated `admin-web/node_modules` source to `/workspace/admin-web/node_modules` (read-only)
- Evidence mount: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908` to `/evidence`
- Toolchain: Node `v24.18.1`, Next `16.3.0`, TypeScript `5.7.2`, Playwright `1.62.1`

The requested `rg` executable is absent from this container. The installed documentation was therefore located with a bounded `find`/`grep` fallback inside this same assigned container; no package was installed and the root-owned webtools container was not used.

## Installed Next.js documentation read

| Installed document | SHA-256 |
| --- | --- |
| `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/04-linking-and-navigating.md` | `94e08f7a23ec189935787a8103a1fa27b7adec487e1fbb6f83c123997c0cb45d` |
| `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/05-server-and-client-components.md` | `4f22ddf73c0a2365367ee25f435c15ef2d82b349690462c6f3bd0e44f92d24d2` |
| `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/06-fetching-data.md` | `9933585e9574da41832254cb1d74c41b268099ec4a9091e2e5f7f59494297944` |
| `/workspace/admin-web/node_modules/next/dist/docs/01-app/01-getting-started/10-error-handling.md` | `c8b6a034f078db4c35907e04bae47a7878b247671f58ccad252b88b3f50c3c37` |
| `/workspace/admin-web/node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-router.md` | `974155c3f4fa1a539b1a5fd924faaf49289b9f34742611caeaa3c0955368d0eb` |
| `/workspace/admin-web/node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-pathname.md` | `b319f3ef319a721764ae4ba22ca247bf2fc7313c47bec79a040baa089ef2f99e` |
| `/workspace/admin-web/node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-search-params.md` | `4bc634b7383bec0236d97d61c501ca837f73d2f9b1282cffaecfacd16708df6a` |

Implementation implications:

- Keep browser state, effects, and browser APIs in Client Components without unnecessary Server/Client boundary changes.
- Treat `useSearchParams()` as a read-only current snapshot. Copy the current value before updates, keep callbacks dependent on the current hook value, and choose `replace` versus `push` deliberately so filters, reloads, and back/forward navigation retain one URL source of truth. This directly constrains D25.
- Keep TanStack Query invalidation as the client-data refresh mechanism; `router.refresh()` alone does not invalidate server-side caches.
- Model loading and expected request failures explicitly. Event-handler and asynchronous mutation errors must be caught and rendered because error boundaries do not catch them. This applies to I2, I4, I7, and M3.
- Preserve shared UI and state across client-side transitions. A production build after release remains responsible for detecting any static-rendering/Suspense constraint involving `useSearchParams()`.

## Contract reconciliation for implementation

The implementation must preserve the established layout and every action family. In particular, scheduler behavior must retain stable intent, HTTP-origin secure UUID fallback, real HTTP 409 adoption through `TaskRun.meta.mode`, reload behavior, cancellation, and terminal refresh for loaded items. Repeat-sync must remain actor/original scoped and must preserve the accepted and structured refusal contracts. Generated types remain authoritative; no hand-written duplicate contract types are permitted.

Fixture browser coverage after release must intercept every application request and fail on any unhandled request, with one Playwright worker. The later report must list exact commands and log hashes and may only claim the explicitly exercised matrix, never all 160 acceptance cases.

## Release dependencies

Implementation remains gated on a root-agent follow-up that provides all three items:

1. the final backend base commit;
2. the final generated OpenAPI/type export to consume; and
3. release of the sole application/test slot.

Until that follow-up, this preflight is READY, while implementation, generated-contract refresh, tests, browser work, and acceptance remain pending. Elapsed preparation and document review do not constitute a passing gate.
