"""Pure contract tests for bounded importer batches (no database required)."""

from __future__ import annotations

import asyncio
import inspect
from uuid import uuid4

from sqlalchemy.dialects import postgresql

import app.jobs.import_runner as import_runner_module
from app.jobs.import_runner import (
    IMPORT_WORK_BATCH_SIZE,
    _BulkImportConflict,
    _artifact_batch_lease_guard,
    _claim_import_execution,
    _execute_bulk_outboxes,
    _insert_bulk_domain_with_fallback,
    _normalize_tag_value,
    _prepared_work_batches,
    _take_prepared_work_slice,
    normal_import_batch_statement_bound,
    run_import_job,
)
from app.services.artifact_ledger import ArtifactLedger


class _FakeResult:
    def __init__(self, scalar_values=(), rowcount: int = 0):
        self._scalar_values = list(scalar_values)
        self.rowcount = rowcount

    def scalars(self):
        return list(self._scalar_values)


class _RecordingSession:
    def __init__(self, claimed=()):
        self.claimed = list(claimed)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _FakeResult(self.claimed)
        return _FakeResult(rowcount=len(self.claimed))


class _SequenceSession:
    def __init__(self, scalar_results):
        self.scalar_results = [list(values) for values in scalar_results]
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        values = self.scalar_results.pop(0) if self.scalar_results else []
        return _FakeResult(values, rowcount=len(values))


def _postgresql_sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_prepared_work_batches_are_stable_and_capped_at_25():
    prepared = {str(index): {"index": index} for index in range(53)}

    batches = list(_prepared_work_batches(prepared, batch_size=1000))

    assert [len(batch) for batch in batches] == [25, 25, 3]
    assert [key for batch in batches for key, _ in batch] == list(prepared)
    assert IMPORT_WORK_BATCH_SIZE == 25


def test_prepared_work_slice_uses_current_budget_without_reordering():
    batch = [(str(index), {"index": index}) for index in range(25)]

    selected, deferred = _take_prepared_work_slice(batch, work_units=2)

    assert [key for key, _ in selected] == ["0", "1"]
    assert [key for key, _ in deferred] == [str(index) for index in range(2, 25)]
    assert selected + deferred == batch


def test_import_resource_slice_cools_down_after_releasing_profile_lock():
    source = inspect.getsource(import_runner_module._import_resource_slice)

    assert source.index("await stack.aclose()") < source.index(
        "await sleep_for_profile_slice_cooldown("
    )
    assert "yield limits" in source
    assert "if not limits.allowed" in source


def test_import_claim_consumes_adaptive_work_units():
    source = inspect.getsource(run_import_job)

    assert "_take_prepared_work_slice(" in source
    assert "import_limits.work_units" in source
    assert "pending_batches.appendleft(deferred_batch)" in source


def test_import_controller_has_no_job_wide_heavy_io_decorator():
    assert not hasattr(run_import_job, "__wrapped__")


def test_import_execution_claim_is_one_skip_locked_transaction():
    source = inspect.getsource(_claim_import_execution)

    assert ".with_for_update(skip_locked=True)" in source
    assert "execution_token = execution_token" in source
    assert "execution_attempt" in source


def test_long_media_slice_renews_artifact_lease_in_background():
    source = inspect.getsource(_artifact_batch_lease_guard)

    assert "renew_work_leases" in source
    assert "asyncio.create_task" in source
    assert "sorted(lease.owned)" in source


def test_import_generation_is_armed_for_after_commit():
    source = inspect.getsource(_execute_bulk_outboxes)

    assert "mark_works_generation_pending(db)" in source
    assert "cache_bump_generation" not in source


def test_tag_normalization_is_bounded_and_collision_stable():
    oversized = " Example " + ("x" * 250)

    normalized = _normalize_tag_value(oversized)

    assert len(normalized) == 200
    assert normalized == _normalize_tag_value(oversized)
    assert normalized != _normalize_tag_value(oversized + "different")
    assert _normalize_tag_value("  MiXeD  ") == "mixed"


def _row(column_count: int, value: int) -> dict:
    return {f"column_{index}": value for index in range(column_count)}


def test_normal_25_work_bulk_path_has_at_most_20_sql_statements():
    """Acceptance shape: 25 works, 90 assets and 1,400 tag relations."""

    staged = []
    remaining_assets = 90
    remaining_tags = 1_400
    for work_index in range(25):
        work_assets = remaining_assets // (25 - work_index)
        work_tags = remaining_tags // (25 - work_index)
        remaining_assets -= work_assets
        remaining_tags -= work_tags
        staged.append(
            {
                "rows": {
                    "works": [_row(8, work_index)],
                    "work_sources": [_row(10, work_index)],
                    "assets": [
                        _row(17, work_index) for _ in range(work_assets)
                    ],
                    "asset_sources": [
                        _row(9, work_index) for _ in range(work_assets)
                    ],
                    "work_tags": [
                        _row(4, work_index) for _ in range(work_tags)
                    ],
                    "work_source_tags": [
                        _row(5, work_index) for _ in range(work_tags)
                    ],
                    "work_curation_states": [_row(5, work_index)],
                },
                "derivative_requests": [
                    {"asset_id": object()} for _ in range(work_assets)
                ],
            }
        )

    search_entities = 25 + 25 + 1_400 + 1 + 1
    statement_bound = normal_import_batch_statement_bound(
        staged,
        search_entities,
    )

    # 2 claim + 1 batch media CAS + 1 pre-write CAS + 2 SAVEPOINT
    # + 7 domain + 3 outbox + 4 checkpoint/progress statements.
    assert statement_bound == 20
    assert statement_bound <= 20


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _SavepointSession:
    def begin_nested(self):
        return _NestedTransaction()


