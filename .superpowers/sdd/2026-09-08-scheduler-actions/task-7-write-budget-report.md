# Task 7: bounded search HTTP write admission repair

## Outcome

`search_delivery` now keeps GET and HEAD requests on the existing five-second cap while giving mutating requests the remaining 20-second delivery-slice budget minus a three-second durable-completion reserve. Connect and pool waits remain capped at five seconds; HTTPX read and write waits receive the longer bounded request budget. The existing outer `asyncio.timeout` remains authoritative.

A prepared receipt is durably deferred for 30 seconds when fewer than eight seconds remain before its remote intent marker would be created. A final guard in request timeout construction handles budget consumed between that check and the HTTP call: no HTTP is issued, the receipt returns to `prepared`, and only that receipt's marker is eligible for clearing. Once HTTP has been attempted, timeout and unknown-write outcomes remain `ambiguous`, retain their marker, and are never replayed automatically.

Timeout and unknown-write errors now store the exception class plus a nonempty safe reason, including a fallback for blank timeout messages.

## TDD evidence

The inherited RED artifact was captured against the pre-fix implementation, whose single float timeout capped every HTTP method at five seconds:

- Command (sanitized): `pytest -q --junitxml=/evidence/task-7-write-budget-red.xml tests/test_search_delivery.py -k '<eight focused write-budget cases>'`
- Result: 8 cases, 5 failed and 3 passed in 6.335 seconds.
- Expected failures: delayed POST raised `httpx.ReadTimeout`; write timeout remained a float; no-budget DELETE still reached the client; short-budget prepared delivery created a marker; blank `ReadTimeout` stored an empty diagnostic.
- Artifact: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/task-7-write-budget-red.xml`
- SHA-256: `18fe05a4d8faf964b5f68a462e5e596b74d20bb51032accf328921b721427af8`

The expired-deadline boundary was separately observed RED before the final timeout classification:

- Result: 2 parameter cases, 1 failed and 1 passed in 0.646 seconds.
- Artifact: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/task-7-write-budget-expired-red.xml`
- SHA-256: `8bc2c5ee18e45f0428ba80a101bce2dad78348af29ffc5a17547f9864db32af2`

## Fresh GREEN verification

Database URLs were read from the private recovery JSON and passed directly as `docker exec` environment arguments. Their values were not printed. `tests/conftest.py` forced Redis DB 15 and created a randomized `ag_test_*` Meilisearch prefix. The source was the current worktree bind-mounted at `/workspace`; no production container or production data was mutated.

Focused regression command (credential arguments elided):

```text
docker exec -e DATABASE_URL=<private> -e TEST_DATABASE_URL=<private> ag-button-runner \
  sh -lc 'cd /workspace/backend && pytest -q \
  --junitxml=/evidence/task-7-write-budget-final-r3.xml \
  tests/test_search_delivery.py -k "mutating_request_waits_beyond_five_seconds_for_task_uid or mutating_request_reserves_completion_with_short_connect_and_pool or mutating_request_without_completion_budget_is_not_issued or prepared_receipt_with_short_budget_defers_without_http_or_marker or outer_cancellation_after_send_keeps_unknown_write_fenced or blank_write_timeout_records_nonempty_safe_diagnostic or pending_poll_keeps_short_get_timeout_and_known_uid or task_uid_marker_survives_pending_checkpoint_failure"'
```

- Result: 9 passed, 37 deselected in 14.54 seconds.
- Delayed local HTTP POST returned task UID 22329 after 5.825 seconds, proving the mutating read wait exceeds the old five-second cap while staying inside the slice budget.
- Artifact: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/task-7-write-budget-final-r3.xml`
- SHA-256: `f4618f24facd250b95abe469391208b017684d189a966b6b459fff614b1abca3`

Focused compatibility command (credential arguments elided):

```text
docker exec -e DATABASE_URL=<private> -e TEST_DATABASE_URL=<private> ag-button-runner \
  sh -lc 'cd /workspace/backend && pytest -q \
  --junitxml=/evidence/task-7-write-budget-compat-r3.xml \
  tests/test_search_delivery.py::test_http_ambiguity_never_releases_or_replays_unproven_write \
  tests/test_search_delivery.py::test_task_identity_survives_crash_before_sql_response_commit \
  tests/test_search_delivery.py::test_poll_network_error_keeps_remote_reservation_without_attempt_failure \
  tests/test_search_delivery.py::test_cancel_during_marker_fsync_does_not_release_its_caller'
```

- Result: 4 passed in 4.91 seconds.
- Artifact: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/task-7-write-budget-compat-r3.xml`
- SHA-256: `b1d5186bc7abb71737a474c43d076acb5c19bc2e12e4e1cfdb585dc6bb1e2b22`

Static checks:

```text
docker exec ag-button-runner sh -lc 'cd /workspace/backend && ruff check app/services/search_delivery.py tests/test_search_delivery.py'
git diff --check -- backend/app/services/search_delivery.py backend/tests/test_search_delivery.py
```

- Result: Ruff reported `All checks passed!`; `git diff --check` exited 0 with no output.

## Exact boundaries and concerns

- The delivery slice remains 20 seconds and the receipt claim lease remains 30 seconds.
- GET and HEAD timeouts remain at most five seconds.
- Write connect and pool timeouts remain at most five seconds.
- Write read and write timeouts receive `deadline - monotonic_now - 3 seconds`.
- Prepared writes require at least eight seconds before marker creation; otherwise they defer for 30 seconds without issuing HTTP.
- A budget failure detected after creating this receipt's intent marker is treated as definitely not sent, persisted back to `prepared`, and clears the marker through the existing owner-checked `clear_marker(receipt.id)` path.
- Cancellation or any exception after entering the client request preserves the `submitting`/`ambiguous` fence. Known task UIDs are persisted to the marker before the SQL `pending` checkpoint.
- No new settings, endpoints, resource-admission changes, broad refactors, or production mutations were made.
- Per the brief, verification stayed focused; the full 46-test search-delivery module and broader backend suite were not run under current disk-pressure constraints.
