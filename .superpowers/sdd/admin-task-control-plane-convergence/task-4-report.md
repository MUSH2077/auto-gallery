# Task 4 report: forward migration and release validation

## Outcome

Task 4 adds a new forward Alembic repair revision after the published calendar
revision, proves the damaged previous-head state against PostgreSQL, normalizes
whitespace in both subscription and default `schedule_rule.times` arrays, and
preserves the repaired schema/data across downgrade and re-upgrade. The
published `a7c9e1f3b5d7` migration was not edited.

The release gate also reconciles stale pre-registry tests with Task 1's durable
`admin_dispatch` model, fixes two test-environment assumptions and one Compose
acceptance verifier omission, regenerates/checks API contracts and frontend
types, and runs the complete backend/frontend/static/release matrix.

## Migration diagnosis

The published revision `a7c9e1f3b5d7_add_calendar_subscription_rules.py`
introduces `subscriptions.schedule_rule`. A reused database can nevertheless be
stamped at that revision while the physical column is absent. Alembic treats the
revision stamp as authoritative and will not replay the published migration, so
editing that revision would neither repair already-stamped databases nor
preserve immutable migration history.

The new revision `b3d5f7a9c1e4` therefore:

- adds `subscriptions.schedule_rule JSONB` with `IF NOT EXISTS`;
- trims leading/trailing whitespace from every ordered
  `subscriptions.schedule_rule.times` item;
- applies the same ordered normalization to
  `system_settings.subscription_defaults.schedule_rule.times`;
- leaves the column and normalized values intact on downgrade because the
  previous published revision owns the column and discarded padding is not
  reconstructable.

## Strict migration RED / GREEN evidence

The PostgreSQL integration tests were written before the repair revision.

RED against the previous head:

```text
test_forward_calendar_repair_adds_missing_schedule_rule_from_previous_head
expected information_schema data_type "jsonb", received None

test_forward_calendar_repair_normalizes_times_across_round_trip
expected ["03:00", "21:30:00"], received [" 03:00 ", "21:30:00 "]

Combined focused result: 2 failed
```

GREEN after adding `b3d5f7a9c1e4`:

```bash
docker compose run --rm --no-deps -v "$PWD/backend:/app" backend \
  python -m pytest -q \
  tests/test_migrations_idempotent.py::TestMigrationIdempotency::test_forward_calendar_repair_adds_missing_schedule_rule_from_previous_head \
  tests/test_migrations_idempotent.py::TestMigrationIdempotency::test_forward_calendar_repair_normalizes_times_across_round_trip
# 2 passed in 36.60s
```

The normalization test uses literal ordered expectations before downgrade,
after downgrade to `a7c9e1f3b5d7`, and after re-upgrade:

```text
subscription times: ["03:00", "21:30:00"]
default times:      ["05:15:00", "18:45"]
downgraded column:  pg_typeof(schedule_rule) = jsonb
```

Full migration-chain gate:

```bash
docker compose run --rm --no-deps -v "$PWD/backend:/app" backend \
  python -m pytest -q tests/test_migrations_idempotent.py
# 10 passed in 78.71s
```

This covers upgrade twice, full base downgrade/re-upgrade, historical calendar
conversion, the missing-column repair, normalization, downgrade, and round trip.

Calendar baseline gate:

```bash
docker compose run --rm --no-deps -v "$PWD/backend:/app" backend \
  python -m pytest -q \
  tests/test_calendar_schedule_schema.py \
  tests/test_subscription_schedule_consistency.py \
  tests/test_subscription_scheduler_persistence.py \
  tests/test_subscription_reliable_schedule.py \
  tests/test_scheduler_contract.py \
  tests/test_operation_attention.py
# 60 passed in 166.74s
```

## Full-suite regression triage

The first complete backend run used a backend-only bind mount:

```bash
docker compose run --rm --no-deps \
  -v "$PWD/backend:/app" -v "$PWD/scripts:/scripts:ro" \
  backend python -m pytest -q
# 27 failed, 986 passed, 4 skipped in 2361.66s (0:39:21)
```

Classification and focused RED/GREEN resolution:

1. The first true regression was a synchronous test fake for a Task 1 async
   durable enqueue. It failed with `TypeError`; changing only the fake to
   `async def` produced `1 passed in 2.10s` with all observable assertions
   retained.
