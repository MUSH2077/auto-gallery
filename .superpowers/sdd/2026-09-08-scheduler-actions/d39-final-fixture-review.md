# D39 final fixture-only review

Date: 2026-09-13

Fixture SHA-256: `0996be6cd06e100a22f4f25d8b605cec897dcfb6f896baf6c7104416ad599045`

**Spec verdict: CHANGES REQUIRED. Quality verdict: CHANGES REQUIRED.** The five scenarios assert the required canonical TaskRun versus RQ transport behavior, and the retained R3/R5 evidence is correctly described as fixture-only. One Important mock-boundary defect allows wrong HTTP methods on the read/poll/detail endpoints to pass as handled requests.

This was a read-only review of `review-d39-final-fixture.diff`, `d39-fixture-finish-report.md` including the root R5 append, `d39-d40-readiness-review.md`, the final fixture source, and the preserved R3/R5 result files. I did not rerun Playwright or static checks, edit application source, access an API, or mutate a container/production system. This verdict covers only the frontend fixture gate; it is not runtime or whole-site acceptance.

## Finding

### Important — mocked poll/detail/read routes ignore the HTTP method and can mask a caller regression

`admin-web/tests/e2e/d39-operation-task-links.spec.ts:46-72` captures the request method for the catch-all failure record, but every shared read route is matched only by pathname. Most materially, `/api/v1/tasks/${TASK_ID}` at lines 53-64 will return canonical task detail and record `taskDetailIds` for a `POST`, `PATCH`, or `DELETE`. The operation status handlers likewise match only pathname at lines 102-105, 129-132, 151-153, and 177-179. Page bootstrap reads at lines 51-70, 94-97, 150, and 176 have the same gap.

A concrete trigger is a frontend regression that polls `POST /api/v1/admin/operations/${RQ_JOB_ID}` and opens task detail through `POST /api/v1/tasks/${TASK_ID}`. The current handlers return the expected JSON, append the expected transport/task IDs, and leave `unhandled` empty. All canonical-navigation assertions can therefore pass even though both client contracts use the wrong verbs. This contradicts the requested no-masking-unknown-requests boundary.

Require `method === "GET"` for every poll, task-detail, authentication/bootstrap, and page-data read fixture. Let every path/method mismatch fall through to the existing 501 handler and `unhandled` list. Retain exact `POST` checks for the three action submissions and the WebSocket interception.

## Confirmed behavior

- The fixture uses deliberately distinct values: canonical `TASK_ID` is a UUID and `RQ_JOB_ID` is the attempt-scoped transport identity (`d39-operation-task-links.spec.ts:5-6`).
- Import and creator re-enrichment return both identities, poll the `/admin/operations/${RQ_JOB_ID}` path, require at least one such poll, and navigate to `/admin/jobs?tab=admin&task=${TASK_ID}` before loading exact canonical detail (`:85-120`). The stale re-enrichment section-text locator is gone; the exact `Re-enrich` button locator exercised the intended action in R3.
- Danbooru refresh keeps its historical `job_id`/explicit `rq_job_id` transport identity while exposing `task_id`; its status endpoint is polled with the RQ ID and its task action navigates with the UUID (`:122-146`).
- Restored notification state begins with only the RQ job ID, receives canonical `task_id` from its completed operation status, exposes the returned result message, and then navigates to canonical task detail (`:148-172`). Seeing `done` before the click establishes that the mocked status response was consumed.
- The retained legacy notification receives no task UUID, has no pointer styling, keeps the current URL after click, and makes no canonical task-detail request (`:174-202`).
- WebSocket traffic is closed by the fixture (`:37`), while unmatched HTTP API requests are recorded with method/path/query and receive 501 (`:46-72`). Once method predicates are added to all allowed routes, this is an appropriate fail-closed mock boundary.

## Evidence assessment and limits

The R3 and R5 evidence is internally consistent with the report and retains failure truth rather than relabeling the original combined run:

