"""Tests for Meilisearch search service — unit tests that verify code structure."""

import inspect


class TestSearchService:
    def test_module_imports(self):
        """Search service uses meilisearch_python_sdk."""
        from app.services.search import SearchService, MeiliClient
        assert MeiliClient is not None

    def test_index_settings_defined(self):
        """Index settings are defined for all three indexes."""
        from app.services.search import INDEX_SETTINGS, WORKS_INDEX, CREATORS_INDEX, TAGS_INDEX
        assert WORKS_INDEX in INDEX_SETTINGS
        assert CREATORS_INDEX in INDEX_SETTINGS
        assert TAGS_INDEX in INDEX_SETTINGS
        # Each index has searchable and filterable attributes
        for idx in [WORKS_INDEX, CREATORS_INDEX, TAGS_INDEX]:
            assert "searchableAttributes" in INDEX_SETTINGS[idx]
            assert "filterableAttributes" in INDEX_SETTINGS[idx]

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
