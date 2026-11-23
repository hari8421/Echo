import tempfile
import unittest
from pathlib import Path

from echo_agent import heal
from echo_agent.config import Config
from echo_agent.db import Database
from echo_agent.docs import DocStore
from echo_agent.llm import LLMError, _with_retries
from echo_agent.memory import MemoryStore
from echo_agent.util import TMP_SUFFIX, write_atomic


class RetryTests(unittest.TestCase):
    def test_transient_failures_are_retried(self):
        attempts = []

        def call():
            attempts.append(1)
            if len(attempts) < 3:
                raise LLMError("busy", transient=True)
            return {"ok": True}

        self.assertEqual(_with_retries(call, retries=2, delay=0), {"ok": True})
        self.assertEqual(len(attempts), 3)

    def test_permanent_failures_are_not_retried(self):
        attempts = []

        def call():
            attempts.append(1)
            raise LLMError("bad model name")

        with self.assertRaises(LLMError):
            _with_retries(call, retries=3, delay=0)
        self.assertEqual(len(attempts), 1)

    def test_retry_budget_is_bounded(self):
        attempts = []

        def call():
            attempts.append(1)
            raise LLMError("down", transient=True)

        with self.assertRaises(LLMError):
            _with_retries(call, retries=2, delay=0)
        self.assertEqual(len(attempts), 3)


class HealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name) / "workspace")
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.store = DocStore(self.cfg, self.db)
        self.memory = MemoryStore(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_healthy_workspace_has_no_problems(self):
        self.store.create("Note", "hello", "misc")
        self.assertEqual(heal.check(self.cfg, self.db, self.store), [])
        self.assertEqual(heal.repair(self.cfg, self.db, self.store), [])

    def test_repair_recreates_folders_and_reindexes(self):
        self.store.create("Plan", "body", "work")
        self.db.delete_doc("plan")
        import shutil

        shutil.rmtree(self.cfg.docs_dir)
        (self.cfg.docs_dir).mkdir(parents=True)
        Path(self.cfg.docs_dir / "plan.md").write_text(
            "---\ntitle: Plan\ntags: work\n---\n\nbody\n", encoding="utf-8"
        )

        problems = heal.check(self.cfg, self.db, self.store)
        self.assertTrue(any("not indexed" in p for p in problems))

        actions = heal.repair(self.cfg, self.db, self.store)
        self.assertTrue(any("re-indexed" in a for a in actions))
        self.assertIsNotNone(self.store.get("plan"))
        self.assertEqual(heal.check(self.cfg, self.db, self.store), [])

    def test_repair_removes_leftover_temp_files(self):
        stray = self.cfg.docs_dir / (".note.md" + TMP_SUFFIX)
        stray.write_text("half written", encoding="utf-8")
        self.assertTrue(any("temporary" in p for p in heal.check(self.cfg, self.db, self.store)))

        actions = heal.repair(self.cfg, self.db, self.store)
        self.assertTrue(any("temporary" in a for a in actions))
        self.assertFalse(stray.exists())

    def test_corrupt_database_is_quarantined_and_rebuilt(self):
        self.db.close()
        self.cfg.db_path.write_bytes(b"this is not a sqlite database at all")
        db = Database(self.cfg.db_path)
        try:
            self.assertTrue(db.recovered_from)
            self.assertTrue(Path(db.recovered_from).exists())
            self.assertTrue(db.integrity())
            self.assertEqual(db.doc_count(), 0)
        finally:
            db.close()

    def test_atomic_write_leaves_no_temp_files(self):
        doc = self.store.create("Atomic", "one", "misc")
        self.store.update(doc.id, body="two")
        self.assertEqual(list(self.cfg.docs_dir.rglob("*" + TMP_SUFFIX)), [])
        self.assertIn("two", self.store.get(doc.id).body)

    def test_atomic_write_replaces_content(self):
        path = self.cfg.docs_dir / "raw.md"
        write_atomic(path, "first")
        write_atomic(path, "second")
        self.assertEqual(path.read_text(encoding="utf-8"), "second")


if __name__ == "__main__":
    unittest.main()
