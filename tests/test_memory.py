import tempfile
import unittest
from pathlib import Path

from echo_agent.config import Config
from echo_agent.db import Database
from echo_agent.memory import MemoryStore


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.db = Database(self.cfg.db_path)
        self.memory = MemoryStore(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_add_and_dedupe(self):
        first, created = self.memory.add("Prefers short emails", kind="preference")
        self.assertTrue(created)
        second, created_again = self.memory.add("prefers short emails", kind="preference")
        self.assertFalse(created_again)
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.memory.count(), 1)

    def test_search_and_filter(self):
        self.memory.add("Works at Northwind as a designer", kind="fact", tags=["work"])
        self.memory.add("Likes green tea", kind="preference", tags=["food"])
        results = self.memory.search("northwind")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "fact")
        prefs = self.memory.search("", kind="preference")
        self.assertEqual(len(prefs), 1)
        self.assertEqual(prefs[0].text, "Likes green tea")

    def test_forget(self):
        item, _ = self.memory.add("Temporary fact")
        self.assertTrue(self.memory.forget(item.id))
        self.assertFalse(self.memory.forget(item.id))
        self.assertEqual(self.memory.count(), 0)

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            self.memory.add("   ")


if __name__ == "__main__":
    unittest.main()
