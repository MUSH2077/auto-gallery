"""Real PostgreSQL regressions for bounded browsing and nullable cursor order."""
from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database import async_session
from app.models import Asset, AssetSource, Work, WorkSource
from app.services.search import (
    WORKS_INDEX,
    SearchService,
    _apply_sql_sort,
    _decode_random_work_cursor,
    _decode_work_cursor,
    _encode_random_work_cursor,
    _encode_work_cursor,
    _fetch_random_work_page,
    _ensure_indexes,
    _meili_shuffle_key,
    _meili_shuffle_parts,
    _shuffle_ring_start,
    _wait_for_task,
    _client,
    _work_seek_expression,
)
from app.services.search_language import parse_search_query
from app.services.work_heat import stable_shuffle_key


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
        await connection.execute(
            "CREATE TEMP TABLE works "
            "(LIKE public.works INCLUDING DEFAULTS INCLUDING GENERATED) ON COMMIT DROP"
        )
        await connection.execute("CREATE INDEX ix_works_created_id ON works (created_at, id)")
        await connection.execute("CREATE INDEX ix_works_updated_id ON works (updated_at, id)")
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.fixture
async def sort_session():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.exec_driver_sql(
            "CREATE TEMP TABLE works "
            "(LIKE public.works INCLUDING DEFAULTS INCLUDING GENERATED) ON COMMIT DROP"
        )
        await connection.exec_driver_sql(
            "CREATE INDEX ix_works_shuffle_key_id ON works (shuffle_key, id)"
        )
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


def sql(statement):
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_random_cursor_binds_query_permission_sort_seed_and_ring_phase():
    query = parse_search_query("tag:landscape sort:random", "works")
    work = Work(id=UUID(int=7), shuffle_key=1234)
    token = _encode_random_work_cursor(
        query,
        work,
        seek="after",
        force_sfw=True,
        seed=42,
        phase=1,
    )

    cursor = _decode_random_work_cursor(
        token,
        query,
        force_sfw=True,
        seed=42,
    )
    assert (cursor.seek, cursor.key, cursor.identity, cursor.phase) == (
        "after",
        1234,
        UUID(int=7),
        1,
    )
    assert 0 <= _shuffle_ring_start(0) < 2**63
    assert 0 <= _shuffle_ring_start(2**32 - 1) < 2**63

    with pytest.raises(ValueError, match="Invalid or stale works cursor"):
        _decode_random_work_cursor(token, query, force_sfw=True, seed=43)
    with pytest.raises(ValueError, match="Invalid or stale works cursor"):
        _decode_random_work_cursor(token, query, force_sfw=False, seed=42)
    with pytest.raises(ValueError, match="Invalid or stale works cursor"):
        _decode_random_work_cursor(
            token,
            parse_search_query("tag:portrait sort:random", "works"),
            force_sfw=True,
            seed=42,
        )


@pytest.mark.asyncio
async def test_random_ring_pages_wrap_once_without_duplicates_and_reverse(sort_session):
    await sort_session.execute(
        Work.__table__.insert(),
        [
            {
                "id": UUID(int=value),
                "is_nsfw": False,
                "is_ai_generated": False,
                "is_favorite": False,
            }
            for value in range(1, 14)
        ],
    )
    query = parse_search_query("sort:random", "works")
    seed = 1_234_567_890
    start = _shuffle_ring_start(seed)
    all_works = list(
        (await sort_session.execute(select(Work))).scalars()
    )
    expected = sorted(
        all_works,
        key=lambda work: (0 if work.shuffle_key >= start else 1, work.shuffle_key, work.id),
    )

    pages = []
    cursor = None
    while True:
        rows = await _fetch_random_work_page(
            sort_session,
            select(Work),
            query,
            offset=0,
            limit=4,
            force_sfw=False,
            cursor=cursor,
            seed=seed,
        )
        works = [row[0] for row in rows]
        if not works:
            break
        pages.append(works)
        last = works[-1]
        cursor = _encode_random_work_cursor(
            query,
            last,
            seek="after",
            force_sfw=False,
            seed=seed,
            phase=0 if last.shuffle_key >= start else 1,
        )

    actual = [work.id for page in pages for work in page]
    assert actual == [work.id for work in expected]
    assert len(actual) == len(set(actual)) == 13

    first = pages[2][0]
    previous = await _fetch_random_work_page(
        sort_session,
        select(Work),
        query,
        offset=0,
        limit=4,
        force_sfw=False,
        cursor=_encode_random_work_cursor(
            query,
            first,
            seek="before",
            force_sfw=False,
            seed=seed,
            phase=0 if first.shuffle_key >= start else 1,
        ),
        seed=seed,
    )
    assert [row[0].id for row in previous] == [work.id for work in pages[1]]


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