def test_bulk_conflict_recursively_isolates_only_the_bad_work(monkeypatch):
    async def fake_bulk_insert(_db, staged, _counters):
        if any(item["source_work_id"] == "bad" for item in staged):
            raise _BulkImportConflict("duplicate source work")
        return 7

    monkeypatch.setattr(
        import_runner_module,
        "_execute_bulk_domain",
        fake_bulk_insert,
    )
    staged = [
        {"source_work_id": source_work_id}
        for source_work_id in ("one", "two", "bad", "four")
    ]
    results = {}
    counters = {
        "savepoints": 0,
        "bulk_conflicts": 0,
        "domain_statements": 0,
        "domain_execute_calls": 0,
    }

    accepted = asyncio.run(
        _insert_bulk_domain_with_fallback(
            _SavepointSession(),
            staged,
            results,
            counters,
        )
    )

    assert [item["source_work_id"] for item in accepted] == [
        "one",
        "two",
        "four",
    ]
    assert set(results) == {"bad"}
    assert results["bad"][0] == "failed"
    assert counters["bulk_conflicts"] >= 1
    assert counters["savepoints"] < len(staged) * 2


def test_claim_work_batch_locks_one_representative_and_updates_all_artifacts():
    candidates = [f"work-{index}" for index in range(30)]
    claimed = candidates[:IMPORT_WORK_BATCH_SIZE]
    session = _RecordingSession(claimed)

    result = asyncio.run(
        ArtifactLedger(session).claim_work_batch(
            uuid4(),
            uuid4(),
            lease_token=uuid4(),
            source_work_ids=candidates,
            limit=1000,
        )
    )

    assert list(result.claimed) == claimed
    assert result.contended == ()
    assert result.existing == ()
    assert len(session.statements) == 2
    claim_sql = _postgresql_sql(session.statements[0]).upper()
    update_sql = _postgresql_sql(session.statements[1]).upper()
    assert "ROW_NUMBER() OVER" in claim_sql
    assert "FOR UPDATE OF STORAGE_ARTIFACTS SKIP LOCKED" in claim_sql
    assert "UPDATE STORAGE_ARTIFACTS" in update_sql
    assert "SOURCE_WORK_ID IN" in update_sql
    assert "LEASE_TOKEN" in update_sql
    # A valid lease is never reclaimable merely because the durable job UUID
    # happens to match; ownership belongs to the execution token.
    assert "IMPORT_JOB_ID" not in claim_sql


def test_claim_classifies_skip_locked_misses_without_false_existing():
    # claim one, update it, classify one authoritative WorkSource, then mark it
    # done.  The remaining miss must stay contended.
    session = _SequenceSession((("claimed",), (), ("existing",), ()))
    token = uuid4()

    result = asyncio.run(
        ArtifactLedger(session).claim_work_batch(
            uuid4(),
            uuid4(),
            lease_token=token,
            source="pixiv",
            source_work_ids=["claimed", "contended", "existing"],
        )
    )

    assert result.claimed == ("claimed",)
    assert result.contended == ("contended",)
    assert result.existing == ("existing",)
    assert len(session.statements) == 4
    claim_sql = _postgresql_sql(session.statements[0]).upper()
    update_sql = _postgresql_sql(session.statements[1]).upper()
    assert "DOWNLOAD_JOB_ID" not in claim_sql
    assert "DOWNLOAD_JOB_ID" not in update_sql
    assert "STORAGE_ARTIFACTS.SOURCE" in claim_sql


def test_lease_renewal_is_execution_token_cas():
    session = _RecordingSession(("owned",))
    job_id = uuid4()
    token = uuid4()

    owned = asyncio.run(
        ArtifactLedger(session).renew_work_leases(
            uuid4(),
            ["owned", "lost"],
            expected_import_job_id=job_id,
            expected_lease_token=token,
        )
    )

    assert owned == {"owned"}
    assert len(session.statements) == 1
    sql = _postgresql_sql(session.statements[0]).upper()
    assert "ARTIFACT_TYPE" in sql
    assert "IMPORT_JOB_ID" in sql
    assert "LEASE_TOKEN" in sql
    assert "LEASE_EXPIRES_AT" in sql
    assert "DOWNLOAD_JOB_ID" not in sql


def test_mark_work_results_uses_one_case_update_for_distinct_errors():
    outcomes = {
        "ok": ("done", None),
        "bad-a": ("failed", "first failure"),
        "bad-b": ("failed", "different failure"),
    }
    session = _RecordingSession()

    changed = asyncio.run(
        ArtifactLedger(session).mark_work_results(
            uuid4(),
            outcomes,
            expected_import_job_id=uuid4(),
            expected_lease_token=uuid4(),
        )
    )

    assert changed == set()
    assert len(session.statements) == 1
    sql = _postgresql_sql(session.statements[0]).upper()
    assert sql.count("CASE WHEN") == 2
    assert "IMPORT_JOB_ID" in sql
    assert "LEASE_TOKEN" in sql
    assert "LEASE_EXPIRES_AT" in sql
    assert "DOWNLOAD_JOB_ID" not in sql


def test_release_owned_leases_is_global_token_cas():
    session = _RecordingSession(("released",))
    token = uuid4()

    released = asyncio.run(
        ArtifactLedger(session).release_owned_leases(uuid4(), token)
    )

    assert released == {"released"}
    sql = _postgresql_sql(session.statements[0]).upper()
    assert "IMPORT_JOB_ID" in sql
    assert "LEASE_TOKEN" in sql
    assert "DOWNLOAD_JOB_ID" not in sql
