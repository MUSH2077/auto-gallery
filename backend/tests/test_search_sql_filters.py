"""SQL work-projection query-rule regression tests."""

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models import Work
from app.services.search_language import parse_search_query
from app.services.search_sql_filters import (
    _work_filter_conditions,
    _works_db_compatible,
)


def _sql(query_text: str, *, force_sfw: bool = False) -> tuple[str, set[str]]:
    query = parse_search_query(query_text, "works")
    conditions, visibility, _ = _work_filter_conditions(
        query, {}, force_sfw=force_sfw
    )
    statement = select(Work.id).where(*conditions)
    return str(statement.compile(dialect=postgresql.dialect())), visibility


def test_default_work_projection_filters_visibility_and_optional_sfw():
    query, visibility = _sql("", force_sfw=True)
    assert visibility == set()
    assert "NOT (EXISTS" in query
    assert "works.is_nsfw IS false" in query


def test_trashed_media_query_keeps_sql_projection_rules():
    query, visibility = _sql("is:trashed has:video")
    assert visibility == {"trashed"}
    assert "work_curation_states" in query
    assert "asset_sources" in query
    assert "assets" in query
    assert _works_db_compatible(parse_search_query("is:trashed has:video", "works"))