2. Three portable-deploy failures resolved under the correct full-worktree
   mount. Their project root had incorrectly resolved to `/` in the first
   command; all three passed unchanged with `-v "$PWD:/workspace" -w
   /workspace/backend`.
3. The PSI test inherited Compose's `RESOURCE_GOVERNANCE_MODE=enforce` while
   asserting shadow-mode behavior. Explicitly fixing the mode to `shadow`
   preserved the effective/computed scale and admission assertions and passed
   in `0.89s`.
4. Three import-lifecycle tests patched a lock-release symbol removed by Task
   1. Removing those obsolete patches produced `3 passed in 20.13s`.
5. The remaining legacy retry tests expected Redis to own admission and retry
   authority. They were migrated to observable durable behavior: PostgreSQL
   active-scope conflicts, exact owner ids, monotonic attempts, deterministic
   RQ ids, persisted options, stale-attempt publication no-ops, deferred
   publication metadata, exact same-attempt republish, recovery fencing, and
   real RQ records/worker execution. No production compatibility path was
   restored and no assertion was skipped.

Focused final module gates:

```bash
docker compose run --rm --no-deps -v "$PWD:/workspace" \
  -w /workspace/backend backend \
  python -m pytest -q tests/test_tasks.py
# 27 passed in 178.93s

docker compose run --rm --no-deps -v "$PWD:/workspace" \
  -w /workspace/backend backend \
  python -m pytest -q tests/test_import_lifecycle_batches.py
# 27 passed in 199.13s
```

## Generated API contracts and frontend types

Generation was performed with explicit test-only backend settings:

```bash
SECRET_KEY='<test-only>' ADMIN_PASSWORD='<test-only>' \
DATABASE_URL='postgresql+asyncpg://.../autogallery_test' \
REDIS_URL='redis://127.0.0.1:6379/15' \
MEILI_INDEX_PREFIX='ag_contract_check_' \
backend/.venv/bin/python backend/scripts/export_api_contracts.py

cd admin-web && npm run generate:api-types
```

There was no tracked generated OpenAPI, AsyncAPI, or TypeScript drift. Final
drift checks:

```bash
SECRET_KEY='<test-only>' ADMIN_PASSWORD='<test-only>' \
DATABASE_URL='postgresql+asyncpg://.../autogallery_test' \
REDIS_URL='redis://127.0.0.1:6379/15' \
MEILI_INDEX_PREFIX='ag_contract_check_' \
backend/.venv/bin/python backend/scripts/export_api_contracts.py --check
# passed

cd admin-web && npm run check:api-types
# Generated OpenAPI types match docs/api/openapi.json.
```

An initial local OpenAPI check omitted `MEILI_INDEX_PREFIX` and was correctly
rejected by the test-database isolation guard. The authoritative rerun above
uses the required isolated prefix.

## Release validation matrix

### Backend

```bash
docker compose run --rm --no-deps -v "$PWD:/workspace" \
  -w /workspace/backend backend python -m pytest -q
# 1013 passed, 4 skipped in 2433.38s (0:40:33)

backend/.venv/bin/ruff check \
  backend/app backend/tests backend/scripts \
  backend/worker_entrypoint.py backend/seed_sync.py \
  scripts/verify-compose-resources.py scripts/offline-restore.py
# All checks passed!

backend/.venv/bin/python -m compileall -q \
  backend/app backend/tests backend/scripts \
  backend/worker_entrypoint.py backend/seed_sync.py scripts
# passed
```

### Frontend contracts, types, i18n, and production build

```bash
cd admin-web
npm run check:i18n
# i18n validation passed (2586 bilingual keys checked).
npm run check:charts
# chart contract validation passed.
npm run check:media
# check:media passed (9 core surfaces)
npm run check:search-contract
# search contract validation passed.
npm run check:admin-routes
# Admin route contract check passed.
npm run check:api-types
# Generated OpenAPI types match docs/api/openapi.json.
npm run typecheck
# passed
npm run build
# Next.js production build compiled successfully; TypeScript, page data,
# static generation, and all routes passed (22.11s).

node tests/calendar-schedule-editor.contract.mjs
node tests/download-conflict-dialog.contract.mjs
# passed
```

