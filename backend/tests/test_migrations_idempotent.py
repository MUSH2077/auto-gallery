"""Migration idempotency and chain integrity tests.

Chain integrity tests run without a database.
Idempotency tests require a real PostgreSQL (marked ``@pytest.mark.integration``).
"""

import asyncio
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_migration_revisions() -> dict[str, str | None]:
    """Parse every .py migration file and return {revision: down_revision}.

    Skips files starting with '.' (disabled migrations) and __pycache__.
    """
    revisions: dict[str, str | None] = {}
    for path in sorted(VERSIONS_DIR.iterdir()):
        if path.name.startswith(".") or path.suffix != ".py":
            continue
        text = path.read_text()
        rev_match = re.search(
            r'^revision\s*[:=]\s*(?:\w+\s*[:=]\s*)?["\']([^"\']+)["\']',
            text,
            re.MULTILINE,
        )
        down_match = re.search(
            r'^down_revision\s*[:=]\s*(?:\w+(?:\[[^\]]*\])?\s*[:=]\s*)?'
            r'(None|["\']([^"\']+)["\'])',
            text,
            re.MULTILINE,
        )
        if not rev_match:
            raise ValueError(f"No revision found in {path.name}")
        rev_id = rev_match.group(1)
        if down_match and down_match.group(1) == "None":
            down_id = None
        elif down_match:
            down_id = down_match.group(2)
        else:
            raise ValueError(f"No down_revision found in {path.name}")
        revisions[rev_id] = down_id
    return revisions


def _build_revision_chain(revisions: dict[str, str | None]) -> list[str]:
    """Walk from root (down_revision=None) to head, returning ordered list."""
    children: dict[str | None, list[str]] = {}
    for rev_id, down_id in revisions.items():
        children.setdefault(down_id, []).append(rev_id)
    ordered: list[str] = []
    current: str | None = None
    while current in children:
        next_revs = children[current]
        current = next_revs[0]
        ordered.append(current)
    return ordered


# ── Chain Integrity Tests (no database needed) ──────────────────────────────

class TestMigrationChainIntegrity:
    """Tests that the migration revision chain is well-formed."""

    def test_chain_has_no_orphans(self):
        """Every down_revision (except None) must point to an existing revision."""
        revisions = _parse_migration_revisions()
        all_ids = set(revisions.keys())
        for rev_id, down_id in revisions.items():
            if down_id is not None:
                assert down_id in all_ids, (
                    f"Migration {rev_id} references down_revision={down_id} "
                    f"which does not exist"
                )

    def test_chain_has_single_root(self):
        """Exactly one migration should have down_revision=None."""
        revisions = _parse_migration_revisions()
        roots = [rid for rid, dr in revisions.items() if dr is None]
        assert len(roots) == 1, (
            f"Expected exactly 1 root revision, got {len(roots)}: {roots}"
        )

    def test_chain_is_connected_from_root_to_head(self):
        """Walking from root (None) to head visits all revisions."""
        revisions = _parse_migration_revisions()
        ordered = _build_revision_chain(revisions)
        assert len(ordered) == len(revisions), (
            f"Chain walk visited {len(ordered)} revisions "
            f"but {len(revisions)} exist. Missing: "
            f"{set(revisions.keys()) - set(ordered)}"
        )

    def test_no_duplicate_revisions(self):
        """No two migration files should share the same revision ID."""
        revisions = _parse_migration_revisions()
        py_files = [
            p
            for p in VERSIONS_DIR.iterdir()
            if p.suffix == ".py" and not p.name.startswith(".")
        ]
        assert len(revisions) == len(py_files), (
            f"Expected {len(py_files)} revisions but parsed {len(revisions)}"
        )

    def test_disabled_migrations_have_dot_prefix(self):
        """Non-.py files in versions/ must be prefixed with '.' (disabled)."""
        for path in VERSIONS_DIR.iterdir():
            if path.suffix == ".py" or path.name == "__pycache__":
                continue
            assert path.name.startswith("."), (
                f"Non-Python file in versions/ without '.' prefix: {path.name}"
            )


