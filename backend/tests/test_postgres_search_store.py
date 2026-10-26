import unittest
from app.services.postgres_search_store import normalize_text, tokenize
_IMPORT_ERROR = None


class PgSearchStorePureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest("backend dependencies are provided inside the backend test image")
    def test_normalization_and_tokenization(self):
        self.assertEqual(normalize_text("ＡＢＣ_Test"), "abc_test")
        tokens = tokenize("女 青_猫")
        self.assertIn("女", tokens)
        self.assertIn("青", tokens)
        self.assertIn("女", tokens)
        self.assertNotIn("女青", tokens)
        self.assertNotIn("青猫", tokens)
        self.assertIn("青猫", tokenize("青猫"))

    def test_cjk_grams_do_not_cross_separators(self):
        tokens = tokenize("女，青")
        self.assertNotIn("女青", tokens)
        self.assertIn("女", tokens)
        self.assertIn("青", tokens)


if __name__ == "__main__":
    unittest.main()