### Rendered Playwright flows

The production build was served from the current worktree on loopback port
13001 and exercised with real Chromium. API calls are mocked by the repository
spec at the browser network boundary.

```bash
PLAYWRIGHT_BASE_URL=http://127.0.0.1:13001 \
npx playwright test tests/e2e/admin-shell.spec.ts \
  --project=chromium --workers=1 --timeout=45000 --reporter=line \
  --grep 'slow administrator|backup estimate|restore stages ordered chunks|gallery-dl connectivity|proxy operation discovery|gallery-dl operation discovery|integrity scan failure|scheduler separates task controls|scheduler sync-all|tasks request actionable pages'
# 11 passed (44.8s)
```

This includes scheduler pagination/permissions/sync, integrity start/poll/retry,
backup estimate/create, ordered restore chunks and rollback diagnostics,
gallery-dl connectivity/discovery, and proxy reattachment.

### Compose, governance, routes, and repository hygiene

```bash
docker compose config --quiet
docker compose --env-file .env.ci config --quiet
# passed

python3 scripts/check-governance-contracts.py
# governance contracts: ok

python3 scripts/verify-compose-resources.py
# compose resource contract: OK

git diff --check
# passed

scripts/privacy-scan.sh
# privacy-scan: ok
```

The Compose resource verifier initially failed because the acceptance override
did not set the restore staging/receipt host paths introduced by Task 3; Compose
therefore resolved them inside the worktree instead of the isolated `/tmp`
acceptance root. Adding `HOST_RESTORE_STAGING` and `HOST_RESTORE_RECEIPTS` to
the verifier's existing test-root environment produced the green result above.

## Files changed

- `backend/alembic/versions/b3d5f7a9c1e4_repair_calendar_schedule_rules.py`
- `backend/tests/test_migrations_idempotent.py`
- `backend/tests/test_download_defaults_threshold.py`
- `backend/tests/test_resource_pressure.py`
- `backend/tests/test_import_lifecycle_batches.py`
- `backend/tests/test_tasks.py`
- `scripts/verify-compose-resources.py`
- `.superpowers/sdd/admin-task-control-plane-convergence/task-4-report.md`

## Self-review

- Confirmed the published migration is byte-for-byte untouched.
- Confirmed the new revision has the single prior head and no new Alembic
  branch.
- Confirmed normalization preserves array order, literal time precision, JSONB
  objects, and empty arrays.
- Confirmed downgrade does not delete a column owned by the previous revision
  or invent discarded whitespace.
- Confirmed test migration does not weaken assertions: migrated retry tests
  inspect committed TaskRun/dispatch state and real PostgreSQL scope behavior,
  while transport-only mocks are asserted explicitly.
- Confirmed the release verifier fix keeps every acceptance bind beneath its
  disposable test root.
- Confirmed no production database, data, deployment, repository recount, or
  ledger repair was touched. Those actions remain gated on both independent
  whole-branch reviews as required by the brief.
- Confirmed generated contract/type files have no drift and `git diff --check`
  is clean.

## Concerns / deferred operational phase

No known code or validation concern remains in this review gate. Deployment,
rollback capture, production smoke tests, repository recount/repair, and global
ledger repair are intentionally deferred until both independent whole-branch
reviews are clean; this commit does not claim those post-review operations.

## Review fix round 1/5: POSIX edge-whitespace normalization

The whole-branch review found that PostgreSQL's one-argument `btrim` removes
ordinary spaces but not every edge character accepted by application
`str.strip()`. The migration test was extended first with real PostgreSQL
values built from `chr(9)` (tab), `chr(10)` (newline), and `chr(13)` (carriage
return). Literal expectations also preserve an internal tab/newline,
millisecond precision, and array order before downgrade, after downgrade, and
after re-upgrade.

RED against the original Task 4 revision:

```bash
docker compose run --rm --no-deps -v "$PWD:/workspace" \
  -w /workspace/backend backend python -m pytest -q \
  tests/test_migrations_idempotent.py::TestMigrationIdempotency::test_forward_calendar_repair_normalizes_times_across_round_trip
# 1 failed in 25.93s
# At index 0: '\t03:00:00.000\n' != '03:00:00.000'
```

