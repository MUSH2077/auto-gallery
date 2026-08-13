"""Tests for Meilisearch search service — unit tests that verify code structure."""

import inspect
import os

import pytest


class TestSearchService:
    def test_module_imports(self):
        """Search service uses meilisearch_python_sdk."""
        from app.services.search import SearchService, MeiliClient
        assert MeiliClient is not None

    def test_index_settings_defined(self):
        """Index settings are defined for every projection."""
        from app.services.search import (
            CREATORS_INDEX,
            INDEX_SETTINGS,
            REPOSITORIES_INDEX,
            SUBSCRIPTIONS_INDEX,
            TAGS_INDEX,
            WORKS_INDEX,
        )
        assert WORKS_INDEX in INDEX_SETTINGS
        assert CREATORS_INDEX in INDEX_SETTINGS
        assert TAGS_INDEX in INDEX_SETTINGS
        assert REPOSITORIES_INDEX in INDEX_SETTINGS
        assert SUBSCRIPTIONS_INDEX in INDEX_SETTINGS
        # Each index has searchable and filterable attributes
        for idx in [WORKS_INDEX, CREATORS_INDEX, TAGS_INDEX, REPOSITORIES_INDEX, SUBSCRIPTIONS_INDEX]:
            assert "searchableAttributes" in INDEX_SETTINGS[idx]
            assert "filterableAttributes" in INDEX_SETTINGS[idx]
            assert idx.startswith(os.environ["MEILI_INDEX_PREFIX"])

    def test_test_database_requires_an_index_namespace(self):
        from app.services.search import _validate_index_namespace

        with pytest.raises(RuntimeError, match="MEILI_INDEX_PREFIX"):
            _validate_index_namespace(
                "postgresql+asyncpg://user:pass@postgres/autogallery_test",
                "",
            )
        assert _validate_index_namespace(
            "postgresql+asyncpg://user:pass@postgres/autogallery_test",
            "ag_test_safe_",
        ) == "ag_test_safe_"
        assert _validate_index_namespace(
            "postgresql+asyncpg://user:pass@postgres/autogallery",
            "",
        ) == ""

    def test_search_method_signature(self):
        from app.services.search import SearchService
        sig = inspect.signature(SearchService.search)
        params = list(sig.parameters.keys())
        assert "query" in params
        assert "offset" in params
        assert "limit" in params

    def test_reindex_method_exists(self):
        from app.services.search import SearchService
        assert hasattr(SearchService, "reindex")
        assert inspect.iscoroutinefunction(SearchService.reindex)

    def test_meili_filters_escape_values_and_preserve_or_semantics(self):
        from app.services.search import _compile_meili_filter
        from app.services.search_language import parse_search_query

        query = parse_search_query('tag:"a\\\"b" tag:landscape -source:x -source:pixiv', "works")
        filters = _compile_meili_filter(query, "works", {}, force_sfw=False)
        assert '(tags = "a\\"b" OR tags = "landscape")' in filters
        assert '(sources != "x" AND sources != "pixiv")' in filters

    def test_global_targets_are_trimmed_by_permission(self):
        from app.services.search import SearchService
        from app.services.search_language import parse_search_query

        query = parse_search_query("aurora", "global")
        assert SearchService._allowed_targets(query, {"library"}) == (
            "works",
            "creators",
            "tags",
        )
        assert SearchService._allowed_targets(query, {"subscriptions"}) == (
            "repositories",
            "subscriptions",
        )

    def test_explicit_denied_type_is_not_silently_omitted(self):
        import pytest

        from app.services.search import SearchPermissionError, SearchService
        from app.services.search_language import parse_search_query

        query = parse_search_query("type:repo aurora", "global")
        with pytest.raises(SearchPermissionError):
            SearchService._allowed_targets(query, {"library"})

    def test_upload_permission_only_opens_creator_picker_scope(self):
        import pytest

        from app.services.search import SearchPermissionError, SearchService
        from app.services.search_language import parse_search_query

        picker = parse_search_query("atlas", "creator-picker")
        assert SearchService._allowed_targets(picker, {"upload"}) == ("creators",)
        global_query = parse_search_query("type:creator atlas", "global")
        with pytest.raises(SearchPermissionError):
            SearchService._allowed_targets(global_query, {"upload"})

    def test_forced_sfw_filter_cannot_be_overridden_by_query(self):
        from app.services.search import _compile_meili_filter
        from app.services.search_language import parse_search_query

        query = parse_search_query("is:nsfw", "works")
        filters = _compile_meili_filter(query, "works", {}, force_sfw=True)
        assert "is_nsfw = true" in filters
        assert "is_nsfw = false" in filters

    def test_structured_work_lists_use_database_projection(self):
        from app.services.search import SearchService
        from app.services.search_language import parse_search_query

        for value in (
            "",
            "source:pixiv sort:posted-desc",
            "is:favorite is:sfw sort:title-asc",
            "is:trashed sort:updated-desc",
            "has:multiple-assets",
            "has:image has:video has:animation",
            "repo:00000000-0000-0000-0000-000000000001",
        ):
            assert SearchService._works_db_compatible(
                parse_search_query(value, "works")
            )



@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_removes_orphans_and_projection_audit_is_clean():
    from app.database import async_session
    from app.services.search import (
        WORKS_INDEX,
        SearchService,
        _client,
        _ensure_indexes,
        _wait_for_task,
    )

    client = _client()
    try:
        _ensure_indexes(client)
    except Exception as exc:
        pytest.skip(f"Meilisearch unavailable: {exc}")

    orphan_id = "00000000-0000-0000-0000-000000000001"
    _wait_for_task(
        client,
        client.index(WORKS_INDEX).add_documents([{"id": orphan_id, "title": "orphan"}]),
    )

    async with async_session() as db:
        service = SearchService(db)
        before = await service.audit_projection()
        assert orphan_id in before["indexes"]["works"]["stale_ids"]

        rebuilt = await service.reindex()
        assert rebuilt["status"] == "ok"

        after = await service.audit_projection()
        assert after["status"] == "ok"
        assert after["indexes"]["works"]["stale_ids"] == []
        assert after["indexes"]["works"]["missing_ids"] == []


class TestLogBuffer:
    def test_get_recent_returns_list(self):
        from app.services.log_buffer import get_recent
        entries = get_recent(10)
        assert isinstance(entries, list)

    def test_level_filter(self):
        from app.services.log_buffer import get_recent
        entries = get_recent(10, level="NONEXIST")
        assert len(entries) == 0

    def test_name_filter(self):
        from app.services.log_buffer import get_recent
        entries = get_recent(10, name_filter="nonexistentzzzz")
        assert len(entries) == 0


class TestProxyService:
    def test_get_proxy_env_disabled(self):
        from app.services.proxy import get_proxy_env
        env = get_proxy_env({"enabled": False, "http_proxy": "http://x:7890"})
        assert env == {}

    def test_get_proxy_env_enabled(self):
        from app.services.proxy import get_proxy_env
        env = get_proxy_env({"enabled": True, "http_proxy": "http://x:7890", "https_proxy": "", "no_proxy": "localhost"})
        assert "HTTP_PROXY" in env
        assert env["no_proxy"] == "localhost"
