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

    def __iter__(self):
        return iter(self._rows)


class _FakeDB:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
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
        _Result(rows=[(tag_id, "pixiv", 5), (tag_id, "iwara", 2)]),
    ])

    detail = asyncio.run(tags.get_tag(tag_id, db=db))

    assert detail.id == tag_id
    assert detail.usage_count == 7
    assert detail.top_creators[0].creator_id == creator_id
    assert detail.top_creators[0].work_count == 3
    assert [(usage.source, usage.work_count) for usage in detail.source_usage] == [
        ("pixiv", 5),
        ("iwara", 2),
    ]


def test_tag_list_batches_source_usage_for_all_returned_tags():
    """Fails if source composition is omitted or queried once per tag."""
    from app.repositories.tag import TagRepository

    first_tag_id = uuid4()
    second_tag_id = uuid4()
    first_tag = SimpleNamespace(
        id=first_tag_id,
        normalized_name="arknights",
        category="general",
        created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )
    second_tag = SimpleNamespace(
        id=second_tag_id,
        normalized_name="amiya",
        category="character",
        created_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )
    db = _FakeDB([
        _Result(rows=[(first_tag, 4), (second_tag, 2)]),
        _Result(rows=[
            (first_tag_id, "pixiv", 3),
            (first_tag_id, "iwara", 1),
            (second_tag_id, "danbooru", 2),
        ]),
    ])

    tags = asyncio.run(TagRepository(db).list_all())

    assert tags[0].source_usage == [
        {"source": "pixiv", "work_count": 3},
        {"source": "iwara", "work_count": 1},
    ]
    assert tags[1].source_usage == [{"source": "danbooru", "work_count": 2}]
    assert len(db.statements) == 2
    assert db.results == []


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