The unpublished Task 4 revision now uses the same anchored POSIX expression in
both `jsonb_agg` transforms and both `EXISTS` change predicates:

```sql
regexp_replace(item.value, '^[[:space:]]+|[[:space:]]+$', '', 'g')
```

The anchors remove only leading/trailing POSIX whitespace. Internal characters,
time precision, and `WITH ORDINALITY` ordering remain unchanged.

Focused GREEN:

```bash
docker compose run --rm --no-deps -v "$PWD:/workspace" \
  -w /workspace/backend backend python -m pytest -q \
  tests/test_migrations_idempotent.py::TestMigrationIdempotency::test_forward_calendar_repair_normalizes_times_across_round_trip
# 1 passed in 31.52s
```

Full migration chain and calendar baseline:

```bash
docker compose run --rm --no-deps -v "$PWD:/workspace" \
  -w /workspace/backend backend python -m pytest -q \
  tests/test_migrations_idempotent.py
# 10 passed in 80.14s (0:01:20)

docker compose run --rm --no-deps -v "$PWD:/workspace" \
  -w /workspace/backend backend python -m pytest -q \
  tests/test_calendar_schedule_schema.py \
  tests/test_subscription_schedule_consistency.py \
  tests/test_subscription_scheduler_persistence.py \
  tests/test_subscription_reliable_schedule.py \
  tests/test_scheduler_contract.py \
  tests/test_operation_attention.py
# 60 passed in 186.85s (0:03:06)
```

Static, history, and hygiene checks:

```bash
backend/.venv/bin/ruff check \
  backend/alembic/versions/b3d5f7a9c1e4_repair_calendar_schedule_rules.py \
  backend/tests/test_migrations_idempotent.py
# All checks passed!

backend/.venv/bin/python -m compileall -q \
  backend/alembic/versions/b3d5f7a9c1e4_repair_calendar_schedule_rules.py \
  backend/tests/test_migrations_idempotent.py
# passed

cd backend && .venv/bin/alembic heads
# b3d5f7a9c1e4 (head)

git diff --check
# passed
```

Review-fix self-review:

- All four former `btrim` sites use one identical anchored expression; the
  transform and change predicate cannot disagree.
- Leading/trailing tab, newline, carriage return, and ordinary space are
  covered with literal PostgreSQL-backed expectations.
- Internal tab/newline characters, fractional time precision, JSONB shape, and
  array order remain exact through downgrade and re-upgrade.
- The published `a7c9e1f3b5d7` revision remains untouched; only the unpublished
  Task 4 repair revision and its integration test changed.
- The earlier complete backend result (`1013 passed, 4 skipped`) remains the
  release baseline; per review instructions it was not rerun for this isolated
  migration-expression correction, and the focused integration matrices are
  green.

## Review fix round 2/5: exact Python edge-whitespace semantics

The second whole-branch review correctly identified that PostgreSQL POSIX
`[[:space:]]` is still not equivalent to Python 3 `str.strip()`. The migration
now supplies two-argument `btrim`, superseding the round-1 POSIX expression,
with a literal SQL Unicode-escape character set containing exactly these 29
code points:

```text
U+0009-U+000D, U+001C-U+001F, U+0020, U+0085, U+00A0, U+1680,
U+2000-U+200A, U+2028-U+2029, U+202F, U+205F, U+3000
```

The integration inputs use PostgreSQL `U&'...'` literals and cover every
listed group: ASCII whitespace, the four information separators, NEL, NBSP,
OGHAM SPACE MARK, every U+2000-family character, line/paragraph separators,
narrow no-break space, medium mathematical space, and ideographic space.
Expected arrays are hard-coded literals; no production normalization helper is
used to calculate them. Internal NBSP/U+2003/U+202F and U+2028 characters are
retained while array order and fractional precision remain literal.

RED against the POSIX implementation:

```bash
docker compose run --rm --no-deps -v "$PWD:/workspace" \
  -w /workspace/backend backend python -m pytest -q \
  tests/test_migrations_idempotent.py::TestMigrationIdempotency::test_forward_calendar_repair_normalizes_times_across_round_trip
# 1 failed in 25.98s
# At index 0: '\x1c\x1d03:00:00.000\x1e\x1f' != '03:00:00.000'
```

