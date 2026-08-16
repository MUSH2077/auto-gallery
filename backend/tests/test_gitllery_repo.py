from pathlib import Path

import pytest

from app.services.gitllery.repo import GitlleryRepo, GITLLERY_DIRNAME
from app.services.gitllery.objects import hash_payload


def _repo(tmp_path: Path) -> GitlleryRepo:
    return GitlleryRepo(tmp_path, "pixiv", "七诗")


def test_init_creates_git_isomorphic_layout(tmp_path: Path):
    repo = _repo(tmp_path)
    assert repo.exists() is False
    repo.init({"schema_version": 1, "source": "pixiv"}, "pixiv / 七诗 curation history")
    assert repo.exists() is True
    base = tmp_path / "pixiv" / "七诗" / GITLLERY_DIRNAME
    assert (base / "HEAD").read_text().strip() == "ref: refs/heads/main"
    assert (base / "objects").is_dir()
    assert (base / "refs" / "heads").is_dir()
    assert repo.read_config()["source"] == "pixiv"
    assert repo.head_commit() is None


def test_set_head_moves_ref_and_appends_reflog(tmp_path: Path):
    repo = _repo(tmp_path)
    repo.init({"schema_version": 1}, "d")
    repo.set_head("a" * 64, actor="admin", message="trash 1 work")
    assert repo.head_commit() == "a" * 64
    repo.set_head("b" * 64, actor="admin", message="restore 1 work")
    assert repo.head_commit() == "b" * 64
    log = repo.read_reflog()
    assert len(log) == 2
    assert log[0]["old"] == "0" * 64 and log[0]["new"] == "a" * 64
    assert log[1]["old"] == "a" * 64 and log[1]["new"] == "b" * 64
    assert log[1]["message"] == "restore 1 work"


def test_index_round_trip(tmp_path: Path):
    repo = _repo(tmp_path)
    repo.init({"schema_version": 1}, "d")
    assert repo.read_index() == {"head": None, "tree": None, "entities": {}}
    repo.write_index({"head": "c" * 64, "tree": "d" * 64, "entities": {"work/1": {"visibility": "trashed"}}})
    assert repo.read_index()["entities"]["work/1"]["visibility"] == "trashed"


def test_path_containment_rejects_escape(tmp_path: Path):
    with pytest.raises(ValueError):
        GitlleryRepo(tmp_path, "pixiv", "../../etc")


def test_projected_db_commit_ids_walks_chain(tmp_path: Path):
    repo = _repo(tmp_path)
    repo.init({"schema_version": 1}, "d")
    c1 = repo.objects.write({"type": "commit", "parent": None, "db_commit_id": "db-1"})
    c2 = repo.objects.write({"type": "commit", "parent": c1, "db_commit_id": "db-2"})
    repo.set_head(c2, actor="admin", message="m")
    assert repo.projected_db_commit_ids() == {"db-1", "db-2"}


def _seed_v1_index(repo: GitlleryRepo):
    blob_1 = repo.objects.write(
        {"type": "blob", "subject_type": "work", "subject_id": "1",
         "state": {"visibility": "trashed"}}
    )
    blob_2 = repo.objects.write(
        {"type": "blob", "subject_type": "work", "subject_id": "2",
         "state": {"visibility": "visible"}}
    )
    tree = repo.objects.write(
        {"type": "tree", "entries": {"work/1": blob_1, "work/2": blob_2}}
    )
    commit = repo.objects.write(
        {"type": "commit", "tree": tree, "parent": None,
         "db_commit_id": "db-v1"}
    )
    repo.set_head(commit, actor="system", message="v1")
    repo.write_index({
        "head": commit,
        "tree": tree,
        "last_db_commit_id": "db-v1",
        "entities": {
            "work/1": {"visibility": "trashed"},
            "work/2": {"visibility": "visible"},
        },
        "tree_entries": {"work/1": blob_1, "work/2": blob_2},
    })


def test_v2_overlay_creation_does_not_read_flat_v1_index(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    repo.init({"schema_version": 1}, "d")
    _seed_v1_index(repo)

    monkeypatch.setattr(
        repo,
        "_read_legacy_index",
        lambda: (_ for _ in ()).throw(AssertionError("flat index read")),
    )
    manifest = repo.projection_manifest_for_update()

    assert manifest["schema_version"] == 2
    assert manifest["base"]["index"] == "index.json"
    assert manifest["last_db_commit_id"] == "db-v1"


def test_read_index_dual_reads_v1_base_and_v2_tombstones(tmp_path):
    repo = _repo(tmp_path)
    repo.init({"schema_version": 1}, "d")
    _seed_v1_index(repo)
    manifest = repo.projection_manifest_for_update()

    payloads = []
    for entity_key, state in {
        "work/1": {"visibility": "visible"},
        "work/2": None,
    }.items():
        prefix = repo.shard_for_entity(entity_key)
        current = next(
            (item for item in payloads if item["prefix"] == prefix),
            None,
        )
        if current is None:
            current = {
                "type": "tree-shard-v2",
                "prefix": prefix,
                "entities": {},
                "entries": {},
            }
            payloads.append(current)
        current["entities"][entity_key] = state
        current["entries"][entity_key] = None
    repo.objects.write_many(payloads)
    manifest["shards"] = {
        payload["prefix"]: hash_payload(payload) for payload in payloads
    }
    repo.write_projection_manifest(manifest)

    index = repo.read_index()

    assert index["entities"]["work/1"] == {"visibility": "visible"}
    assert "work/2" not in index["entities"]


def test_explicit_v1_migration_compacts_overlay_and_keeps_v1_file(tmp_path):
    repo = _repo(tmp_path)
    repo.init({"schema_version": 1}, "d")
    _seed_v1_index(repo)

    result = repo.migrate_v1_to_v2()

    expected_shards = len({
        repo.shard_for_entity("work/1"),
        repo.shard_for_entity("work/2"),
    })
    assert result == {
        "migrated": True,
        "entities": 2,
        "shards": expected_shards,
    }
    assert repo.read_projection_manifest()["base"] is None
    assert repo.read_config()["schema_version"] == 2
    assert (repo.root / "index.json").exists()
    assert set(repo.read_index()["entities"]) == {"work/1", "work/2"}
