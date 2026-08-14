import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4


class _Result:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, results):
        self.results = list(results)

    async def execute(self, _statement):
        return self.results.pop(0)


def test_tag_detail_aggregates_usage_count_instead_of_reading_orm_attribute(monkeypatch):
    from app.api import tags

    tag_id = uuid4()
    creator_id = uuid4()
    created_at = datetime(2026, 7, 26, tzinfo=timezone.utc)
    tag = SimpleNamespace(
        id=tag_id,
        normalized_name="arknights",
        category="general",
        created_at=created_at,
    )

    async def fake_get(_repository, received_tag_id):
        assert received_tag_id == tag_id
        return tag

    monkeypatch.setattr(tags.TagRepository, "get", fake_get)
    db = _FakeDB([
        _Result(scalar=7),
        _Result(rows=[(creator_id, "Test Creator", 3)]),
    ])

    detail = asyncio.run(tags.get_tag(tag_id, db=db))

    assert detail.id == tag_id
    assert detail.usage_count == 7
    assert detail.top_creators[0].creator_id == creator_id
    assert detail.top_creators[0].work_count == 3


def test_list_tags_include_all_removes_offset_and_limit(monkeypatch):
    from app.api import tags

    received = {}

    async def fake_list_all(_repository, offset, limit, *, sort_by, sort_order):
        received.update({
            "offset": offset,
            "limit": limit,
            "sort_by": sort_by,
            "sort_order": sort_order,
        })
        return []

    monkeypatch.setattr(tags.TagRepository, "list_all", fake_list_all)

    result = asyncio.run(tags.list_tags(
        offset=250,
        limit=50,
        sort_by="name",
        sort_order="asc",
        include_all=True,
        db=SimpleNamespace(),
    ))

    assert result == []
    assert received == {
        "offset": 0,
        "limit": None,
        "sort_by": "name",
        "sort_order": "asc",
    }