@pytest.mark.asyncio
async def test_heat_sort_is_descending_null_last_and_cursor_stable(sort_db):
    await sort_db.execute("""
        INSERT INTO works (id, is_nsfw, is_ai_generated, is_favorite, heat_score)
        SELECT lpad(to_hex(n), 32, '0')::uuid, false, false, false, heat
        FROM (VALUES (1, 90.0), (2, 90.0), (3, 40.0), (4, 40.0),
                     (5, NULL), (6, NULL)) AS seed(n, heat)
    """)
    query = parse_search_query("sort:heat-desc", "works")

    async def page(cursor=None):
        statement = select(Work.id, Work.heat_score)
        reverse = False
        if cursor:
            seek, value, identity = _decode_work_cursor(cursor, query, force_sfw=False)
            statement = statement.where(
                _work_seek_expression(query, seek=seek, value=value, identity=identity)
            )
            reverse = seek == "before"
        rows = await sort_db.fetch(
            sql(_apply_sql_sort(statement, query, Work, reverse=reverse).limit(2))
        )
        return list(reversed(rows)) if reverse else rows

    def cursor(row, seek):
        return _encode_work_cursor(
            query,
            Work(id=row["id"], heat_score=row["heat_score"]),
            seek=seek,
            force_sfw=False,
        )

    pages = [await page()]
    pages.append(await page(cursor(pages[-1][-1], "after")))
    pages.append(await page(cursor(pages[-1][-1], "after")))
    assert [[row["id"].int for row in rows] for rows in pages] == [
        [2, 1],
        [4, 3],
        [6, 5],
    ]
    assert [row["id"].int for row in await page(cursor(pages[2][0], "before"))] == [4, 3]


@pytest.mark.asyncio
async def test_db_work_projection_returns_selected_thumbnail_dimensions(monkeypatch):
    identity = uuid4()
    source_work_id = str(identity.int)
    async with async_session() as db:
        transaction = await db.begin()
        try:
            asset = Asset(
                file_path=f"/tmp/{identity}.jpg",
                file_name=f"{identity}.jpg",
                mime_type="image/jpeg",
                width=1200,
                height=800,
            )
            work = Work(
                id=identity,
                title="dimension contract",
                is_nsfw=False,
                is_ai_generated=False,
                is_favorite=False,
            )
            db.add_all([asset, work])
            await db.flush()
            work.thumbnail_asset_id = asset.id
            source = WorkSource(
                work_id=work.id,
                source="pixiv",
                source_work_id=source_work_id,
            )
            db.add(source)
            await db.flush()
            db.add(AssetSource(
                asset_id=asset.id,
                work_source_id=source.id,
                source="pixiv",
                source_asset_id=f"{source_work_id}-p0",
                ordinal=0,
                role="page",
            ))
            await db.flush()

            service = SearchService(db)
            service._parallel_hydration = False

            async def total(*_args, **_kwargs):
                return 1

            monkeypatch.setattr(service, "_cached_work_total", total)
            result = await service._search_works_db(
                parse_search_query(f"pid:pixiv/{source_work_id}", "works"),
                {},
                0,
                30,
            )

            assert result["items"][0]["thumbnail_asset_id"] == str(asset.id)
            assert result["items"][0]["thumbnail_width"] == 1200
            assert result["items"][0]["thumbnail_height"] == 800

            documents = await service._build_work_documents([work.id])
            assert documents[0]["thumbnail_width"] == 1200
            assert documents[0]["thumbnail_height"] == 800
            assert documents[0]["heat_available"] is False
            assert documents[0]["heat_score"] is None
            assert documents[0]["shuffle_key"].isdigit()
            assert len(documents[0]["shuffle_key"]) == 19
            assert all(
                isinstance(documents[0][field], int)
                for field in (
                    "shuffle_high",
                    "shuffle_low",
                    "shuffle_id_0",
                    "shuffle_id_1",
                    "shuffle_id_2",
                    "shuffle_id_3",
                )
            )
        finally:
            await transaction.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_meilisearch_heat_and_random_order_match_stable_contract():
    client = _client()
    try:
        _ensure_indexes(client)
    except Exception as exc:
        pytest.skip(f"Meilisearch unavailable: {exc}")

    identities = [UUID(int=value) for value in range(1, 10)]
    heats = [90.0, 90.0, 70.0, 50.0, 50.0, None, None, 10.0, None]
    documents = []
    for identity, heat in zip(identities, heats, strict=True):
        shuffle_key = stable_shuffle_key(identity)
        documents.append({
            "id": str(identity),
            "title": "common random contract",
            "visibility": "visible",
            "is_nsfw": False,
            "heat_available": heat is not None,
            "heat_score": heat,
            "shuffle_key": _meili_shuffle_key(shuffle_key),
            **_meili_shuffle_parts(shuffle_key, identity),
        })
    _wait_for_task(
        client,
        client.index(WORKS_INDEX).add_documents(documents),
    )

    service = SearchService(object())
    heat_query = parse_search_query("common sort:heat-desc", "works")
    heat_group = (await service._search_meili(
        heat_query,
        ["works"],
        {},
        0,
        20,
        False,
    ))["works"]
    expected_heat = sorted(
        zip(identities, heats, strict=True),
        key=lambda item: (
            item[1] is None,
            -(item[1] or 0),
            -item[0].int,
        ),
    )
    assert [item["id"] for item in heat_group["items"]] == [
        str(identity) for identity, _heat in expected_heat
    ]

    random_query = parse_search_query("common sort:random", "works")
    seed = 987_654_321
    start = _shuffle_ring_start(seed)
    expected_random = sorted(
        identities,
        key=lambda identity: (
            0 if stable_shuffle_key(identity) >= start else 1,
            stable_shuffle_key(identity),
            identity,
        ),
    )
    cursor = None
    actual_random = []
    while True:
        group = (await service._search_meili(
            random_query,
            ["works"],
            {},
            0,
            3,
            False,
            seed=seed,
            cursor=cursor,
        ))["works"]
        if not group["items"]:
            break
        actual_random.extend(UUID(item["id"]) for item in group["items"])
        cursor = group["next_cursor"]

    assert actual_random == expected_random
    assert len(actual_random) == len(set(actual_random)) == len(identities)
