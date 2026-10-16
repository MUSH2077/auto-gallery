"""One-shot download configuration and credential validation before RQ starts."""

from __future__ import annotations

import asyncio
import json
import sys

DB_READ_TIMEOUT_SECONDS = 5


def _db_download_concurrency():
    """Keep the existing best-effort DB read, with a bounded boot-time wait."""
    try:
        from app.database import async_session, engine
        from app.services.settings import get_download_defaults

        async def query():
            try:
                async with async_session() as db:
                    defaults = await get_download_defaults(db)
                    return defaults.get("download_concurrency")
            finally:
                await engine.dispose()

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
