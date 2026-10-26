import asyncio
import importlib.util
import sys
from pathlib import Path
import unittest

from app.services import postgres_search_query as pg
pg.INDEX_SETTINGS["test_works"] = {"filterableAttributes": ["is_nsfw"], "sortableAttributes": ["created_ts"]}


class FakeRows:
    def __init__(self, rows, scalar=None):
        self.rows = rows
        self.scalar = scalar
    def mappings(self):
        return self
    def all(self):
        return self.rows
    def scalar_one_or_none(self):
        return self.scalar if self.scalar is not None else len(self.rows)


class FakeDB:
    def __init__(self, rows, scalar=None):
        self.rows = rows
        self.scalar = scalar
        self.statement = None
        self.params = None
    async def execute(self, statement, params):
        self.statement = str(statement)
        self.params = params
        if str(statement).startswith("WITH source AS MATERIALIZED"):
            return FakeRows([{**row,"index_exists":True,"total_hits":2}for row in self.rows])
        return FakeRows(self.rows, 1 if "SELECT 1 FROM search_pg_indexes" in str(statement) else 2 if "SELECT count" in str(statement) else None)


class PgSearchQueryTests(unittest.TestCase):
    def test_normalize_and_tokenize_unicode_underscore(self):
        self.assertEqual(pg.normalize_text("ＡＢＣ_Test"), "abc_test")
        self.assertEqual(pg.tokenize("ＡＢＣ_Test 星空"), ["abc", "test", "星空"])

    def test_filter_parser_supports_in_and_not_in_lists(self):
        node = pg.parse_filter('id IN ["one", "two"] AND id NOT IN ["three"]')
        self.assertEqual(node.kind, "and")
        self.assertEqual(node.left.value[1], "IN")
        self.assertEqual(node.left.value[2], ["one", "two"])

    def test_missing_index_is_explicit_backend_error(self):
        with self.assertRaises(pg.SearchBackendUnavailable):
            pg._allowed("missing_works")

    def test_filter_parser_supports_nested_boolean_and_json_values(self):
        node = pg.parse_filter('(is_nsfw = false AND tags = "blue") OR NOT (count >= 2)')
        self.assertEqual(node.kind, "or")
        self.assertEqual(node.left.kind, "and")
        self.assertEqual(node.right.kind, "not")

    def test_filter_sql_rejects_unindexed_field(self):
        node = pg.parse_filter("drop_table = 1")
        with self.assertRaises(pg.FilterSyntaxError):
            pg._filter_sql(node, {}, {"id"}, [0])

    def test_text_matching_prefix_typo_and_numeric_guard(self):
        self.assertTrue(pg.score_text("blue archive", "blue arch")[0])
        self.assertTrue(pg.score_text("landscape", "landscpae")[0])
        self.assertFalse(pg.score_text("123456", "123457")[0])
        self.assertFalse(pg.score_text("one two", "one absent")[0])

    def test_matching_strategy_last_drops_trailing_term_only(self):
        self.assertFalse(pg.score_text("blue archive", "blue missing", matching_strategy="all")[0])
        self.assertTrue(pg.score_text("blue archive", "blue missing", matching_strategy="last")[0])

    def test_search_one_applies_filter_sort_and_attributes(self):
        rows = [
            {"entity_id": "1", "document": {"id": "1", "title": "Blue Archive", "is_nsfw": False, "created_ts": 2}, "search_text": "blue archive"},
            {"entity_id": "2", "document": {"id": "2", "title": "Blue", "is_nsfw": True, "created_ts": 1}, "search_text": "blue"},
        ]
        db = FakeDB(rows)
        async def run():
            return await pg.search_one(db, "test_works", "blue", {
                "sort": ["created_ts:desc"],
                "offset": 0,
                "limit": 10,
                "attributes_to_retrieve": ["title"],
            })
        result = asyncio.run(run())
        self.assertEqual([item["id"] for item in result.hits], ["1", "2"])
        self.assertEqual(result.total_hits, 2)
        self.assertIn("ORDER BY", db.statement)
        self.assertEqual(result.hits[0], {"id": "1", "title": "Blue Archive"})


if __name__ == "__main__":
    unittest.main()
