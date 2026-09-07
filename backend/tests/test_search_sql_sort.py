"""Real PostgreSQL regressions for bounded browsing and nullable cursor order."""
from __future__ import annotations

import os
from uuid import UUID

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models import Work
from app.services.search import (
    _apply_sql_sort,
    _decode_work_cursor,
    _encode_work_cursor,
    _work_seek_expression,
)
from app.services.search_language import parse_search_query


@pytest.fixture
async def sort_db():
    connection = await asyncpg.connect(
        os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"),
        timeout=5,
    )
    transaction = connection.transaction()
    await transaction.start()
    try:
        # Shadow works only on this connection, using the real schema's column
        # nullability. The two indexes match the existing browse migration.
        await connection.execute("CREATE TEMP TABLE works (LIKE public.works INCLUDING DEFAULTS) ON COMMIT DROP")
        await connection.execute("CREATE INDEX ix_works_created_id ON works (created_at, id)")
        await connection.execute("CREATE INDEX ix_works_updated_id ON works (updated_at, id)")
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()


def sql(statement):
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", ["created-desc", "created-asc", "updated-desc", "updated-asc"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_nonnullable_browse_reads_one_index_page_without_sorting_all_works(sort_db, sort, reverse):
    """Explicit wrong NULL placement must not make a 50-row page scan 4096 rows."""
    import json

    await sort_db.execute("""
        INSERT INTO works (id, is_nsfw, is_ai_generated, is_favorite, created_at, updated_at)
        SELECT lpad(to_hex(n), 32, '0')::uuid, false, false, false,
               timestamptz '2026-01-01' + (n / 4) * interval '1 second',
               timestamptz '2026-01-01' + (n / 4) * interval '1 second'
        FROM generate_series(1, 4096) AS n
    """)
    await sort_db.execute("ANALYZE works")
    query = parse_search_query(f"sort:{sort}", "works")
    base = select(Work.id, Work.created_at, Work.updated_at)
    statement = _apply_sql_sort(base, query, Work, reverse=reverse).limit(50)
    rows = await sort_db.fetch(sql(statement))
    actual = [row["id"].int for row in rows]
    ascending = sort.endswith("-asc") != reverse
    assert actual == (list(range(1, 51)) if ascending else list(range(4096, 4046, -1)))

    plan = json.loads(await sort_db.fetchval("EXPLAIN (ANALYZE, FORMAT JSON) " + sql(statement)))[0]["Plan"]
    nodes = [plan]
    for node in nodes:
        nodes.extend(node.get("Plans", []))
    examined = max(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
        for node in nodes
    )
    # Execution work, not a latency threshold or a SQL-string expectation.
    assert examined <= 50, plan

    boundary = Work(**dict(rows[-1]))
    token = _encode_work_cursor(query, boundary, seek="before" if reverse else "after", force_sfw=False)
    seek, value, identity = _decode_work_cursor(token, query, force_sfw=False)
    following = base.where(_work_seek_expression(query, seek=seek, value=value, identity=identity))
    following_rows = await sort_db.fetch(sql(_apply_sql_sort(following, query, Work, reverse=reverse).limit(50)))
    assert [row["id"].int for row in following_rows] == (
        list(range(51, 101)) if ascending else list(range(4046, 3996, -1))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["title", "posted"])
@pytest.mark.parametrize(
    ("direction", "expected_pages"),
    [("asc", [[3, 4], [1, 2], [5, 6]]), ("desc", [[2, 1], [4, 3], [6, 5]])],
)
async def test_nullable_sorts_keep_nulls_last_and_cursor_pages_round_trip(sort_db, field, direction, expected_pages):
    await sort_db.execute("""
        INSERT INTO works (id, is_nsfw, is_ai_generated, is_favorite, title, posted_at)
        SELECT lpad(to_hex(n), 32, '0')::uuid, false, false, false, title,
               CASE WHEN title IS NULL THEN NULL
                    WHEN title = 'a' THEN timestamptz '2026-01-01'
                    ELSE timestamptz '2026-01-02' END
        FROM (VALUES (1, 'b'), (2, 'b'), (3, 'a'), (4, 'a'), (5, NULL), (6, NULL)) AS seed(n, title)
    """)
    query = parse_search_query(f"sort:{field}-{direction}", "works")

    async def page(cursor=None):
        statement = select(Work.id, Work.title, Work.posted_at)
        reverse = False
        if cursor:
            seek, value, identity = _decode_work_cursor(cursor, query, force_sfw=False)
            statement = statement.where(_work_seek_expression(query, seek=seek, value=value, identity=identity))
            reverse = seek == "before"
        rows = await sort_db.fetch(sql(_apply_sql_sort(statement, query, Work, reverse=reverse).limit(2)))
        return list(reversed(rows)) if reverse else rows

    def cursor(row, seek):
        work = Work(id=row["id"], title=row["title"], posted_at=row["posted_at"])
        return _encode_work_cursor(query, work, seek=seek, force_sfw=False)

    pages = [await page()]
    for _ in range(2):
        pages.append(await page(cursor(pages[-1][-1], "after")))
    assert [[row["id"].int for row in rows] for rows in pages] == expected_pages
    assert await page(cursor(pages[-1][-1], "after")) == []
    assert await page(cursor(pages[0][0], "before")) == []
    for index in (2, 1):
        previous = await page(cursor(pages[index][0], "before"))
        assert [row["id"] for row in previous] == [UUID(int=n) for n in expected_pages[index - 1]]