- R3 result SHA-256 `d1892ea86b08f068a8b67afafd35b0902fd73cc46e70aa124be7eb485793ca43` records `fixture_exit_code: 1` and `exit_code: 1`; the report preserves the four passes plus restored-notification timeout.
- R5 targeted log SHA-256 `9a23a69b634345f4696d26d10c8f398c50c8a625c85553f19d90637d8cc20812` records the previously unresolved restored-notification case passing. Its result JSON SHA-256 is `74043f618c46711ce378819f51112bbbef5803fc9dcef61e878eb337d5fdc8b2`; retained trace archive SHA-256 is `32df4a36d382f4ee972e253ec04ae9725d366786a57b36a71f7350162abfd73f`.
- The frozen 250-file staging manifest SHA-256 remains `2f26a0f37b0f4695f2093279a6056e5eb262074fc3c7d3d0fd2cb8285281df33`, and the report states the worktree/staged fixture hash matches the reviewed `0996be6c...` bytes.
- The four reported static checks passed on that frozen source. I did not repeat them.

All business responses and completion state in these five scenarios are mocked. Their pass evidence proves frontend identity selection, persistence, link construction, and local request behavior only; it does not prove real authorization, enqueue, polling, TaskRun completion, API availability, deployment, or whole-site behavior. The report states that boundary accurately.

## Final method-boundary re-review (R7)

Final fixture SHA-256: `37ee40c1c04b78c9d07830013023f1f16583677f0d11585dee0fe16db249492e`

Method-fix review diff SHA-256: `fee79a930f1cef1622a8e461c298f86e55cd0eea3123e9830609890fb85df2b4`

Finish report SHA-256: `ad6893140b88b6791e093a77b3c85be42f59ee4e3fb77d21fb1e054e364aa7bc`

**Final spec verdict: APPROVED. Final quality verdict: APPROVED.** This verdict supersedes the initial changes-required verdict for the source fixture. The original Important finding is resolved, and the focused re-review found no new defect.

Every previously pathname-only handler now requires the actual HTTP method. Shared authentication, task detail, task list, workbench, scheduler-decision, and operations-overview reads require `GET`; WebSocket ticket and search-assist requests require `POST` (`d39-operation-task-links.spec.ts:51-70`). Data-management bootstrap reads and operation polls require `GET`, while import and creator re-enrichment submissions remain `POST` (`:94-105`). Danbooru submission/polling remains `POST`/`GET` (`:125-132`), and restored and legacy tag-page/poll handlers require `GET` (`:150-153`, `:176-179`). Any wrong method now falls through to the existing method/path/query record and 501 response at lines 71-72. The client implementation and OpenAPI both confirm the less-obvious `POST` contracts for `/api/v1/auth/ws-ticket` and `/api/v1/search/assist`.

The preserved R6 run is a concrete negative control for that boundary: with search assist incorrectly allowed as `GET`, the actual client sent `POST /api/v1/search/assist`; the four cases that exercise task links recorded that request in `unhandled` and failed their empty-list assertion. R6 remains an exit-1 result rather than being relabeled. After the handler was corrected to `POST`, R7 ran the final frozen fixture and recorded all five cases passing in 21.7 seconds with exit code 0. The R7 result JSON SHA-256 is `acd29a5619f557ccf5fb3b5fe64ffdd7198632d01e6d9aedcc39207531b96a1d`, fixture log SHA-256 is `afbee2767b0af4f8d7a9b114b4bf7aefd83bc9d039302e89ae81a84ea9be4608`, and five-trace archive SHA-256 is `5fda3cab4302a42ce2299d11707705a404e9c913afd61d5e9185389ea8af71f8`.

The R7 251-file manifest and independently versioned post-R6 manifest are byte-identical, each SHA-256 `b3e9f12a8d199a59facc7b715844d239cfac1ebb3761c41e6f29971bf32973d3`, and each binds the fixture to the final hash above. The assertions continue to distinguish durable TaskRun UUID navigation from RQ polling, learn the UUID from restored notification status, and prevent a legacy notification without a UUID from linking. The mocked `complete` status is confined to the restored-notification fixture and the finish report continues to label all evidence as fixture-only.

I did not rerun the passing fixture or static suites, edit production/application source, call a live API, or mutate a container. This approval is only the D39 source-fixture gate; it does not establish runtime or whole-site acceptance.
