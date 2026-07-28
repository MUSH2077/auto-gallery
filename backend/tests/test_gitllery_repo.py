from pathlib import Path

import pytest

from app.services.gitllery.repo import GitlleryRepo, GITLLERY_DIRNAME


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
