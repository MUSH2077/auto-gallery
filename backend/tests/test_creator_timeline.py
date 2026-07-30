import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql


class _TimelineResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _TimelineDB:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _TimelineResult(self.rows)


def test_timeline_api_converts_iso_dates_to_utc_boundaries(monkeypatch):
    from app.api import creators
    from app.repositories import work as work_repository

    creator_id = uuid4()
    received = {}

    async def fake_timeline(self, received_creator_id, range_start, range_end):
        received.update(
            creator_id=received_creator_id,
            range_start=range_start,
            range_end=range_end,
        )
        return [], []

    monkeypatch.setattr(work_repository.WorkRepository, "get_creator_timeline", fake_timeline)
    payload = asyncio.run(
        creators.get_creator_timeline(
            creator_id,
            from_date=date(2025, 1, 1),
            to_date=date(2026, 1, 1),
            db=SimpleNamespace(),
        )
    )

    assert payload["total"] == 0
    assert received == {
        "creator_id": creator_id,
        "range_start": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "range_end": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def test_timeline_api_rejects_empty_or_reversed_ranges():
    from app.api.creators import get_creator_timeline

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            get_creator_timeline(
                uuid4(),
                from_date=date(2026, 1, 1),
                to_date=date(2026, 1, 1),
                db=SimpleNamespace(),
            )
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "invalid_date_range"


def test_timeline_repository_uses_source_event_time_and_preserves_source_counts():
    from app.repositories.work import WorkRepository

    pixiv_work = uuid4()
    x_work = uuid4()
    db = _TimelineDB(
        [
            (date(2025, 2, 14), "pixiv", 4, [pixiv_work]),
            (date(2025, 2, 14), "x", 4, [x_work]),
        ]
    )
    days, sources = asyncio.run(
        WorkRepository(db).get_creator_timeline(
            uuid4(),
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )

    assert sources == ["pixiv", "x"]
    assert days == [
        {
            "date": "2025-02-14",
            "total": 8,
            "pixiv": 4,
            "pixiv_ids": [str(pixiv_work)],
            "x": 4,
            "x_ids": [str(x_work)],
        }
    ]
    compiled = db.statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"render_postcompile": True},
    )
    sql = str(compiled)
    assert "coalesce(work_sources.posted_at, works.posted_at)" in sql
    assert "count(distinct(work_sources.id))" in sql
    assert "timezone(" in sql
    assert any(
        value == datetime(2025, 1, 1, tzinfo=timezone.utc)
        for value in compiled.params.values()
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_timeline_counts_same_day_cross_source_events_with_posted_at_fallback():
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.source_creator import SourceCreator
    from app.models.work import Work
    from app.models.work_source import WorkSource
    from app.repositories.work import WorkRepository

    try:
        async with async_session() as db:
            creator = Creator(name=f"timeline-{uuid4()}", display_name="Timeline fixture")
            db.add(creator)
            await db.flush()
            pixiv_identity = f"pixiv-{uuid4()}"
            x_identity = f"x-{uuid4()}"
            db.add_all(
                [
                    SourceCreator(
                        creator_id=creator.id,
                        source="pixiv",
                        source_creator_id=pixiv_identity,
                    ),
                    SourceCreator(
                        creator_id=creator.id,
                        source="x",
                        source_creator_id=x_identity,
                    ),
                ]
            )
            for source, identity in (("pixiv", pixiv_identity), ("x", x_identity)):
                for index in range(4):
                    work_time = datetime(2025, 2, 14, 8 + index, tzinfo=timezone.utc)
                    work = Work(title=f"{source}-{index}", posted_at=work_time)
                    db.add(work)
                    await db.flush()
                    db.add(
                        WorkSource(
                            work_id=work.id,
                            source=source,
                            source_work_id=f"{source}-{uuid4()}",
                            source_creator_id=identity,
                            posted_at=None if index == 0 else work_time,
                        )
                    )
            await db.flush()

            days, sources = await WorkRepository(db).get_creator_timeline(
                creator.id,
                datetime(2025, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

            assert sources == ["pixiv", "x"]
            assert len(days) == 1
            assert days[0]["date"] == "2025-02-14"
            assert days[0]["pixiv"] == 4
            assert days[0]["x"] == 4
            assert days[0]["total"] == 8
            await db.rollback()
    finally:
        await engine.dispose()
