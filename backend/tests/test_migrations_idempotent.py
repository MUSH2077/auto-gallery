"""Migration idempotency and chain integrity tests.

Chain integrity tests run without a database.
Idempotency tests require a real PostgreSQL (marked ``@pytest.mark.integration``).
"""

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
        """Downgrade -1 then upgrade head for each revision — must succeed."""
        revisions = _parse_migration_revisions()
        ordered = _build_revision_chain(revisions)

        # First, apply all migrations
        self._run_alembic("upgrade", "head", timeout=120)

        # Test each revision individually from head back to root
        for rev_id in reversed(ordered):
            downgrade = self._run_alembic("downgrade", "-1", timeout=60)
            upgrade = self._run_alembic("upgrade", "head", timeout=60)
            assert upgrade.returncode == 0, (
                f"alembic upgrade head failed after downgrading from {rev_id}:\n"
                f"  Downgrade STDERR: {downgrade.stderr.strip()}\n"
                f"  Upgrade STDOUT: {upgrade.stdout.strip()}\n"
                f"  Upgrade STDERR: {upgrade.stderr.strip()}"
            )
