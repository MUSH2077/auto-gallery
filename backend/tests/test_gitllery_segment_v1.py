from __future__ import annotations

import json

import pytest

from gitllery_format import FORMAT_ID, FORMAT_REVISION, PRODUCT_VERSION
from gitllery_format.repository import GitlleryFormatError, SegmentRepository


def _commit(number: int, parent: str | None, *, changes: int = 1) -> dict:
    return {
        "commit_id": f"commit-{number}",
        "parent_commit_id": parent,
        "message": f"commit {number}",
        "created_at": f"2026-08-11T00:00:{number:02d}+00:00",
        "changes": [
            {
                "subject_type": "work",
                "subject_id": f"work-{index}",
                "action": "work_trashed",
                "after_state": {"visibility": "trashed"},
            }
            for index in range(changes)
        ],
    }


def test_segment_repository_identifies_product_v1_and_format_revision(tmp_path):
    repo = SegmentRepository(tmp_path / ".gitllery")
    repo.initialise(repository_id="repo-1", generation="test")
    manifest = repo.append([_commit(1, None)])

    assert manifest["product_version"] == PRODUCT_VERSION == "v1"
    assert manifest["format_id"] == FORMAT_ID == "gitllery-segment"
    assert manifest["format_revision"] == FORMAT_REVISION == 1
    assert repo.verify(deep=True).ok


def test_append_is_idempotent_after_manifest_publication(tmp_path):
    repo = SegmentRepository(tmp_path / ".gitllery")
    repo.initialise(repository_id="repo-1")
    commit = _commit(1, None)
    first = repo.append([commit])
    second = repo.append([commit])

    assert second == first
    assert second["segment_count"] == 1
    assert second["commit_count"] == 1


def test_continuation_frames_are_reassembled(tmp_path):
    repo = SegmentRepository(tmp_path / ".gitllery")
    repo.initialise(repository_id="repo-1")
    repo.append([_commit(1, None, changes=61)])

    restored = list(repo.iter_commits())
    assert len(restored) == 1
    assert len(restored[0]["changes"]) == 61
    assert repo.verify(deep=True).changes == 61


def test_manifest_is_authoritative_over_unpublished_segment(tmp_path):
    repo = SegmentRepository(tmp_path / ".gitllery")
    repo.initialise(repository_id="repo-1")
    manifest = repo.read_manifest()
    orphan = {
        "product": "gitllery",
        "product_version": PRODUCT_VERSION,
        "format_id": FORMAT_ID,
        "format_revision": FORMAT_REVISION,
        "repository_id": "repo-1",
        "sequence": 1,
        "previous_segment": None,
        "frames": [],
    }
    repo._write_segment(orphan)

    assert repo.read_manifest() == manifest
    assert list(repo.iter_commits()) == []


def test_rejects_non_contiguous_repository_history(tmp_path):
    repo = SegmentRepository(tmp_path / ".gitllery")
    repo.initialise(repository_id="repo-1")
    repo.append([_commit(1, None)])
    with pytest.raises(GitlleryFormatError, match="does not follow"):
        repo.append([_commit(2, "wrong-parent")])


def test_discover_rejects_legacy_git_object_layout(tmp_path):
    legacy = tmp_path / ".gitllery"
    legacy.mkdir()
    (legacy / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    with pytest.raises(GitlleryFormatError):
        SegmentRepository.discover(tmp_path)


def test_corrupt_segment_fails_verification(tmp_path):
    repo = SegmentRepository(tmp_path / ".gitllery")
    repo.initialise(repository_id="repo-1")
    manifest = repo.append([_commit(1, None)])
    repo._segment_path(manifest["head_segment"]).write_bytes(b"not-zlib")
    result = repo.verify(deep=True)
    assert result.ok is False
    assert result.errors


def test_manifest_is_canonical_json(tmp_path):
    repo = SegmentRepository(tmp_path / ".gitllery")
    repo.initialise(repository_id="repo-1")
    raw = repo.manifest_path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw)["product_version"] == "v1"
