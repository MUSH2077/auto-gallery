# Task 4 backend contracts — implementation report

BASE: `495cf00ad2667ead7d68e793c667cb734aa82feb`. Implementation and this report are committed together on `fix/scheduler-actions`; the delivery message supplies the commit hash. Status: implemented and locally verified, awaiting root's independent review. No frontend behavior/dependencies/generated exports, frozen release source, acceptance resources, or production were changed. No subagents were used.

## Result and consumer contracts

- One actor-aware `services/task_actions.py` policy plus set-based fact loading supplies `available_actions` and stable `disabled_reasons`. Domain status, active children, artifact/execution ownership, current membership/account/provider eligibility, registry/dispatch validity and persisted dedup-scan prerequisites are facts, not independent frontend guesses. Download/import lists and details, unified task list/detail/search/attention, discovery scans, administrator operation reads and dashboard recent jobs use it. Mutations recheck it; GET capabilities are advisory. `DownloadJobRead.retryable` is now a compatibility projection of `available_actions`.
- Domain retry is limited to failed/stale work and preserves conflict/import checkpoint refusal reasons. Completed downloads return 409 `invalid_task_action`, reason `completed_sync_requires_repeat`; completed imports and cancelled domain tasks do not retry. Registered ordinary admin failed/stale/cancelled retries retain registry scope, execution lease and attempt fencing; legacy/malformed dispatches remain non-executable. Global batch aggregates offer their actual cancel control and preserve terminal results. A visible orphan TaskRun can still acknowledge its anomaly without invented execution actions.
- Actor visibility precedes policy serialization/control. Generic task access still requires tasks, except the existing system-only global batch surface. Direct `/admin/operations` retains its system permission and each operation's owning module; it does not acquire a new tasks-only prerequisite. Private task visibility also applies there. Workbench recent jobs are actor-filtered and its 10-second cache is keyed by actor/permissions, preventing a cached administrator recent-job list from crossing into another actor's response. Existing global health/count aggregates remain global.
- `POST /api/v1/download-jobs/{job_id}/repeat-sync` and `POST /api/v1/tasks/{task_id}/repeat-sync` accept `{request_id: UUID}` and return 202 `RepeatSyncAccepted`: `{task_id, job_id, previous_job_id, request_id, action: "repeat_sync", status: "enqueued"}`. These are acceptance identities, not a live-status snapshot on replay. A new DownloadJob/TaskRun/ordinary dispatch is created through current acting membership/account selection and existing source/resource admission. No stored private credential replay and no old batch parent attachment. The original job and receipt remain unchanged.
- Migration `fb13c5d7e9a1` (after `fa02b4c6d8e0`) adds `download_repeat_intents`, unique `(actor_user_id, request_id)`. Original job/task and accepted job/task IDs are scalar historical identities, deliberately without deletion cascades. Request advisory lock precedes source admission; receipt, new domain rows and outbox commit in the same transaction. The same actor/request returns the same identity after completion or operational compaction; conflicting original identity returns 409. Different simultaneous intents retain source single-flight and return 409 `repeat_sync_not_admitted` with existing-job reference. Best-effort ordinary publication follows commit under the existing short Redis budget; transport failure leaves the durable dispatch recoverable.
- Unified `DELETE /tasks/{task_id}` and existing domain deletion use settled-history deletion. Active/paused work must cancel first. Fresh artifact/parent/child/TaskRun locks, live execution tokens, artifact leases and one bounded Redis liveness pipeline protect deletion. Redis uncertainty refuses with `liveness_unknown`; retained STARTED/heartbeat refuses with `execution_unsettled`. A matching existing RepositorySyncReceipt is retained byte-for-byte in its statistics/timestamps rather than rebuilt from compacted detail; a missing receipt is materialized before deletion, and a conflicting receipt status refuses with `receipt_state_conflict`. History deletion does not remove library works/assets.
- Pause/resume/cancel acquire parent then child locks; retry acquires artifact locks before parent/children because it resets assignment rows. This preserves the existing scheduler cleanup parent/child order and import lifecycle order. Publication/attempt guards remain in existing dispatch services. Batch selection counts owned matches before mutation, rejects over the retained 10,000 cap with `{code: "batch_limit_exceeded", total_matched, limit}`, and bounds its second selection too. Explicit empty IDs match nothing. Each reported successful batch item commits before a later item can fail; per-item structured refusals remain in `errors`. `/download-jobs/clear` now includes that result alongside compatible `status`/`deleted` fields.
- Task attention items keep execution `available_actions` separate from `navigation_actions: ["open_repository", "copy_diagnostics"]`. Source anomaly navigation is unchanged and remains a separate item type.
- `GET /api/v1/tags/page`: default limit100, max200, offset>=0; literal q, category, `sort_by=name|usage_count`, `sort_order=asc|desc`; typed `{items,total,offset,limit,next_offset}` with stable name/UUID ties. Name pages aggregate direct WorkTag usage only for page IDs; usage ranking uses a grouped relation. Per-provider distinct-work composition remains separate from direct usage_count. Old bare-list/include_all and detail routes remain compatible.
- Scheduler decisions add literal substring q, `state=all|due|manual|disabled`, real offset/limit (max500), exact matching total/next_offset, and typed global `summary: {blocked_count, overdue_count, oldest_overdue_at}`. Existing view/all/attention, selected subscription IDs, calendar/DST calculations and global suppressed_count semantics are retained. q matches existing creator/subscription/source/URL fields plus source creator ID/source UUID. Filters are applied before page retention; 809-source tail and attention traversal are verified.
- D20 is fixed structurally: normal SearchService task queries apply exact `TaskRun.operation_type == operation_type`; free q remains independent. Include-account and system-only list branches preserve exact type filtering too. Cleanup subtype no longer leaks into batch type results.