# ── Idempotency Tests (requires PostgreSQL) ──────────────────────────────────

@pytest.mark.integration
class TestMigrationIdempotency:
    """Tests that alembic upgrade head is idempotent."""

    @pytest.fixture(autouse=True)
    def _setup_env(self, test_database):
        """Ensure the isolated test database exists before each test."""
        self.test_database_url = test_database

    def _run_alembic(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run an alembic command against the test database."""
        env = {
            **dict(__import__("os").environ),
            "DATABASE_URL": self.test_database_url,
            "APP_CONFIG_ROOT": "/tmp/auto-gallery-migration-test-config",
            "SECRET_KEY": "test-idempotency-key-32chars!!",
            "ADMIN_PASSWORD": "test-idempotency-pw",
            "REDIS_URL": "redis://:dummy@localhost:6379/0",
        }
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_upgrade_head_is_idempotent(self):
        """Run alembic upgrade head TWICE — second run must succeed (exit 0)."""
        # First run
        r1 = self._run_alembic("upgrade", "head")
        assert r1.returncode == 0, (
            f"First alembic upgrade head failed:\n"
            f"STDOUT:\n{r1.stdout}\nSTDERR:\n{r1.stderr}"
        )

        # Second run — must be idempotent (no-op or succeed)
        r2 = self._run_alembic("upgrade", "head")
        assert r2.returncode == 0, (
            f"Second alembic upgrade head FAILED (migration NOT idempotent):\n"
            f"STDOUT:\n{r2.stdout}\nSTDERR:\n{r2.stderr}"
        )

    def test_downgrade_upgrade_cycle(self):
        """Downgrade and re-upgrade the complete migration chain."""
        initial_upgrade = self._run_alembic("upgrade", "head", timeout=120)
        assert initial_upgrade.returncode == 0, (
            "Initial alembic upgrade head failed:\n"
            f"STDOUT:\n{initial_upgrade.stdout}\nSTDERR:\n{initial_upgrade.stderr}"
        )

        downgrade = self._run_alembic("downgrade", "base", timeout=120)
        assert downgrade.returncode == 0, (
            "Alembic downgrade to base failed:\n"
            f"STDOUT:\n{downgrade.stdout}\nSTDERR:\n{downgrade.stderr}"
        )

        upgrade = self._run_alembic("upgrade", "head", timeout=120)
        assert upgrade.returncode == 0, (
            "Alembic upgrade head after full downgrade failed:\n"
            f"STDOUT:\n{upgrade.stdout}\nSTDERR:\n{upgrade.stderr}"
        )

    def test_calendar_migration_converts_live_fixed_time_rows_before_constraint(self):
        import asyncpg

        initial_up = self._run_alembic("upgrade", "head")
        assert initial_up.returncode == 0, initial_up.stderr

        down = self._run_alembic("downgrade", "f4c6d8e0a2b3")
        assert down.returncode == 0, down.stderr

        async def execute_sql(sql: str):
            conn = await asyncpg.connect(
                self.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
            )
            try:
                await conn.execute(sql)
            finally:
                await conn.close()

        async def fetch_row(sql: str):
            conn = await asyncpg.connect(
                self.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
            )
            try:
                return await conn.fetchrow(sql)
            finally:
                await conn.close()

        asyncio.run(execute_sql("""
            DELETE FROM system_settings WHERE key = 'subscription_defaults';
            INSERT INTO creators (id, name, is_active)
            VALUES ('11111111-1111-1111-1111-111111111111', 'calendar-migration', TRUE)
            ON CONFLICT (id) DO NOTHING;
            DELETE FROM subscriptions WHERE id = '22222222-2222-2222-2222-222222222222';
            INSERT INTO subscriptions (
                id, creator_id, is_active, sync_enabled, sync_interval_hours,
                schedule_mode, scheduled_times
            ) VALUES (
                '22222222-2222-2222-2222-222222222222',
                '11111111-1111-1111-1111-111111111111',
                TRUE, TRUE, 6, 'fixed_time', '03:00, 21:30'
            );
            INSERT INTO system_settings (key, value)
            VALUES (
                'subscription_defaults',
                '{"schedule_mode":"fixed_time","scheduled_times":"03:00,21:30"}'::jsonb
            );
        """))

        up = self._run_alembic("upgrade", "head")
        assert up.returncode == 0, up.stderr
        converted = asyncio.run(fetch_row("""
            SELECT
                schedule_mode,
                schedule_rule->>'frequency' AS frequency,
                ARRAY(
                    SELECT jsonb_array_elements_text(schedule_rule->'times')
                ) AS times
            FROM subscriptions
            WHERE id = '22222222-2222-2222-2222-222222222222'
        """))
        assert converted["schedule_mode"] == "calendar"
        assert converted["frequency"] == "daily"
        assert converted["times"] == ["03:00", "21:30"]

        down_again = self._run_alembic("downgrade", "f4c6d8e0a2b3")
        assert down_again.returncode == 0, down_again.stderr
        restored = asyncio.run(fetch_row("""
            SELECT schedule_mode, scheduled_times
            FROM subscriptions
            WHERE id = '22222222-2222-2222-2222-222222222222'
        """))
        assert restored["schedule_mode"] == "fixed_time"
        assert restored["scheduled_times"] == "03:00,21:30"

        final_up = self._run_alembic("upgrade", "head")
        assert final_up.returncode == 0, final_up.stderr

    def test_forward_calendar_repair_adds_missing_schedule_rule_from_previous_head(self):
        import asyncpg

        previous_head = "a7c9e1f3b5d7"
        initial_up = self._run_alembic("upgrade", "head")
        assert initial_up.returncode == 0, initial_up.stderr
        down = self._run_alembic("downgrade", previous_head)
        assert down.returncode == 0, down.stderr

        async def execute_sql(sql: str):
            conn = await asyncpg.connect(
                self.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
            )
            try:
                await conn.execute(sql)
            finally:
                await conn.close()

        async def fetch_value(sql: str):
            conn = await asyncpg.connect(
                self.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
            )
            try:
                return await conn.fetchval(sql)
            finally:
                await conn.close()

        asyncio.run(execute_sql("ALTER TABLE subscriptions DROP COLUMN schedule_rule"))
        stamped_revision = asyncio.run(
            fetch_value("SELECT version_num FROM alembic_version")
        )
        assert stamped_revision == previous_head

        repaired = self._run_alembic("upgrade", "head")
        assert repaired.returncode == 0, repaired.stderr
        column_type = asyncio.run(fetch_value("""
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'subscriptions'
              AND column_name = 'schedule_rule'
        """))
        assert column_type == "jsonb"

    def test_forward_calendar_repair_normalizes_times_across_round_trip(self):
        import asyncpg

        previous_head = "a7c9e1f3b5d7"
        at_head = self._run_alembic("upgrade", "head")
        assert at_head.returncode == 0, at_head.stderr
        down = self._run_alembic("downgrade", previous_head)
        assert down.returncode == 0, down.stderr

        async def execute_sql(sql: str):
            conn = await asyncpg.connect(
                self.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
            )
            try:
                await conn.execute(sql)
            finally:
                await conn.close()

        async def fetch_row(sql: str):
            conn = await asyncpg.connect(
                self.test_database_url.replace("postgresql+asyncpg://", "postgresql://")
            )
            try:
                return await conn.fetchrow(sql)
            finally:
                await conn.close()

        asyncio.run(execute_sql("""
            INSERT INTO creators (id, name, is_active)
            VALUES ('33333333-3333-3333-3333-333333333333', 'calendar-repair', TRUE)
            ON CONFLICT (id) DO NOTHING;
            DELETE FROM subscriptions WHERE id = '44444444-4444-4444-4444-444444444444';
            INSERT INTO subscriptions (
                id, creator_id, is_active, sync_enabled, sync_interval_hours,
                schedule_mode, schedule_rule
            ) VALUES (
                '44444444-4444-4444-4444-444444444444',
                '33333333-3333-3333-3333-333333333333',
                TRUE, TRUE, 6, 'calendar',
                jsonb_build_object(
                    'frequency', 'weekly',
                    'weekdays', jsonb_build_array(1, 5),
                    'times', jsonb_build_array(
                        chr(9) || '03:00:00.000' || chr(10),
                        chr(13) || '21:30:00' || chr(9),
                        ' 12:' || chr(9) || '34:56.789 ' || chr(13)
                    )
                )
            );
            DELETE FROM system_settings WHERE key = 'subscription_defaults';
            INSERT INTO system_settings (key, value)
            VALUES (
                'subscription_defaults',
                jsonb_build_object(
                    'schedule_mode', 'calendar',
                    'schedule_rule', jsonb_build_object(
                        'frequency', 'daily',
                        'times', jsonb_build_array(
                            chr(10) || '05:15:00.120' || chr(13),
                            chr(9) || '18:45' || chr(13) || chr(10),
                            chr(13) || '07:' || chr(10) || '08:09.010' || chr(9)
                        )
                    )
                )
            );
        """))

        upgraded = self._run_alembic("upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        normalized = asyncio.run(fetch_row("""
            SELECT
                ARRAY(
                    SELECT jsonb_array_elements_text(schedule_rule->'times')
                ) AS subscription_times,
                ARRAY(
                    SELECT jsonb_array_elements_text(value->'schedule_rule'->'times')
                    FROM system_settings
                    WHERE key = 'subscription_defaults'
                ) AS default_times
            FROM subscriptions
            WHERE id = '44444444-4444-4444-4444-444444444444'
        """))
        assert normalized["subscription_times"] == [
            "03:00:00.000",
            "21:30:00",
            "12:\t34:56.789",
        ]
        assert normalized["default_times"] == [
            "05:15:00.120",
            "18:45",
            "07:\n08:09.010",
        ]

        downgraded = self._run_alembic("downgrade", previous_head)
        assert downgraded.returncode == 0, downgraded.stderr
        after_downgrade = asyncio.run(fetch_row("""
            SELECT
                ARRAY(
                    SELECT jsonb_array_elements_text(schedule_rule->'times')
                ) AS subscription_times,
                ARRAY(
                    SELECT jsonb_array_elements_text(value->'schedule_rule'->'times')
                    FROM system_settings
                    WHERE key = 'subscription_defaults'
                ) AS default_times,
                pg_typeof(schedule_rule)::text AS schedule_rule_type
            FROM subscriptions
            WHERE id = '44444444-4444-4444-4444-444444444444'
        """))
        assert after_downgrade["subscription_times"] == [
            "03:00:00.000",
            "21:30:00",
            "12:\t34:56.789",
        ]
        assert after_downgrade["default_times"] == [
            "05:15:00.120",
            "18:45",
            "07:\n08:09.010",
        ]
        assert after_downgrade["schedule_rule_type"] == "jsonb"

        final_up = self._run_alembic("upgrade", "head")
        assert final_up.returncode == 0, final_up.stderr
        after_round_trip = asyncio.run(fetch_row("""
            SELECT
                ARRAY(
                    SELECT jsonb_array_elements_text(schedule_rule->'times')
                ) AS subscription_times,
                ARRAY(
                    SELECT jsonb_array_elements_text(value->'schedule_rule'->'times')
                    FROM system_settings
                    WHERE key = 'subscription_defaults'
                ) AS default_times
            FROM subscriptions
            WHERE id = '44444444-4444-4444-4444-444444444444'
        """))
        assert after_round_trip["subscription_times"] == [
            "03:00:00.000",
            "21:30:00",
            "12:\t34:56.789",
        ]
        assert after_round_trip["default_times"] == [
            "05:15:00.120",
            "18:45",
            "07:\n08:09.010",
        ]