The first implementation run exposed an f-string interpolation mistake in the
SQL JSONB path before any green result was accepted:

```text
# 1 failed in 22.77s
# NameError: name 'times' is not defined
```

Escaping the literal JSONB path braces fixed that construction error. The
reviewed expression is shared by both `jsonb_agg` transforms and both `EXISTS`
change predicates, so they cannot disagree:

```sql
btrim(
    item.value,
    U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000'
)
```

Focused GREEN, including downgrade and re-upgrade:

```bash
docker compose run --rm --no-deps -v "$PWD:/workspace" \
  -w /workspace/backend backend python -m pytest -q \
  tests/test_migrations_idempotent.py::TestMigrationIdempotency::test_forward_calendar_repair_normalizes_times_across_round_trip
# 1 passed in 33.36s
```

Migration chain and calendar baseline:

```bash
docker compose run --rm --no-deps -v "$PWD:/workspace" \
  -w /workspace/backend backend python -m pytest -q \
  tests/test_migrations_idempotent.py
# 10 passed in 77.80s (0:01:17)

docker compose run --rm --no-deps -v "$PWD:/workspace" \
  -w /workspace/backend backend python -m pytest -q \
  tests/test_calendar_schedule_schema.py \
  tests/test_subscription_schedule_consistency.py \
  tests/test_subscription_scheduler_persistence.py \
  tests/test_subscription_reliable_schedule.py \
  tests/test_scheduler_contract.py \
  tests/test_operation_attention.py
# 60 passed in 167.52s (0:02:47)
```

Exact-set, static, history, and hygiene checks:

```bash
backend/.venv/bin/python - <<'PY'
import importlib.util
import re
from pathlib import Path

path = Path(
    "backend/alembic/versions/"
    "b3d5f7a9c1e4_repair_calendar_schedule_rules.py"
)
spec = importlib.util.spec_from_file_location("migration", path)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)
actual = [
    int(value, 16)
    for value in re.findall(
        r"\\([0-9A-F]{4})", migration._PYTHON_STRIP_CHARACTERS_SQL
    )
]
expected = [
    codepoint for codepoint in range(0x110000) if chr(codepoint).isspace()
]
assert actual == expected, (actual, expected)
print(f"exact Python whitespace repertoire: {len(actual)} code points")
PY
# exact Python whitespace repertoire: 29 code points

backend/.venv/bin/ruff check \
  backend/alembic/versions/b3d5f7a9c1e4_repair_calendar_schedule_rules.py \
  backend/tests/test_migrations_idempotent.py
# All checks passed!

backend/.venv/bin/python -m compileall -q \
  backend/alembic/versions/b3d5f7a9c1e4_repair_calendar_schedule_rules.py \
  backend/tests/test_migrations_idempotent.py
# passed

cd backend && .venv/bin/alembic heads
# b3d5f7a9c1e4 (head)

git diff --exit-code HEAD -- \
  backend/alembic/versions/a7c9e1f3b5d7_add_structured_calendar_subscription_rules.py
# passed; published migration unchanged

git diff --check
# passed
```

Round-2 self-review:

- The SQL character literal was exhaustively compared with Python
  `str.isspace()` across all Unicode scalar values and matches exactly 29 code
  points; behavior no longer depends on PostgreSQL regex locale/classes.
- All edge characters are exercised through real PostgreSQL JSONB values.
  Concatenated prefix/suffix sequences ensure an omitted character would stop
  `btrim` and make the literal expected array fail.
- Two-argument `btrim` removes only listed characters at the two ends. The
  test proves internal Unicode whitespace, time precision, array order, and
  JSONB storage survive upgrade, downgrade, and re-upgrade.
- Both transforms and both predicates interpolate the same immutable SQL
  expression. The two JSONB paths remain literal after f-string brace escaping.
- The published `a7c9e1f3b5d7` migration is unchanged; only the unpublished
  Task 4 forward repair and its PostgreSQL integration test changed.
- Per the round-2 instruction, the prior complete backend release result
  (`1013 passed, 4 skipped`) was not rerun. The focused migration and calendar
  matrices are green, with no known code concern.