Typed OpenAPI response models now include TaskRead/TaskPage, ImportJobRead/ImportJobPage, AdminOperationRead/AdminOperationPage, WorkbenchSummary/WorkbenchRecentJob, RepeatSyncRequest/Accepted, TagPage and SchedulerDecisionPage. Task5 must regenerate final reviewed exports and consume capabilities, explicit repeat request identities, structured bulk refusals, navigation_actions, paged tags/plans/attention and global summary fields. Subscription source-card consumers must follow next_offset rather than assume their first500 decision rows are complete.

## Verification and exact evidence

All tests ran in `ag-button-runner`, cwd `/workspace/backend`, isolated PostgreSQL `agbutton_test` / Redis15. Existing test setup/isolation/durability guards were retained. External evidence root: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-task4/` (runner `/evidence/backend-task4/`). No acceptance DB/Redis or worker was used. Commands below have the prefix `docker exec ag-button-runner python -m pytest`; `T` below is exactly `tests/test_task4_contracts.py`. Output is preserved in the named file; filenames containing green are historical attempt labels, not claims that failed runs passed.

| Exact pytest arguments after prefix | Evidence | Result |
| --- | --- | --- |
| `T -q` (initial 3 tests) | reads-red.txt | 3 failed, 53.79s; genuine exact-type leak and missing page route; scheduler fixture import needed correction |
| `T -q -k "scheduler_decision or domain_and or repeat_sync or active_delete"` | actions-plans-red.txt | 4 failed, 2 deselected, 68.00s; genuine missing scheduler page fields; action membership fixture FK corrected |
| `T -q -k "domain_and or repeat_sync or active_delete"` | actions-red.txt | 3 failed, 3 deselected, 37.03s; missing capabilities, repeat404 and active delete200 |
| `T -q -k "operation_type or tags_pages or scheduler_decision"` | reads-green.txt | 3 passed, 3 deselected, 33.90s |
| `T -q -k "domain_and or repeat_sync or active_delete"` | actions-green.txt | 2 passed, 1 fixture failure, 29.40s; corrected finalization fixture's expired updated_at access |
| `T -q -k all_filtered` | bulk-red.txt | 1 failed, 6 deselected, 9.51s; all3 mutated instead of cap2 refusal |
| `T -q -k 'domain_and or repeat_sync or active_delete or all_filtered'` | actions-bulk-green.txt | 4 passed, 3 deselected, 70.81s |
| `T -q -k 'workbench_recent or repeat_policy'` | additional-red.txt | 2 failed, 7 deselected, 33.11s; missing workbench capabilities/inactive subscription repeat advertisement |
| `T -q -k 'workbench_recent or repeat_policy or control_rechecks or terminal_delete'` | settlement-green.txt | 3 passed, 1 fixture failure, 59.60s; corrected invalid downloading→complete fixture transition to importing→complete |
| `T -q -k action_authorization` | control-lock-red.txt | 1 failed, 11 deselected, 15.23s; independent session could obtain domain row lock during authorized control |
| `T -q -k 'control_rechecks or action_authorization or repeat_concurrent or explicit_empty'` | races-green.txt | 3 passed, 1 fixture assertion failure, 55.67s; ordinary download outbox key is state, not admin publication_state |
| `T -q -k 'fast_retry or registered_admin'` | fast-worker-red.txt | 1 passed, 1 assertion failure, 28.16s; retained generic retry HTTP200 corrected in test (direct admin retry remains202) |
| `T -q` (18 tests) | contracts-green.txt | 16 passed, 2 fixture failures, 201.19s; missing WorkSourceTag.source and legacy OpenAPI redirect corrected |
| `T -q -k 'bulk_partial or delegated_import or live_progress'` | bulk-progress-red.txt | 1 passed, 2 failed, 42.47s; earlier bulk success rolled back; cached Redis progress flushed into PG |
| `T -q -k 'direct_admin_surface or bulk_partial or live_progress or tags_pages or openapi'` | amended-contracts.txt | 4 passed, 1 genuine direct-admin permission regression, 50.93s |
| `T -q -k 'direct_admin_surface or registered_admin or delegated_import or terminal_delete or action_authorization'` | policy-final.txt | 5 passed, 17 deselected, 45.81s |
| `T -q -k malformed_registered` | malformed-red.txt | 1 failed, 22 deselected, 14.12s; malformed options caused serializer AttributeError |
| `T -q -k 'malformed_registered or tags_pages or scheduler_decision or openapi or direct_admin_surface'` | read-permission-final.txt | 4 passed, 1 fixture cleanup failure, 51.74s; standalone WorkSource rows from prior fixture fixed by truncating works too |
| covering command below | covering-regressions.txt | 28 passed, 1 old exception-contract assertion failure, 228.27s |
| `T -q -k 'history_delete_keeps or bulk_partial'` | history-clear-red.txt | 2 failed, 22 deselected, 44.52s; receipt7→0 and clear hid per-item refusal |
| `T -q -k visible_orphan` | orphan-policy-red.txt | 1 failed, 24 deselected, 13.40s; missing domain incorrectly prevented acknowledging visible anomaly |
| `-q tests/test_queue_routing_scheduler.py::test_unsafe_staging_conflict_cannot_be_blindly_retried T -k 'unsafe_staging_conflict or history_delete_keeps or bulk_partial or visible_orphan or terminal_delete'` | history-policy-final.txt | 5 passed, 21 deselected, 54.49s |

The selected existing-regression command was:

```sh
docker exec ag-button-runner python -m pytest -q tests/test_task4_contracts.py::test_tags_pages_are_complete_bounded_and_count_queries_are_grouped tests/test_queue_routing_scheduler.py tests/test_tags_api.py tests/test_workbench_api.py tests/test_scheduler_contract.py::test_weekly_calendar_runs_selected_weekdays_and_multiple_times tests/test_scheduler_contract.py::test_monthly_calendar_clamps_31st_to_month_end_and_deduplicates tests/test_scheduler_contract.py::test_calendar_skips_nonexistent_dst_wall_time tests/test_scheduler_contract.py::test_scheduler_disabled_is_global_suppression_not_one_attention_per_source tests/test_tasks.py::test_pause_failed_task_is_rejected_as_a_structured_conflict tests/test_tasks.py::test_invalid_admin_retry_state_has_structured_conflict_detail tests/test_admin_slow_operations.py::test_tasks_permission_cannot_read_list_or_retry_system_operation tests/test_admin_operation_dispatch.py::test_registered_dedup_retry_resets_failed_scan_before_new_attempt tests/test_import_lifecycle_batches.py::test_retry_resets_only_child_assignment_reopens_parent_and_aggregates tests/test_import_lifecycle_batches.py::test_concurrent_recovery_and_retry_serialize_attempt_rotation tests/test_operation_attention.py::test_import_pause_resume_cancel_updates_parent_and_both_task_runs_atomically tests/test_import_finalization_recovery.py::test_projection_generation_commits_after_opposite_task_lock_order tests/test_import_finalization_recovery.py::test_retry_after_committed_import_finalization_preserves_outcome_without_reimport tests/test_scheduler_batches.py::test_cancel_preserves_completion_between_reconcile_and_domain_lock tests/test_scheduler_batches.py::test_retained_started_batch_recovers_crash_binding_atomically tests/test_scheduler_batches.py::test_retained_started_cleanup_recovers_counts_and_completed_receipts
```

The sole covering failure was an older unit expectation of TaskEngineError instead of the shared structured409 for unresolved staging conflict. Its expected refusal contract was updated and passed in history-policy-final.txt; no passing unrelated suite was repeated. The existing failed-task control test now supplies a real owning actor before expecting a policy refusal, and existing workbench/scheduler extraction tests retain their intent with the new actor/decision seam.

Final coverage is **25 distinct new contract cases + 28 distinct existing selected cases, all passed in their covering runs** (not a claimed single all-green whole-suite run). Real checks include HTTP permission/visibility, registered retry execution, concurrent identical/different repeat intents and source single-flight, durable publication loss, compaction replay, unchanged original receipt despite new failure, parent/import delegation, live execution/heartbeat deletion refusal, two-session domain locking and completion-before-control, malformed/legacy read-only dispatch, per-item bulk durability, exact operation filtering, page traversal and source-usage semantics. The migration upgrade/schema/unique-constraint/downgrade was exercised on real PostgreSQL inside a rolled-back test transaction. No completed import/chunk was reset to obtain passing results.

Final `docker exec ag-button-runner python -m ruff check` over every changed/new backend Python file: `All checks passed!` (ruff-final.txt). `git diff --check` passed. Final import cleanup/docstring edits do not change execution behavior.

## Performance evidence and limits

- `tag-query-plans.json` contains actual `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for the new page queries over601 tags/200 works/2000 direct associations. The page-scoped grouped usage query returned10 used-tag groups in0.399ms; unfiltered grouped usage ranking returned5 rows in0.617ms. Neither plan contains a correlated SubPlan. This is fixture evidence, not a production latency guarantee.
- `tag-http-bounds.json`: four name pages, rows200/200/200/1, approximately34.7KB maximum response; elapsed0.113–0.372s. Zero-use tail tag remains reachable. Two same-provider WorkSources for one Work count once in provider composition while direct usage remains200.
- `scheduler-http-bounds.json`:809 sources fully traversed in five pages200/200/200/200/9, maximum approximately176.7KB; elapsed0.136–0.363s. Literal `%_` tail search, computed attention traversal, disabled filter and due totals passed.
- Scheduler item selection for all/all uses SQL count/page. Exact global suppression/summary calculations require one additional **O(N)** streamed pass, even on an all/all page; computed state/attention filters use bounded200-row streaming and exact counting as well. Retained response memory is bounded, but this does not claim constant CPU over an arbitrarily large library. Summary/suppression are global to selected subscriptions, independent of q/view/state/page.
- GET deletion capabilities omit per-row Redis calls. A later real liveness/receipt/lease conflict can therefore refuse a previously offered action with an explicit reason. Uncertain executor settlement is conservative; it does not silently remove a running worker's identity.
- Bulk selection remains bounded at the retained10,000 cap. Work below that cap uses existing synchronous per-item actions, not a new background bulk runtime. Oversized scope is explicitly rejected and selection retained by the consumer. The separate durable scheduler batch runtime remains MAX25/20s with its reviewed backoffs and fences unchanged.

