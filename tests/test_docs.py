import tempfile
import unittest
from pathlib import Path

from echo_agent.config import Config
from echo_agent.db import Database
from echo_agent.docs import DocStore, chunk_text, parse_frontmatter, render_doc
from echo_agent.retrieval import Retriever


class DocStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.store = DocStore(self.cfg, self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_frontmatter_roundtrip(self):
        text = render_doc("My Note", ["work", "ideas"], "2026-01-01", "2026-01-02", "Body")
        meta, body, tags = parse_frontmatter(text)
        self.assertEqual(meta["title"], "My Note")
        self.assertEqual(body.strip(), "Body")
        self.assertEqual(tags, ["work", "ideas"])

    def test_create_read_and_collision(self):
        first = self.store.create("Weekly plan", "First body", tags=["work"])
        second = self.store.create("Weekly plan", "Second body")
        self.assertEqual(first.id, "weekly-plan")
        self.assertEqual(second.id, "weekly-plan-2")
        self.assertTrue(Path(first.path).exists())
        loaded = self.store.get(first.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.body.strip(), "First body")
        self.assertEqual(loaded.tags, ["work"])

    def test_update_append_and_edit(self):
        doc = self.store.create("Notes", "alpha beta")
        self.store.append(doc.id, "gamma")
        self.assertIn("gamma", self.store.get(doc.id).body)
        self.store.edit(doc.id, "beta", "BETA")
        self.assertIn("BETA", self.store.get(doc.id).body)
        self.store.update(doc.id, title="Renamed notes")
        self.assertEqual(self.store.get(doc.id).title, "Renamed notes")

    def test_archive_removes_from_index(self):
        doc = self.store.create("Temporary", "throwaway")
        target = self.store.archive(doc.id)
        self.assertTrue(Path(target).exists())
        self.assertIsNone(self.store.get(doc.id))
        self.assertEqual(self.db.doc_count(), 0)

    def test_search_and_external_file(self):
        self.store.create("Launch brief", "The launch date is 14 March. Pricing is twenty dollars.")
        hits = Retriever(self.cfg, self.db).search("when is the launch date", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].doc_id, "launch-brief")

        external = Path(self.cfg.docs_dir) / "external-note.md"
        external.write_text(
            "---\ntitle: External note\ntags: imported\n---\n\nA manually added file about kayaks.",
            encoding="utf-8",
        )
        self.store.sync()
        found = self.store.get("external-note")
        self.assertIsNotNone(found)
        self.assertEqual(found.tags, ["imported"])

        external.unlink()
        self.store.sync()
        self.assertIsNone(self.store.get("external-note"))

    def test_chunk_text_overlaps_long_input(self):
        body = "\n\n".join("paragraph %d %s" % (i, "word " * 120) for i in range(4))
        chunks = chunk_text(body, size=400, overlap=50)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 600 for chunk in chunks))

    def test_empty_title_rejected(self):
        with self.assertRaises(Exception):
            self.store.create("   ")


if __name__ == "__main__":
    unittest.main()
