"""Tests for Meilisearch search service — unit tests that verify code structure."""

import inspect
import os
from types import SimpleNamespace

import pytest
from tests.test_search_delivery import delivery  # noqa: F401


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

    def test_identity_alias_settings_preserve_handles_and_disable_technical_typos(self):
        from app.services.search import (
            CREATORS_INDEX,
            INDEX_SETTINGS,
            REPOSITORIES_INDEX,
            SUBSCRIPTIONS_INDEX,
            WORKS_INDEX,
        )

        for index_uid in (
            WORKS_INDEX,
            CREATORS_INDEX,
            REPOSITORIES_INDEX,
            SUBSCRIPTIONS_INDEX,
        ):
            settings = INDEX_SETTINGS[index_uid]
            assert settings["nonSeparatorTokens"] == ["_", "@"]
            assert "disableOnNumbers" not in settings["typoTolerance"]
            assert set(settings["typoTolerance"]["disableOnAttributes"]) >= {
                "alias_identities_current",
                "alias_identities_historical",
            }
            assert "alias_names_current" in settings["searchableAttributes"]
            assert "alias_identities_current" in settings["searchableAttributes"]

    @pytest.mark.asyncio
    async def test_unknown_numeric_creator_identity_never_falls_through_to_typo_search(
        self,
        monkeypatch,
    ):
        from app.services.search import SearchService

        service = SearchService(SimpleNamespace())

        async def no_exact_aliases(_parsed):
            return ()

        async def fail_if_indexed(*_args, **_kwargs):
            raise AssertionError("numeric creator identities must not use typo search")

        monkeypatch.setattr(service, "_exact_creator_aliases", no_exact_aliases)
        monkeypatch.setattr(
            service,
            "_exact_creator_aliases_for_value",
            no_exact_aliases,
        )
        monkeypatch.setattr(service, "_search_meili", fail_if_indexed)

        result = await service.search(
            "108990867",
            scope="creators",
            permissions={"library"},
        )

        assert result["groups"]["creators"] == {"total": 0, "items": []}

    def test_reference_text_search_requires_all_terms_but_works_stays_natural(self):
        from app.services.search import _matching_strategy

        assert _matching_strategy("creators") == "all"
        assert _matching_strategy("subscriptions") == "all"
        assert _matching_strategy("repositories") == "all"
        assert _matching_strategy("works") == "last"

    def test_incremental_projection_waits_for_slow_nas_meili_commits(self):
        from app.services.search import (
            MEILI_INCREMENTAL_SLICE_TIMEOUT_MS,
            MEILI_INCREMENTAL_TASK_TIMEOUT_MS,
        )

        assert MEILI_INCREMENTAL_TASK_TIMEOUT_MS >= 180_000
        assert (
            MEILI_INCREMENTAL_SLICE_TIMEOUT_MS
            >= MEILI_INCREMENTAL_TASK_TIMEOUT_MS + 60_000
        )

    def test_projection_audit_uses_meili_112_compatible_id_filter(self):
        from app.services.search import SearchService, _meili_document_ids_filter

        assert _meili_document_ids_filter(("work-a", "work-b")) == (
            'id IN ["work-a", "work-b"]'
        )
        audit_source = inspect.getsource(SearchService.audit_projection)
        assert "filter=_meili_document_ids_filter(batch_ids)" in audit_source
        assert "ids=batch_ids" not in audit_source

    def test_alias_projection_separates_fuzzy_names_from_exact_identities_and_history(self):
        from app.services.search import _alias_projection_fields

        fields = _alias_projection_fields(
            (
                SimpleNamespace(
                    value="AAkin",
                    normalized_value="aakin",
                    source="local",
                    kind="name",
                    is_current=True,
                ),
                SimpleNamespace(
                    value="user_dsnj5842",
                    normalized_value="user_dsnj5842",
                    source="pixiv",
                    kind="account",
                    is_current=True,
                ),
                SimpleNamespace(
                    value="old_handle",
                    normalized_value="old_handle",
                    source="pixiv",
                    kind="account",
                    is_current=False,
                ),
                SimpleNamespace(
                    value="aakin_old",
                    normalized_value="aakin_old",
                    source="danbooru",
                    kind="other_name",
                    is_current=False,
                ),
            )
        )

        assert fields["alias_names_current"] == ["AAkin"]
        assert fields["alias_names_historical"] == ["aakin_old"]
        assert fields["alias_identities_current"] == ["user_dsnj5842"]
        assert fields["alias_identities_historical"] == ["old_handle"]
        assert fields["alias_records"][0]["is_current"] is True
        assert fields["alias_records"][-1]["is_current"] is False

    def test_fuzzy_alias_explanation_prefers_current_prefix_and_hides_projection_fields(self):
        from app.services.search import _decorate_alias_hit

        hit = {
            "id": "creator-1",
            "name": "aakin5349",
            "display_name": "AAkin",
            "alias_names_current": ["long_creator_alias"],
            "alias_names_historical": ["long_old_alias"],
            "alias_identities_current": ["user_dsnj5842"],
            "alias_identities_historical": [],
            "alias_records": [
                {
                    "creator_id": "creator-1",
                    "value": "long_creator_alias",
                    "normalized_value": "long_creator_alias",
                    "source": "danbooru",
                    "kind": "other_name",
                    "is_current": True,
                },
                {
                    "creator_id": "creator-1",
                    "value": "long_old_alias",
                    "normalized_value": "long_old_alias",
                    "source": "danbooru",
                    "kind": "other_name",
                    "is_current": False,
                },
            ],
        }

        _decorate_alias_hit(hit, "long_creat")

        assert hit["matched_identity"] == {
            "creator_id": "creator-1",
            "value": "long_creator_alias",
            "source": "danbooru",
            "kind": "other_name",
            "is_current": True,
            "match_type": "prefix",
        }
        assert "alias_records" not in hit
        assert "alias_names_current" not in hit

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

    def test_reference_indexes_default_to_stable_name_order(self):
        """Changing the subscription default back to timestamps must fail."""
        from app.services.search import _meili_sort
        from app.services.search_language import parse_search_query

        for scope in ("creators", "subscriptions"):
            assert _meili_sort(
                parse_search_query("", scope),
                scope,
            ) == ["name_sort:asc", "id:asc"]

    def test_parallel_work_hydration_reserves_a_control_connection(self, monkeypatch):
        from app.services import search

        monkeypatch.setattr(search.settings, "db_pool_size", 1)
        monkeypatch.setattr(search.settings, "db_max_overflow", 1)
        assert search._parallel_work_hydration_supported() is False

        monkeypatch.setattr(search.settings, "db_pool_size", 4)
        monkeypatch.setattr(search.settings, "db_max_overflow", 1)
        assert search._parallel_work_hydration_supported() is True

    def test_meili_filters_escape_values_and_preserve_or_semantics(self):
        from app.services.search import _compile_meili_filter
        from app.services.search_language import parse_search_query

        query = parse_search_query('tag:"a\\\"b" tag:landscape -source:x -source:pixiv', "works")
        filters = _compile_meili_filter(query, "works", {}, force_sfw=False)
        assert '(tags = "a\\"b" OR tags = "landscape")' in filters
        assert '(sources != "x" AND sources != "pixiv")' in filters

    def test_source_identity_meili_filters_keep_source_and_id_paired(self):
        from app.services.search import _compile_meili_filter
        from app.services.search_language import parse_search_query

        query = parse_search_query(
            "uid:pixiv/123 uid:x/123 pid:pixiv/456",
            "works",
        )
        filters = _compile_meili_filter(query, "works", {}, force_sfw=False)
        assert '(source_creator_keys = "pixiv/123" OR source_creator_keys = "x/123")' in filters
        assert '(source_work_keys = "pixiv/456")' in filters

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
            "uid:pixiv/1980643",
            "pid:pixiv/38362603",
            'url:"https://www.pixiv.net/artworks/38362603"',
        ):
            assert SearchService._works_db_compatible(
                parse_search_query(value, "works")
            )



@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_removes_orphans_and_projection_audit_is_clean(delivery):
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
        assert rebuilt["status"] == "pending"
        from app.services.search_delivery import run_delivery_slice
        from app.services.search_rebuild import rebuild_status
        from tests.test_search_delivery import ready_receipt
        import asyncio
        for _ in range(300):
            await run_delivery_slice()
            await ready_receipt()
            completed = await rebuild_status(rebuilt["build_id"])
            if completed["status"] != "pending":
                break
            await asyncio.sleep(.05)
        assert completed["status"] == "ok", completed

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