No unresolved implementation blocker is known. Root's independent review and Task5 generated-client/UI adoption remain pending; this report is not full-site browser acceptance or production deployment approval.

## Scoped files

- `backend/alembic/versions/fb13c5d7e9a1_download_repeat_intents.py`
- `backend/app/api/admin/data.py`
- `backend/app/api/discovery.py`
- `backend/app/api/download_jobs.py`
- `backend/app/api/import_jobs.py`
- `backend/app/api/system.py`
- `backend/app/api/tags.py`
- `backend/app/api/tasks.py`
- `backend/app/models/__init__.py`
- `backend/app/models/download_repeat.py`
- `backend/app/repositories/tag.py`
- `backend/app/schemas/admin_operations.py`
- `backend/app/schemas/download_job.py`
- `backend/app/schemas/import_job.py`
- `backend/app/schemas/scheduler_decisions.py`
- `backend/app/schemas/tag.py`
- `backend/app/schemas/task_actions.py`
- `backend/app/services/download.py`
- `backend/app/services/download_repeat.py`
- `backend/app/services/operation_attention.py`
- `backend/app/services/scheduler_decisions.py`
- `backend/app/services/search.py`
- `backend/app/services/subscription_enqueue.py`
- `backend/app/services/task_actions.py`
- `backend/app/services/task_bulk.py`
- `backend/app/services/task_engine.py`
- `backend/app/services/task_history.py`
- `backend/app/services/tasks.py`
- `backend/tests/test_queue_routing_scheduler.py`
- `backend/tests/test_scheduler_contract.py`
- `backend/tests/test_task4_contracts.py`
- `backend/tests/test_tasks.py`
- `backend/tests/test_workbench_api.py`

