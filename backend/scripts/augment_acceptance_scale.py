#!/usr/bin/env python3
"""Pad a sanitized snapshot to the documented minimum cardinalities."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import Asset, StorageArtifact, Tag, Work, WorkSourceTag, WorkTag

TARGETS = {"works": 67_000, "assets": 90_000, "tag_relations": 470_000, "artifacts": 330_000}


async def main() -> None:
    async with async_session() as db:
        counts = {
            "works": int((await db.execute(select(func.count()).select_from(Work))).scalar_one()),
            "assets": int((await db.execute(select(func.count()).select_from(Asset))).scalar_one()),
            "tag_relations": int((await db.execute(select(func.count()).select_from(WorkTag))).scalar_one())
            + int((await db.execute(select(func.count()).select_from(WorkSourceTag))).scalar_one()),
            "artifacts": int((await db.execute(select(func.count()).select_from(StorageArtifact))).scalar_one()),
        }
        for offset in range(0, max(0, TARGETS["works"] - counts["works"]), 500):
            size = min(500, TARGETS["works"] - counts["works"] - offset)
            await db.execute(insert(Work), [
                {"id": uuid4(), "title": f"acceptance-padding-work-{offset + number}", "is_nsfw": False, "is_ai_generated": False, "is_favorite": False}
                for number in range(size)
            ])
        for offset in range(0, max(0, TARGETS["assets"] - counts["assets"]), 500):
            size = min(500, TARGETS["assets"] - counts["assets"] - offset)
            await db.execute(insert(Asset), [
                {
                    "id": uuid4(),
                    "file_path": f"acceptance/padding-{offset + number}.jpg",
                    "file_name": f"padding-{offset + number}.jpg",
                    "file_size": 1024,
                    "mime_type": "image/jpeg",
                    "width": 32,
                    "height": 32,
                }
                for number in range(size)
            ])
        for offset in range(0, max(0, TARGETS["artifacts"] - counts["artifacts"]), 500):
            size = min(500, TARGETS["artifacts"] - counts["artifacts"] - offset)
            await db.execute(insert(StorageArtifact), [
                {
                    "id": uuid4(),
                    "storage_root": "downloads",
                    "file_path": f"acceptance/padding-{offset + number}.json",
                    "source": "acceptance",
                    "creator_dir": "padding",
                    "source_work_id": f"padding-{offset + number}",
                    "file_name": f"padding-{offset + number}.json",
                    "artifact_type": "metadata",
                    "file_size": 128,
                    "state": "done",
                    "attempts": 0,
                }
                for number in range(size)
            ])
        missing_relations = max(0, TARGETS["tag_relations"] - counts["tag_relations"])
        if missing_relations:
            tag_names = [f"acceptance-padding-tag-{number}" for number in range(8)]
            await db.execute(
                pg_insert(Tag)
                .values([{"id": uuid4(), "normalized_name": name} for name in tag_names])
                .on_conflict_do_nothing(index_elements=[Tag.normalized_name])
            )
            tag_ids = list(
                (
                    await db.execute(
                        select(Tag.id)
                        .where(Tag.normalized_name.in_(tag_names))
                        .order_by(Tag.normalized_name)
                    )
                ).scalars()
            )
            work_ids = list((await db.execute(select(Work.id).order_by(Work.id))).scalars())
            if not tag_ids or not work_ids:
                raise RuntimeError("cannot create acceptance tag relations without tags and works")
            batch: list[dict] = []
            created = 0
            for work_id in work_ids:
                for tag_id in tag_ids:
                    if created >= missing_relations:
                        break
                    batch.append(
                        {
                            "id": uuid4(),
                            "work_id": work_id,
                            "tag_id": tag_id,
                            "source": "acceptance",
                        }
                    )
                    created += 1
                    if len(batch) >= 5_000:
                        await db.execute(insert(WorkTag), batch)
                        batch.clear()
                if created >= missing_relations:
                    break
            if batch:
                await db.execute(insert(WorkTag), batch)
            if created != missing_relations:
                raise RuntimeError(
                    f"could only create {created} of {missing_relations} tag relations"
                )
        await db.commit()
    print({"before": counts, "targets": TARGETS})


if __name__ == "__main__":
    asyncio.run(main())
