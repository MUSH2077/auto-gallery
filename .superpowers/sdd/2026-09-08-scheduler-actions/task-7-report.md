# Task 7 search settings comparison report

Date: 2026-09-13

Base: `cdce8dbe822693c9eb05cb928b27aa52f0ba1748`

Status: scoped implementation and focused fixture verification complete; production receipt reconciliation, immutable build/rollout, and final acceptance remain root-owned.

## Result

- `_contains_settings` now carries the JSON path through nested settings.
- Only `filterableAttributes`, `sortableAttributes`, `nonSeparatorTokens`, and `typoTolerance.disableOnAttributes` use unordered membership comparison.
- Those four fields require lists containing only strings on both sides. Missing or malformed values reject containment and therefore retain the settings PATCH.
- `searchableAttributes`, `rankingRules`, nested lookalike names, and all unknown arrays remain order-sensitive.
- Comparison uses new sets and does not sort or otherwise mutate either input list.
- The delivery-level regression verifies that reordered equivalent settings proceed directly to the document deletion request, while a membership change or an ordered searchable-attribute change submits the desired settings PATCH.

## Isolated test configuration

All runtime checks used the pre-existing `ag-button-runner` container (`2e97df5556a3b57c1e7f36ad481f4672dd6e19343680d091591170899ed279ea`) on `ag-button-test-20260908`. The runner bind-mounts this worktree at `/workspace`, works from `/workspace/backend`, and reaches only the assigned test services through aliases `postgres:5432`, `redis:6379`, and `meilisearch:7700`.

The existing fixture resolves PostgreSQL to database `agbutton_test`, Redis DB 15, and a per-process `ag_test_<pid>_<nonce>_` Meilisearch index prefix. No production database, Redis database, Meilisearch index, container, or endpoint was used or changed. Environment inspection printed only host, port, and path components; credentials were not printed.

## TDD evidence

Baseline:

```text
docker exec ag-button-runner sh -lc 'python -m pytest -q tests/test_search_delivery.py::test_first_index_settings_are_durable_separate_task'
```

Result: 1 passed in 32.32 seconds. Log: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/task-7-baseline.log`, SHA-256 `e18bbe30f59c42d02823f11b0360068fbdb390b6b0a7340306e266f3711084b2`.

RED before production changes:

```text
python -m pytest -q tests/test_search_delivery.py -k 'reordered_set_like or material_settings or contains_settings'
```

Result: 5 failed and 7 passed in 24.75 seconds. The failures showed the reordered equivalent settings taking `/settings`, three malformed matching values being accepted, and the four valid reordered fields being rejected. A direct rerun of the set-like regression returned pytest exit 1. Log: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/task-7-red.log`, SHA-256 `db31e7bdb9aa62a33377049532bb397b94d68d0e84bfd9b38fa1dbb9cb00f722`.

Focused GREEN after the implementation:

```text
docker exec ag-button-runner python -m pytest -q tests/test_search_delivery.py -k 'reordered_set_like or material_settings or contains_settings' --junitxml=/evidence/task-7-green-focused.xml
```

Result: 12 passed and 23 deselected in 31.79 seconds, exit 0. JUnit log: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/task-7-green-focused.xml`, SHA-256 `ca26a630405a39cf717ac315b7e24018ff881241152ca95101be7eb0b35901c0`.

Static checks:

```text
docker exec ag-button-runner python -m ruff check app/services/search_delivery.py tests/test_search_delivery.py
docker exec ag-button-runner python -m py_compile app/services/search_delivery.py tests/test_search_delivery.py
git diff --check
```

All three exited 0; Ruff reported `All checks passed!`.

## Interrupted broader run

A complete `tests/test_search_delivery.py` run was started before root reported severe host I/O pressure and asked for bounded checks only. Root reported I/O PSI full near 75% and `vmstat` I/O wait of 77–84%. The run was interrupted and no pytest process remained in `ag-button-runner` afterward. Its partial JUnit contains 18 executed cases over 500.798 seconds: 17 passed and the `searchable_order` parameter hit the existing 20-second delivery scheduling deadline while fixture/storage operations were heavily delayed. It is not used as passing evidence and 17 cases were not run. Partial log: `/volume2/docker/auto-gallery-button-audit-artifacts/20260908/task-7-test-search-delivery.xml`, SHA-256 `22ff2bd5fd2eb22089fed8c95d8eeb3c722ab49248e7409a070947cc28332020`.

Per root instruction, no further tests, image builds, container replacement, production calls, or request-timeout changes were attempted.

## Self-review

The final diff was checked against every brief requirement and mentally mutation-tested. Removing any of the four allowed paths makes the integration/immutability regression fail; broadening matching by leaf key makes the nested lookalike case fail; removing type validation makes the malformed cases fail; changing set membership makes reordered or membership-change cases fail; and unordered handling of other arrays makes the searchable, ranking-rule, or unknown-array cases fail.

The implementation is limited to the comparison helper and test support needed to return controlled settings. It does not alter delivery fencing, durable markers, request deadlines, generated API types, D39/D40 files, or production configuration. Duplicate entries are compared by membership because these Meilisearch attribute settings are semantically sets; both sides must still be string lists.

## Acceptance boundary

Root owns reconciliation of the verified production Meilisearch tasks, production backlog recovery, image build and rollout, container cleanup, and final acceptance under normal I/O conditions. The interrupted broader run should be repeated only after root determines the host is healthy enough for it.

## Independent review correction

The read-only review of `b3252a4` found that dictionary-shaped desired values at the four set-like paths entered ordinary dictionary recursion before list validation. Identical malformed dictionaries could therefore return true. The bounded correction checks the full path first, so all four special paths must pass string-list validation before any general dictionary recursion is considered. Normal nested settings still recurse unchanged.

Review RED:

```text
docker exec ag-button-runner python -m pytest -q tests/test_search_delivery.py::test_contains_settings_rejects_missing_or_non_string_lists --junitxml=/evidence/task-7-settings-review-red.xml
```

Result: 2 failed and 4 passed in 1.57 seconds, exit 1. Both failures were the expected matching malformed dictionary cases: top-level `filterableAttributes` and nested `typoTolerance.disableOnAttributes`. JUnit SHA-256: `3c99d34b1f6d4ff0539b464238a692070782a9bed7b432f537ae1401ffb7075c`.

Review GREEN:

```text
docker exec ag-button-runner python -m pytest -q tests/test_search_delivery.py -k 'reordered_set_like or material_settings or contains_settings' --junitxml=/evidence/task-7-settings-review-green.xml
```

Result: 14 passed and 23 deselected in 17.48 seconds, exit 0. JUnit SHA-256: `24791d328eaf9f294941026a7fd6af9385238df4434945102604abaf22bf8c0e`.

The added cases close the review finding without changing request behavior, deadlines, marker ownership, or other delivery state transitions.
