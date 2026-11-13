"""One-shot download configuration and credential validation before RQ starts."""

from __future__ import annotations

import asyncio
import json
import sys

DB_READ_TIMEOUT_SECONDS = 5


def _db_download_concurrency():
    """Read the startup setting without loading the ORM into this helper."""
    try:
        import asyncpg

        from app.config import settings
        from app.services.download_concurrency import DEFAULT_DOWNLOAD_CONCURRENCY

        async def query():
            connection = await asyncpg.connect(
                settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1),
                timeout=DB_READ_TIMEOUT_SECONDS,
            )
            try:
                value = await connection.fetchval(
                    "SELECT value FROM system_settings WHERE key = $1", "download_defaults"
                )
            finally:
                await connection.close(timeout=DB_READ_TIMEOUT_SECONDS)
            defaults = json.loads(value) if isinstance(value, str) else value
            if not isinstance(defaults, dict):
                defaults = {}
            # A missing setting uses the stored-settings default; a failed
            # read returns None below so the existing CLI fallback can apply.
            return int(defaults.get("download_concurrency", DEFAULT_DOWNLOAD_CONCURRENCY))

        async def bounded():
            return await asyncio.wait_for(query(), timeout=DB_READ_TIMEOUT_SECONDS)

        return asyncio.run(bounded())
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        print(
            f"[worker_entrypoint] DB concurrency read failed ({reason}); using fallback",
            file=sys.stderr,
        )
        return None


def resolve_startup(argv_value: str | None) -> dict:
    from worker_entrypoint import _sweep_personal_auth_startup, resolve_download_concurrency

    # Credential storage failure must stop startup, not fall back to a value.
    removed = _sweep_personal_auth_startup(["downloads"])
    configured, cap, effective = resolve_download_concurrency(
        _db_download_concurrency(), argv_value,
    )
    return {"removed_auth_configs": removed, "configured": configured,
            "cap": cap, "effective": effective}


def main() -> int:
    try:
        result = resolve_startup(sys.argv[1] if len(sys.argv) > 1 else None)
    except Exception as exc:
        print(f"[worker_entrypoint] download startup validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
