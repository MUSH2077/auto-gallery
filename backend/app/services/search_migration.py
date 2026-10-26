"""Check a search cutover and enqueue a bounded rebuild from authoritative rows.

Run after `alembic upgrade head`, with the old application and workers stopped.
This does not copy remote search data or delete the old search volume.
"""
from __future__ import annotations
import argparse
import asyncio
import json

from app.database import async_session
from app.services.search_backend import compatible_commands, postgres_search_ready


async def check_and_rebuild(rebuild=False, *, rebuild_if_needed=False):
    from app.services.remote_search_flight import read_marker
    async with async_session() as db:
        if not await postgres_search_ready(db):
            raise RuntimeError("Search schema is missing or unfinished commands still belong to the old backend; upgrade schema and drain them with the old version first")
        if not await compatible_commands(db) or read_marker():
            raise RuntimeError("An unfinished search command remains; do not remove its marker to force cutover")
    from sqlalchemy import select, text
    from app.services.search_projection import PROJECTION_SPECS
    from app.models.search_rebuild import SearchRebuild
    async with async_session() as db:
        existing = set((await db.execute(text("SELECT uid FROM search_pg_indexes"))).scalars())
        needs_rebuild = not set(PROJECTION_SPECS).issubset(existing)
        active = (await db.execute(select(SearchRebuild).where(SearchRebuild.state.not_in(("complete", "failed"))))).scalar_one_or_none()
    result = {"backend": "postgresql", "ready_for_rebuild": True, "needs_rebuild": needs_rebuild}
    if rebuild or (rebuild_if_needed and needs_rebuild):
        from app.services.search_rebuild import start_rebuild
        from app.services.search_projection import PROJECTION_SPECS
        from app.services.outbox_coordinator import wake_pending_outboxes
        if active is not None:
            from app.services.search_rebuild import _status
            result["rebuild"] = _status(active)
        else:
            result["rebuild"] = await start_rebuild(list(PROJECTION_SPECS))
        wake_pending_outboxes({"search": 1})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--rebuild-if-needed", action="store_true", help="initialize missing indexes without rebuilding every deployment")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(check_and_rebuild(args.rebuild, rebuild_if_needed=args.rebuild_if_needed)), ensure_ascii=False))


if __name__ == "__main__":
    main()
