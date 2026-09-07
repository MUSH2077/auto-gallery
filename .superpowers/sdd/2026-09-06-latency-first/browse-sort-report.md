# Browse index-order correction

Status: Complete; independent review approved with no Critical/Important findings. The original 4dc06f4 benchmark evidence and ignored packaging revision constants remain unchanged. Fresh NAS acceptance is root-owned and still required.

The final 20-pair run exposed avoidable browse work: explicit DESC NULLS LAST on NOT NULL created_at prevented PostgreSQL from using the existing (created_at, id) index order. Root's preserved diagnostic is `.superpowers/latency-acceptance/diagnostics/post-run-candidate-browse.json`; this change does not treat that diagnosis as a passing new timing run.

`_apply_sql_sort` now adds explicit NULL placement only when the selected mapped column is nullable. UUID tie-breaking, reverse direction, nullable NULLS LAST/NULLS FIRST behavior and cursor encoding/seek conditions remain intact. No other query/cache changes or migration were made.

Real PostgreSQL regressions use a connection-local temporary works table copied from the actual test schema, the existing created/updated composite index definitions, 4096 rows with timestamp ties, and EXPLAIN ANALYZE. They require a 50-row page to examine no more than 50 rows without planner overrides or timing thresholds. Exact first/next-page UUID order is checked for created/updated asc/desc and both traversal directions. Nullable title/posted cases verify hand-derived three-page orders, NULL boundaries, backward navigation and exhausted cursors. The obsolete SQL-string assertion was replaced by this behavior coverage; its independent Meili tie-break assertion remains.

## RED/GREEN evidence

All tests ran through the serialized `.superpowers/run-tests` wrapper in ag-latency-runner, with its isolated PostgreSQL/Redis/Meili. No NAS workloads, service changes, production operations or full-suite rerun occurred.

RED command, before application edits:

```text
.superpowers/run-tests tests/test_search_sql_sort.py -q
4 failed, 8 passed in 1.50s
assert examined <= 50
E assert 4096 <= 50
```

Failures were the forward and reverse created-desc/updated-desc cases. Nullable pagination passed already. An earlier fixture setup attempt omitted client-default boolean values required by the real schema; that non-feature failure is retained separately in `browse-sort-fixture-setup.log` and is not counted as RED. Corrected fixture inserts explicitly provide those booleans. The true RED transcript is `browse-sort-red.log`.

Initial GREEN after the minimal implementation: **12 passed in 1.35s** (`browse-sort-green.log`). The final regression run also includes the added tied-boundary next-page assertions:

```text
.superpowers/run-tests tests/test_search_sql_sort.py tests/test_search_algorithms.py tests/test_search.py tests/test_search_language.py tests/test_search_api_contract.py tests/test_source_identity_search.py tests/test_reference_browse.py tests/test_api_contract.py tests/test_nsfw_filter.py -q
115 passed in 44.05s
```

Full focused transcript: `browse-sort-regression.log`.

Serialized runner lint:

```text
python -m ruff check --no-cache app/services/search.py tests/test_search_sql_sort.py tests/test_search_algorithms.py
All checks passed!
```

Transcript: `browse-sort-ruff.log`. `git diff --check` passed. Self-review found no additional application changes needed. Changed paths are the search helper, the new PostgreSQL behavior tests, the existing Meili tie-break test cleanup, and this report. The new test isolates query-planning work; the deployment's full foreground P95 gate still needs the newly sealed candidate's 20-pair rerun.

The turn was interrupted after all checks had finished. On 2026-09-07 the saved logs and unchanged diff were inspected; tests were not repeated. Completed exec sessions were 17122 (fixture setup failure), 65491 (true RED), 50744 (initial GREEN), and 82974 (115-test focused regression). The independent reviewer inspected the code, RED/GREEN and lint evidence, confirmed all callers supply mapped columns, and approved the fix without additional tests or edits.