## Review fix round 1 (BASE a8e53901)

The two Important findings in `task-4-review.md` and root's explicit Task5 bulk-schema follow-up are addressed in this scoped round. The Task5 handoff/preflight brief was read; no frontend or generated files are changed.

- Private member `subscription-sync-batch` aggregates are classified by the existing durable global/private predicate before applying the registry's system permission. The generic endpoint still checks actor visibility, including private legacy scope without an explicit owner. The policy loader retains ordinary task permissions for a visible private aggregate. This restores owner detail and open/resolved acknowledgement without adding execution capabilities: private aggregate retry/cancel still refuse `batch_results_preserved`. Actual global batches retain system/admin access; ordinary registered operations retain their own module permissions.
- `GET /api/v1/operations/overview` and `GET /api/v1/tasks/anomalies` share `OperationsOverview`. Its `items` schema discriminates on `type`, with `TaskAttentionItem` inheriting TaskCapabilities and nesting TaskRead; executable actions/reasons and separate navigation actions are explicit. `RepositoryAttentionItem` retains its existing navigation-only `available_actions`, null task/task_id, and absence of task capability/navigation fields. The runtime feed implementation is unchanged.
- Root additionally authorized closing the existing per-item bulk OpenAPI gap. Download `/batch` and `/batch-by-filter` and import `/batch-by-filter` use `TaskBulkResult`; download `/retry-all` uses `TaskBulkStatusResult`, and `/clear` uses `TaskBulkClearResult`. The shared result declares action, task_type, filters, total_matched, succeeded, failed and `errors: TaskBulkError[]`. Each error has the actual UUID and `error: TaskActionRefusal | string`. The refusal exposes code/action/reason and optional status/capability snapshot; short final liveness/receipt refusals are preserved through `response_model_exclude_unset=True`, without inserting empty capability fields. Status/deleted extensions retain their existing meaning. No bulk selection, execution or transaction behavior changes.

