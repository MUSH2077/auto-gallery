import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.services.artifact_ledger import ArtifactLedger, artifact_row
from app.jobs.import_runner import _consume_import_file_list


class FakeRedis:
    def __init__(self, value):
        self.value = value
        self.deleted = []

    def get(self, key):
        self.key = key
        return self.value

    def delete(self, key):
        self.deleted.append(key)


def test_consume_import_file_list_reads_and_deletes_snapshot_key():
    redis = FakeRedis(json.dumps(["/downloads/pixiv/a.json", "/downloads/pixiv/b.json"]))

    files = _consume_import_file_list(redis, "job-1")

    assert files == ["/downloads/pixiv/a.json", "/downloads/pixiv/b.json"]
    assert redis.key == "import:job-1:files"
    assert redis.deleted == ["import:job-1:files"]


def test_consume_import_file_list_returns_none_without_snapshot():
    redis = FakeRedis(None)

    assert _consume_import_file_list(redis, "job-1") is None
    assert redis.deleted == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ledger_supplies_import_files_when_redis_snapshot_is_missing(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.models.subscription import Subscription

    download_root = tmp_path / "downloads"
    work_dir = download_root / "pixiv" / "1980643" / "38362603"
    work_dir.mkdir(parents=True)
    metadata = work_dir / "metadata.json"
    metadata.write_text(json.dumps({
        "id": 38362603,
        "user": {"id": 1980643, "name": "Fixture"},
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "download_root", str(download_root))

    async def clear(db):
        await db.execute(text("""
            TRUNCATE
                storage_artifacts,
                import_jobs,
                download_jobs,
                subscriptions,
                creators
            RESTART IDENTITY CASCADE
        """))
        await db.commit()

    try:
        async with async_session() as db:
            await clear(db)
            creator = Creator(name="fixture", display_name="Fixture")
            db.add(creator)
            await db.flush()
            sub = Subscription(creator_id=creator.id, name="Fixture")
            db.add(sub)
            await db.flush()
            download_job = DownloadJob(
                subscription_id=sub.id,
                source="pixiv",
                source_url="https://www.pixiv.net/users/1980643",
                status="importing",
            )
            db.add(download_job)
            await db.flush()
            import_job = ImportJob(download_job_id=download_job.id, status="enqueued")
            db.add(import_job)
            await db.flush()

            row = artifact_row(metadata, download_root, download_job.id)
            assert row is not None
            await ArtifactLedger(db).upsert_many([row])
            await db.commit()

            assert _consume_import_file_list(FakeRedis(None), str(uuid4())) is None
            paths = await ArtifactLedger(db).new_metadata_paths(download_job.id)

            assert paths == ["pixiv/1980643/38362603/metadata.json"]
    finally:
        async with async_session() as db:
            await clear(db)
        await engine.dispose()
