import json
from pathlib import Path

from app.providers.x import XProvider
from app.services.artifact_discovery import group_metadata_by_work, media_files_for_group
from app.services.artifact_ledger import artifact_row


def _metadata(tweet_id: int, filename: str) -> dict:
    return {
        "tweet_id": tweet_id,
        "filename": filename,
        "date": "2024-06-27 11:55:56",
        "content": f"Tweet {tweet_id}",
        "hashtags": ["fixture"],
        "user": {"id": 123, "name": "fixture"},
    }


def test_date_directory_groups_media_by_metadata_stem(tmp_path):
    work_dir = tmp_path / "twitter" / "fixture" / "2024-06-27"
    work_dir.mkdir(parents=True)
    paths = []
    for tweet_id, stem in ((101, "media-a"), (202, "media-b")):
        metadata_path = work_dir / f"{stem}.json"
        metadata_path.write_text(json.dumps(_metadata(tweet_id, stem)), encoding="utf-8")
        (work_dir / f"{stem}.jpg").write_bytes(stem.encode())
        paths.append(metadata_path)

    groups, invalid = group_metadata_by_work(XProvider(), paths)

    assert invalid == []
    assert set(groups) == {"101", "202"}
    assert [path.name for path in media_files_for_group(groups["101"], "101")] == ["media-a.jpg"]
    assert [path.name for path in media_files_for_group(groups["202"], "202")] == ["media-b.jpg"]


def test_date_directory_matches_gallerydl_work_id_and_asset_index(tmp_path):
    work_dir = tmp_path / "twitter" / "fixture" / "2024-06-27"
    work_dir.mkdir(parents=True)
    first = work_dir / "remote-media-a.json"
    second = work_dir / "remote-media-b.json"
    first.write_text(json.dumps({
        **_metadata(101, "remote-media-a"),
        "num": 1,
        "extension": "jpg",
    }), encoding="utf-8")
    second.write_text(json.dumps({
        **_metadata(202, "remote-media-b"),
        "num": 1,
        "extension": "jpg",
    }), encoding="utf-8")
    (work_dir / "101_1.jpg").write_bytes(b"first")
    (work_dir / "202_1.jpg").write_bytes(b"second")

    groups, invalid = group_metadata_by_work(XProvider(), [first, second])

    assert invalid == []
    assert [path.name for path in media_files_for_group(groups["101"], "101")] == ["101_1.jpg"]
    assert [path.name for path in media_files_for_group(groups["202"], "202")] == ["202_1.jpg"]


def test_shared_parent_is_inventoried_once_for_all_work_groups(tmp_path, monkeypatch):
    work_dir = tmp_path / "twitter" / "fixture" / "2024-06-27"
    work_dir.mkdir(parents=True)
    metadata_paths = []
    for tweet_id, stem in ((101, "media-a"), (202, "media-b")):
        metadata_path = work_dir / f"{stem}.json"
        metadata_path.write_text(json.dumps(_metadata(tweet_id, stem)), encoding="utf-8")
        (work_dir / f"{stem}.jpg").write_bytes(stem.encode())
        metadata_paths.append(metadata_path)

    groups, invalid = group_metadata_by_work(XProvider(), metadata_paths)
    original_iterdir = Path.iterdir
    scans = 0

    def counting_iterdir(path):
        nonlocal scans
        if path == work_dir:
            scans += 1
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)
    assert invalid == []
    assert [path.name for path in media_files_for_group(groups["101"], "101")] == ["media-a.jpg"]
    assert [path.name for path in media_files_for_group(groups["202"], "202")] == ["media-b.jpg"]
    assert scans == 1


def test_delta_filter_uses_only_increment_without_parent_scan(tmp_path, monkeypatch):
    work_dir = tmp_path / "twitter" / "fixture" / "2024-06-27"
    work_dir.mkdir(parents=True)
    metadata_path = work_dir / "remote-media-a.json"
    metadata_path.write_text(json.dumps({
        **_metadata(101, "remote-media-a"),
        "num": 1,
        "extension": "jpg",
    }), encoding="utf-8")
    old_exact = work_dir / "remote-media-a.jpg"
    old_exact.write_bytes(b"old")
    new_indexed = work_dir / "101_1.jpg"
    new_indexed.write_bytes(b"new")

    groups, invalid = group_metadata_by_work(XProvider(), [metadata_path])

    def unexpected_parent_scan(_path):
        raise AssertionError("staging delta must not scan the canonical parent")

    monkeypatch.setattr(Path, "iterdir", unexpected_parent_scan)

    assert invalid == []
    assert media_files_for_group(
        groups["101"],
        "101",
        allowed_paths={metadata_path, new_indexed},
    ) == [new_indexed]


def test_artifact_row_accepts_canonical_source_and_parsed_work_id(tmp_path):
    metadata = tmp_path / "twitter" / "fixture" / "2024-06-27" / "media-a.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("{}", encoding="utf-8")

    row = artifact_row(metadata, tmp_path, source="x", source_work_id="101")

    assert row is not None
    assert row["file_path"] == "twitter/fixture/2024-06-27/media-a.json"
    assert row["source"] == "x"
    assert row["creator_dir"] == "fixture"
    assert row["source_work_id"] == "101"


def test_artifact_row_supports_flat_gallerydl_layout_with_parsed_identity(tmp_path):
    metadata = tmp_path / "twitter" / "fixture" / "101_1.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("{}", encoding="utf-8")

    row = artifact_row(metadata, tmp_path, source="x", source_work_id="101")

    assert row is not None
    assert row["creator_dir"] == "fixture"
    assert row["source_work_id"] == "101"
