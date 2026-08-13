"""Rebuild every Meilisearch projection and fail closed on an incomplete run."""

from __future__ import annotations

import asyncio
import json

from app.database import async_session
from app.services.search import SearchService


async def run() -> dict:
    async with async_session() as db:
        return await SearchService(db).reindex(resource_owner="manual-production-rebuild")


def main() -> None:
    result = asyncio.run(run())
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result.get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