New regressions use real application HTTP routes and isolated PostgreSQL, with actual Redis publication/liveness where relevant. Private fixtures carry the exact producer provenance (owning membership, owner, subscription scope metadata, downloads queue). Mixed attention responses contain a real failed TaskRun and unhealthy actor source binding. Bulk tests exercise one committed success plus one structured refusal through each of the five routes, including real retry outbox publication; an additional real heartbeat case checks the shorter refusal shape, and a schema serialization check preserves ordinary string errors. OpenAPI assertions inspect the actual two discriminated variants on both feeds and typed result/error schemas on all five bulk routes. Existing relevant admin, orphan, attention and partial-commit regressions are selected separately from unchanged passing suites.

Raw evidence remains external at `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-task4/` (runner `/evidence/backend-task4/`). All commands below run from this worktree; pytest runs only in `ag-button-runner` / `agbutton_test` / Redis15.

```sh
docker exec ag-button-runner python -m pytest tests/test_task4_contracts.py -q -k 'private_member_batch or mixed_attention or bulk_partial_http' > /volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-task4/review-round1-red.txt 2>&1
```

Initial RED: **8 failed, 25 deselected in 94.19s**, exit1. Five failures establish the absent real schemas (generic fallback has no typed properties). Two private cases initially used the wrong registered queue/scope, and retry-all initially used a non-persisted conflict attribute; these fixture defects were corrected before implementation. A shell invocation attempted the fixture amendment using unavailable host `python` (host requires `python3`) and started the same three-case selection before the amendment; the Docker client was interrupted, its `review-round1-red-fixtures-corrected.txt` is empty, and no pytest result is claimed for it. The runner had no remaining pytest process before the corrected invocation below.

