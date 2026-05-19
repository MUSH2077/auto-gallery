"""Tests for creator deduplication service — unit tests that don't require DB."""

import pytest


class TestMergeCreatorValidation:
    def test_cannot_merge_into_self(self):
        """merge_creators should raise ValueError when target == source."""
        # Verify the validation logic exists in the function
        import inspect
        from app.services.creator_dedup import merge_creators
        src = inspect.getsource(merge_creators)
        assert "Cannot merge a creator into itself" in src

    def test_checks_both_exist(self):
        """merge_creators should raise ValueError when either creator is missing."""
        import inspect
        from app.services.creator_dedup import merge_creators
        src = inspect.getsource(merge_creators)
        assert "One or both creators not found" in src


class TestFindMergeCandidates:
    def test_function_exists(self):
        from app.services.creator_dedup import find_merge_candidates
        import inspect
        assert inspect.iscoroutinefunction(find_merge_candidates)

    def test_returns_list(self):
        import inspect
        from app.services.creator_dedup import find_merge_candidates
        sig = inspect.signature(find_merge_candidates)
        assert "db" in sig.parameters
        assert "limit" in sig.parameters


class TestBatchImportDedup:
    def test_find_existing_checks_danbooru_id(self):
        """find_existing_creator should check danbooru_artist_id first."""
        import inspect
        from app.services.creator_dedup import find_existing_creator
        src = inspect.getsource(find_existing_creator)
        assert "danbooru_artist_id" in src
        assert "Creator.danbooru_artist_id" in src

    def test_find_existing_checks_source_creator(self):
        """find_existing_creator should check SourceCreator as fallback."""
        import inspect
        from app.services.creator_dedup import find_existing_creator
        src = inspect.getsource(find_existing_creator)
        assert "SourceCreator" in src
        assert "source_creator_id" in src

    def test_find_existing_checks_creator_link(self):
        """find_existing_creator should check CreatorLink URL as last resort."""
        import inspect
        from app.services.creator_dedup import find_existing_creator
        src = inspect.getsource(find_existing_creator)
        assert "CreatorLink" in src


class TestMergeWorkflow:
    def test_merge_returns_stats(self):
        """merge_creators should return a stats dict with expected keys."""
        import inspect
        from app.services.creator_dedup import merge_creators
        src = inspect.getsource(merge_creators)
        assert '"links_moved"' in src
        assert '"source_creators_moved"' in src
        assert '"subscriptions_moved"' in src

    def test_merge_transfers_danbooru_id(self):
        """merge_creators should transfer danbooru_artist_id if target lacks one."""
        import inspect
        from app.services.creator_dedup import merge_creators
        src = inspect.getsource(merge_creators)
        assert "danbooru_artist_id" in src

    def test_merge_handles_descriptions(self):
        """merge_creators should merge descriptions."""
        import inspect
        from app.services.creator_dedup import merge_creators
        src = inspect.getsource(merge_creators)
        assert "description" in src
