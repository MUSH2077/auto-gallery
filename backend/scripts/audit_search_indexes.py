#!/usr/bin/env python3
"""Read-only database/Meilisearch projection consistency audit."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import async_session
from app.services.search import SearchService


async def _main() -> int:
    async with async_session() as db:
        result = await SearchService(db).audit_projection()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