```sh
docker exec ag-button-runner python -m pytest tests/test_task4_contracts.py -q -k 'private_member_batch or (bulk_partial_http and download-retry-all)' > /volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-task4/review-round1-red-corrected.txt 2>&1
```

Corrected RED before application edits: **3 failed, 30 deselected in 35.02s**, exit1. Both private-owner cases return no acknowledgement capability instead of `['acknowledge']`; retry-all actually publishes its eligible child and retains the conflict refusal, then fails the missing typed-schema assertion.

```sh
docker exec ag-button-runner python -m pytest tests/test_task4_contracts.py -q -k 'private_member_batch or mixed_attention or bulk_partial_http or bulk_string or direct_admin_surface or registered_admin_policy or visible_orphan or delegated_import_controls or openapi_exports or bulk_partial_refusal' > /volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-task4/review-round1-green.txt 2>&1
docker exec ag-button-runner python -m ruff check app/api/download_jobs.py app/api/import_jobs.py app/api/operations.py app/api/tasks.py app/services/task_actions.py app/schemas/operation_attention.py app/schemas/task_bulk.py tests/test_task4_contracts.py > /volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-task4/review-round1-ruff.txt 2>&1
```

Focused GREEN: **16 passed, 19 deselected in 244.13s**, exit0: ten new cases (two private states, one mixed feed, six real bulk response cases, one string-error serialization case) and six selected existing contract regressions. Ruff: **All checks passed!**, exit0. No application/test source was amended after these checks.

The existing positive system-only global admission/detail/cancel regression is additionally selected to cover the other side of the amended private/global permission boundary:

```sh
docker exec ag-button-runner python -m pytest tests/test_scheduler_batches.py::test_http_permissions_and_system_only_batch_control -q > /volume2/docker/auto-gallery-button-audit-artifacts/20260908/backend-task4/review-round1-global-permission.txt 2>&1
```

Global boundary regression: **1 passed in 14.68s**, exit0. Final `git diff --check` passed. Total covering evidence this round is **17 passing cases**, not a whole-suite claim. Self-review checked the real producer provenance, both permission gates, ordinary admin module checks, actual feed variants, and every current bulk refusal shape (full policy, short final-liveness/receipt, string exception). No outstanding implementation concern is known; independent re-review and subsequent generated frontend adoption remain root's gates.

Scoped round files: `backend/app/api/{tasks,operations,download_jobs,import_jobs}.py`, `backend/app/services/task_actions.py`, new `backend/app/schemas/{operation_attention,task_bulk}.py`, `backend/tests/test_task4_contracts.py`, and this report. Frozen hotfix/acceptance/production resources, frontend source/dependencies/generated types, scheduler dispatch/fences, import lifecycle, search transactions and migrations were not changed. The scoped delivery commit is supplied in the agent's final message.
