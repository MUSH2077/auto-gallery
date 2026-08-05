from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.media_assets import VideoDerivatives, VideoInspection


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _DB:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0

    async def execute(self, _statement):
        return _Rows(self.rows)

    async def commit(self):
        self.commits += 1


def _asset(file_path: str = "pixiv/creator/clip.mp4"):
    return SimpleNamespace(
        id=uuid4(),
        file_path=file_path,
        file_name=Path(file_path).name,
        mime_type="video/mp4",
        width=None,
        height=None,
        duration=None,
        thumb_sm_path=None,
        thumb_lg_path=None,
        created_at=datetime.now(timezone.utc),
    )


def _work_source():
    return SimpleNamespace(
        source="pixiv",
        source_work_id="work-1",
        raw_metadata={},
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_video_backfill_dry_run_never_writes(tmp_path, monkeypatch):
    from app.services import video_backfill

    asset = _asset()
    db = _DB([(asset, _work_source())])
    monkeypatch.setattr(video_backfill.settings, "download_root", str(tmp_path / "downloads"))
    monkeypatch.setattr(video_backfill.settings, "library_root", str(tmp_path / "library"))
    monkeypatch.setattr(
        video_backfill.WorkImportService,
        "library_directory",
        lambda *_args: ("creator", tmp_path / "library" / "pixiv" / "creator" / "work-1"),
    )

    report = await video_backfill.backfill_video_assets(db)

    assert report == {
        "matched": 1,
        "needs_repair": 1,
        "repaired": 0,
        "failed": 0,
        "dry_run": True,
    }
    assert db.commits == 0
    assert asset.width is None


@pytest.mark.asyncio
async def test_video_backfill_apply_updates_metadata_posters_and_ledger(tmp_path, monkeypatch):
    from app.services import video_backfill

    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    source = download_root / "pixiv" / "creator" / "clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fixture video")
    lib_dir = library_root / "pixiv" / "creator" / "work-1"
    lib_dir.mkdir(parents=True)
    thumb = lib_dir / "clip.thumbnail.webp"
    poster = lib_dir / "clip.poster.webp"
    thumb.write_bytes(b"thumb")
    poster.write_bytes(b"poster")

    asset = _asset("pixiv/creator/clip.mp4")
    db = _DB([(asset, _work_source()), (asset, _work_source())])
    monkeypatch.setattr(video_backfill.settings, "download_root", str(download_root))
    monkeypatch.setattr(video_backfill.settings, "library_root", str(library_root))
    monkeypatch.setattr(
        video_backfill.WorkImportService,
        "library_directory",
        lambda *_args: ("creator", lib_dir),
    )
    monkeypatch.setattr(
        video_backfill,
        "render_video_derivatives",
        lambda *_args, **_kwargs: VideoDerivatives(
            VideoInspection(720, 1016, 29.5, "h264", "mov,mp4"),
            thumb,
            poster,
        ),
    )
    ledger_rows: list[dict] = []

    class _Ledger:
        def __init__(self, _db):
            pass

        async def upsert_many(self, rows):
            ledger_rows.extend(rows)

    monkeypatch.setattr(video_backfill, "ArtifactLedger", _Ledger)

    report = await video_backfill.backfill_video_assets(db, apply=True)

    assert report == {
        "matched": 1,
        "needs_repair": 1,
        "repaired": 1,
        "failed": 0,
        "dry_run": False,
    }
    assert (asset.width, asset.height, asset.duration) == (720, 1016, 29.5)
    assert asset.mime_type == "video/mp4"
    assert asset.thumb_sm_path == "pixiv/creator/work-1/clip.thumbnail.webp"
    assert asset.thumb_lg_path == "pixiv/creator/work-1/clip.poster.webp"
    assert {row["artifact_type"] for row in ledger_rows} == {"thumbnail", "video_poster"}
    assert db.commits == 1
