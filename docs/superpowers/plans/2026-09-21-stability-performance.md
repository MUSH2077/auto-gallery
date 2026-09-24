# Stability and Performance Maintenance

User-approved implementation plan, 2026-09-21. Base: `0f9c29d73745ac982e731ad79fbb419029517d63`.

## Global Constraints

- Keep the product focused on automated acquisition/archive and lightweight multi-device browsing. Do not add RAG, vector storage, a generic plugin framework, Eagle V5 coupling, Immich synchronization, or new DAM features.
- Keep Gitllery in `shadow` throughout implementation and validation. Never consume production projection intents, overwrite `.gitllery`, delete legacy repositories, or switch production to `active` as part of this branch.
- PostgreSQL remains authoritative. Original media and current production data are immutable during development and acceptance.
- Preserve existing public response fields and routes. Additive response fields and asynchronous operation routes are allowed; compatibility aliases remain functional.
- Every behavior change follows RED-GREEN-REFACTOR. Performance claims use repeatable production-build or PostgreSQL evidence, not source inspection.
- Work only in the `codex/stability-performance` isolated worktree. Do not modify the user's dirty master checkout or unrelated open worktrees.

## Task 1: Release identity and actionable authentication health

Add failing backend/API tests proving that disabled, inactive, sync-disabled, and never-checked repositories are excluded from dashboard attention, while an enabled repository with concrete authentication-failure evidence is counted. Test `auth_state` and `credential_state` independently and retain legacy fields. Test that subscription membership recomputation no longer overwrites real authentication health from binding availability.

Implement one shared authentication-health classifier used by the workbench, auth-status API, scheduler/attention surfaces, and repository responses. Treat `auth_status=unhealthy` with a completed check or explicit error as actionable; treat unchecked data as unknown; derive credential readiness separately from provider requirements and usable bindings. Keep `auth_unhealthy_count` as a compatibility alias for actionable failures and add `auth_actionable_count`, `auth_disabled_or_unchecked_count`, and `credential_issue_count`.

Expose an immutable build revision from environment/image metadata through health/system responses and the admin system page. All compose services receive the same value. Add contract and UI tests before implementation.

Expected verification: focused backend tests pass, frontend type/API checks pass, and the current production-shaped fixture reports zero actionable issues for disabled unchecked sources.

## Task 2: Queue and database hot paths

Add failing tests for an outbox readiness API that uses indexed existence semantics, omits Gitllery entirely in shadow mode, and separates cheap readiness from exact health counts. Move the 60-second fallback coordinator to the scheduler process; preserve immediate write-side wakes and worker successor handoff. Cache the exact health snapshot for 30 seconds so HTTP status endpoints never execute global outbox scans.

Add a concurrent migration index for `storage_artifacts(import_job_id)`. Add a regression test proving perceptual-hash band offsets compile as SQL literals matching the five existing expression indexes. Do not add duplicate phash indexes.

Collapse workbench counters into bounded aggregate queries, retain a short shared cache, and provide explicit invalidation after task transitions. Add bounded 30-day cleanup for completed non-Gitllery outboxes (500 rows/transaction); pending, processing, failed, and all Gitllery rows are retained.

Expected verification: focused coordinator, migration, dedup, workbench, retry/idempotency, and cleanup tests pass. `EXPLAIN` fixtures use the phash expression indexes and the storage-artifact foreign-key index. Shadow readiness SQL contains no Gitllery tables.

## Task 3: Frontend bundle, polling, and media performance

Read the installed Next.js 16 documentation relevant to layouts, dynamic imports, images, and package optimization before editing. Add production-build budget tooling and tests first, using client-reference manifests rather than brittle source-text assertions.

Replace `@/components` barrel imports on the login, shell, dashboard, works, jobs, creators, tags, and shared hot paths with direct imports. Separate the login layout from the authenticated admin shell. Dynamically load heavy charts, media viewers/slideshows, task drawers, and large dialogs only when invoked.

Centralize adaptive query intervals: 10 seconds while work is active, 60 seconds while idle, and disabled while the document is hidden. WebSocket task events invalidate workbench/jobs/notifications. Preserve shared query keys so one tab does not duplicate requests.

Ensure thumbnails below the first viewport use lazy loading and async decoding, add private ETag-based caching on authenticated media responses, and apply `content-visibility` to paginated long grids/lists. Do not add virtualization unless a rendered page exceeds 200 rows.

Expected verification: login client-reference JS <250 KiB, common admin shell <350 KiB, and dashboard/works/jobs <550 KiB under the budget script; typecheck and targeted Playwright flows pass with no relevant console errors and idle polling <=4 requests/minute/tab.

## Task 4: Gitllery shadow validation and resumable builds

Add failing status tests proving normal library-wide status uses database state only and performs no filesystem probes. Keep synchronous deep verification only for one repository; library-wide verification is an asynchronous registered operation.

Extend additive status schemas with `unplanned_intents`, `legacy_repositories`, `segment_repositories`, `projection_state`, and `last_verified_at`. Normal status must complete from repository/build/outbox metadata without walking NAS paths.

Use the existing `gitllery_builds` table for asynchronous build and verify operations. Add create/status routes; retain `/build` and `/backfill` as compatibility shims. Capture a high-water commit, select deterministic smallest/median/largest canary repositories, write at most 100 commits per segment, checkpoint cursor/hash/stats, and resume idempotently after restart. Full-build settlement may mark intents at or below the watermark complete only after every affected repository verifies.

Implement a staged, resumable promotion helper but do not execute it: preserve legacy `.gitllery` as `.gitllery.legacy-v0`, then promote the verified generation atomically. Refuse promotion or active projection unless verification and resource gates pass. A failure leaves shadow mode and all data intact.

Expected verification: unit/integration tests cover selection, batching, checkpoint resume, duplicate retry, async verification, failed gates, legacy preservation, compatibility routes, and restore dry-run isolation. No test writes outside a temporary library root.

## Task 5: Integrated verification and handoff

Add repeatable backend query/performance checks for the 70k-intent/100k-asset shape and production-build bundle budgets. Run focused suites during each task, then the complete available backend and frontend contract suites, Ruff, typecheck, production build, API generation drift checks, and targeted Playwright smoke tests.

Run read-only measurements against the current NAS data after the implementation: workbench cache, outbox readiness, dedup candidate query, import-job delete plan, and Gitllery normal status. Do not mutate production or run the full Gitllery build.

Document rollout order, rollback switches, the 24-hour Gitllery incremental gate, and the seven-day soak checklist. The branch is complete only when tests prove no lost/duplicated queue work, compatibility contracts remain valid, all production data is untouched, and any unmet live-duration gate is explicitly reported as pending operational validation rather than claimed complete.

Expected verification: all available automated suites pass or every pre-existing/environment-only failure is named with evidence; `git diff --check` passes; the final reviewer finds no Critical or Important defect.
