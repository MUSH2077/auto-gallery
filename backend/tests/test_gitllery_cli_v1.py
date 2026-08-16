from __future__ import annotations

import pytest

from gitllery_cli.client import APIError, GitlleryClient
from gitllery_cli.gll import GLLParseError, parse_gll
from gitllery_cli.main import (
    EXIT_AUTH,
    EXIT_CONFLICT,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    main,
)
from gitllery_format import SegmentRepository


def test_gll_parses_bounded_domain_commands():
    command = parse_gll(
        '''
        version 1
        message "curate two works"
        reason "manual review"
        expect-head 2dc213de-1527-4ace-9b1a-4acaa329e444
        work f65b1af3-c004-4b15-a9d3-71e24b4a6454 trash
        work 82431af3-c004-4b15-a9d3-71e24b4a6454 tag add "landscape"
        '''
    )
    assert command.message == "curate two works"
    assert len(command.operations) == 2
    assert command.operations[1]["action"] == "tag-add"


@pytest.mark.parametrize(
    "source",
    [
        "message missing-version\nwork id trash",
        "version 1\nmessage x\ninclude other.gll",
        "version 1\nmessage x\nwork id trash\nwork id restore",
        "version 1\nmessage x\nwork id favorite maybe",
    ],
)
def test_gll_rejects_unsafe_or_contradictory_input(source):
    with pytest.raises(GLLParseError):
        parse_gll(source)


def test_gll_enforces_unique_work_limit():
    lines = ["version 1", "message bulk"]
    lines.extend(f"work work-{number} trash" for number in range(26))
    with pytest.raises(GLLParseError, match="25"):
        parse_gll("\n".join(lines))


def test_local_flag_never_enables_offline_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    status = main(
        [
            "--local",
            "commit",
            "-m",
            "test",
            "work",
            "trash",
            "00000000-0000-0000-0000-000000000001",
        ]
    )
    assert status == EXIT_USAGE
    assert list(tmp_path.iterdir()) == []


def test_init_status_log_show_and_verify_v1_repository(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    repository_root = tmp_path / "portable"
    assert main([
        "--repo", str(repository_root), "init",
        "--repository-id", "repo-fixture", "--source", "acceptance",
    ]) == EXIT_OK
    repo = SegmentRepository(repository_root / ".gitllery")
    repo.append([
        {
            "commit_id": "commit-1",
            "parent_commit_id": None,
            "created_at": "2026-08-11T00:00:00Z",
            "message": "fixture",
            "changes": [],
        }
    ])

    assert main(["--repo", str(repository_root), "status"]) == EXIT_OK
    assert main(["--repo", str(repository_root), "log", "--limit", "1"]) == EXIT_OK
    assert main(["--repo", str(repository_root), "show", "commit-1"]) == EXIT_OK
    assert main(["--repo", str(repository_root), "verify", "--deep"]) == EXIT_OK
    output = capsys.readouterr().out
    assert '"product_version": "v1"' in output
    assert "commit-1" in output


def test_remote_push_and_pull_use_explicit_v1_command_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert main(["config", "set", "url", "https://gallery.example.invalid"]) == EXIT_OK
    calls = []

    def request(self, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"status": "shadow-only"}

    monkeypatch.setattr(GitlleryClient, "request", request)
    for command in ("push", "pull"):
        assert main(["--remote", "--repo", "fixture-repo", command]) == EXIT_OK
    assert [item[1] for item in calls] == [
        "/api/v1/curation/gitllery/push",
        "/api/v1/curation/gitllery/pull",
    ]
    assert all(item[2]["json"]["product_version"] == "v1" for item in calls)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(403, EXIT_AUTH), (409, EXIT_CONFLICT), (503, EXIT_UNAVAILABLE)],
)
def test_remote_permissions_conflicts_and_failures_have_stable_exit_codes(
    tmp_path, monkeypatch, status_code, expected
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert main(["config", "set", "url", "https://gallery.example.invalid"]) == EXIT_OK

    def request(self, method, path, **kwargs):
        raise APIError(status_code, {"code": "fixture-error"})

    monkeypatch.setattr(GitlleryClient, "request", request)
    assert main(["--remote", "--repo", "fixture-repo", "push"]) == expected
