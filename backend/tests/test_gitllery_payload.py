from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID


def test_segment_commit_payload_is_shared_and_parent_specific():
    from app.services.gitllery.payload import build_segment_commit_payload
    from app.services.gitllery.service import GitlleryService

    commit = SimpleNamespace(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        parent_commit_id=UUID("22222222-2222-2222-2222-222222222222"),
        actor_type="user",
        actor_id="alice",
        message="curate",
        trigger="manual",
        dedupe_key="dedupe",
        reverts_commit_id=UUID("44444444-4444-4444-4444-444444444444"),
        occurred_at=datetime(2026, 1, 2, 2, 3, 4, tzinfo=timezone.utc),
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        stats=None,
        extra_metadata={"source": "test"},
    )
    changes = [
        SimpleNamespace(
            id=UUID("33333333-3333-3333-3333-333333333333"),
            subject_type="work",
            subject_id="w1",
            action="update",
            sequence=2,
            before_state=None,
            after_state={"visibility": "public"},
            diff=None,
            impact={"search": True},
        ),
        SimpleNamespace(
            id=UUID("55555555-5555-5555-5555-555555555555"),
            subject_type="asset",
            subject_id="a1",
            action="attach",
            sequence=1,
            before_state={"work_id": None},
            after_state={"work_id": "w1"},
            diff={"work_id": [None, "w1"]},
            impact=None,
        ),
    ]

    expected = build_segment_commit_payload(
        commit, changes, repository_parent_commit_id="repo-parent"
    )
    assert expected["parent_commit_id"] == "repo-parent"
    assert expected["authoritative_parent_commit_id"] == str(commit.parent_commit_id)
    assert expected["reverts_commit_id"] == str(commit.reverts_commit_id)
    assert expected["occurred_at"] == "2026-01-02T02:03:04+00:00"
    assert expected["stats"] == {}
    assert expected["metadata"] == {"source": "test"}
    assert expected["changes"][0]["diff"] == {}
    assert expected["changes"][0]["impact"] == {"search": True}
    assert [change["change_id"] for change in expected["changes"]] == [
        str(changes[0].id),
        str(changes[1].id),
    ]
    assert expected["changes"][1]["impact"] == {}

    service_payload = GitlleryService._segment_commit_payload(
        commit, changes, repository_parent_commit_id="repo-parent"
    )
    assert service_payload == expected
    assert service_payload == build_segment_commit_payload(
        commit, changes, repository_parent_commit_id="repo-parent"
    )

    from app.services.gitllery_outbox import _segment_commit_payload

    assert _segment_commit_payload(commit, changes) == build_segment_commit_payload(
        commit, changes, repository_parent_commit_id=None
    )
