from __future__ import annotations

import pytest

from app.services.search_language import (
    SearchQueryError,
    compose_search_query,
    parse_search_query,
    qualifier_catalog,
)


def test_parses_unicode_quotes_full_width_colon_and_canonicalizes():
    query = parse_search_query('蓝色 tag："blue archive" creator:"初音 ミク"', "works")
    assert query.canonical == '蓝色 tag:"blue archive" creator:"初音 ミク"'
    assert [token.value for token in query.terms] == ["蓝色"]
    assert query.values("tag") == ("blue archive",)
    assert query.targets == ("works",)


def test_same_qualifier_is_or_and_negation_is_preserved():
    query = parse_search_query("tag:a tag:b -source:twitter -source:pixiv", "works")
    assert query.values("tag") == ("a", "b")
    assert query.values("source", negated=True) == ("x", "pixiv")
    assert query.canonical == "tag:a tag:b -source:x -source:pixiv"


def test_global_domain_qualifier_narrows_compatible_targets():
    assert parse_search_query("tag:blue", "global").targets == ("works", "tags")
    assert parse_search_query("repo:abc", "global").targets == (
        "works",
        "repositories",
        "subscriptions",
    )


def test_type_alias_is_normalized():
    query = parse_search_query("type:repositories foo", "global")
    assert query.canonical == "type:repo foo"
    assert query.targets == ("repositories",)


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("wat:value", "unknown_qualifier"),
        ("tag:", "missing_value"),
        ('tag:"broken', "unclosed_quote"),
        ("-sort:name-asc", "invalid_negation"),
        ("posted:yesterday", "invalid_date"),
        ("type:repo tag:blue", "qualifier_not_available"),
        ("is:nsfw is:sfw", "conflicting_values"),
        ("tag:a -tag:a", "conflicting_values"),
        ("sort:posted-desc sort:created-desc", "duplicate_sort"),
        ("tag:a OR tag:b", "unsupported_operator"),
        ("(landscape)", "unsupported_grouping"),
    ],
)
def test_invalid_queries_are_precise(query, code):
    with pytest.raises(SearchQueryError) as error:
        parse_search_query(query, "global")
    assert error.value.diagnostic.code == code
    assert error.value.diagnostic.end >= error.value.diagnostic.start


def test_date_comparison_is_accepted():
    query = parse_search_query("posted:>=2026-07-01 created:<2026-08-01", "works")
    assert query.values("posted") == (">=2026-07-01",)


def test_compose_is_the_only_filter_mutation_surface():
    query = compose_search_query("landscape source:x", "works", key="source", value="pixiv", operation="set")
    assert query.canonical == "landscape source:pixiv"
    query = compose_search_query(query.canonical, "works", key="is", value="favorite", operation="toggle")
    assert query.canonical == "landscape source:pixiv is:favorite"
    query = compose_search_query(query.canonical, "works", key="is", value="favorite", operation="toggle")
    assert query.canonical == "landscape source:pixiv"


def test_compose_replace_group_preserves_unrelated_tokens():
    query = compose_search_query(
        "portrait is:favorite is:nsfw source:x",
        "works",
        key="is",
        value="sfw",
        operation="replace-group",
        replace_values=("nsfw", "sfw"),
    )
    assert query.canonical == "portrait is:favorite source:x is:sfw"


def test_catalog_is_scope_aware():
    works = {item["key"]: item for item in qualifier_catalog("works")}
    assert "type" not in works
    assert "tag" in works
    assert "nsfw" in works["is"]["values"]
    tasks = {item["key"]: item for item in qualifier_catalog("tasks")}
    assert "status" in tasks
    assert "tag" not in tasks
