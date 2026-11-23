import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from echo_agent import ui
from echo_agent.config import Config
from echo_agent.db import Database
from echo_agent.docs import DocStore
from echo_agent.memory import MemoryStore


class UITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), llm_retries=0, llm_retry_delay=0)
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.store = DocStore(self.cfg, self.db)
        self.memory = MemoryStore(self.db)
        self.httpd = ui.serve(self.cfg, port=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.db.close()
        self.tmp.cleanup()

    def _get(self, path: str) -> Any:
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return resp.read().decode("utf-8")

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_index_page_is_served(self):
        html = self._get("/")
        self.assertIn("Echo", html)
        self.assertIn("/api/state", html)

    def test_state_reports_documents_and_memories(self):
        self.store.create("Launch brief", "Ship on Friday.", "work")
        self.memory.add("Prefers short emails", kind="preference")
        state = json.loads(self._get("/api/state"))
        self.assertEqual(state["counts"]["docs"], 1)
        self.assertEqual(state["docs"][0]["title"], "Launch brief")
        self.assertEqual(state["memories"][0]["text"], "Prefers short emails")

    def test_create_then_read_and_edit_document(self):
        status, data = self._post(
            "/api/doc/save",
            {"title": "Roadmap", "tags": "work, plan", "body": "Q1: ship."},
        )
        self.assertEqual(status, 200)
        doc_id = data["doc"]["id"]
        self.assertEqual(data["doc"]["tags"], ["work", "plan"])

        doc = json.loads(self._get("/api/doc?id=" + doc_id))["doc"]
        self.assertEqual(doc["body"].strip(), "Q1: ship.")

        status, data = self._post(
            "/api/doc/save", {"id": doc_id, "title": "Roadmap", "tags": "work", "body": "Q1: done."}
        )
        self.assertEqual(status, 200)
        self.assertIn("done", self.store.get(doc_id).body)

    def test_memory_add_and_forget(self):
        status, data = self._post("/api/memory", {"text": "Uses Neovim", "kind": "preference"})
        self.assertEqual(status, 200)
        memory_id = data["memory"]["id"]
        status, _ = self._post("/api/memory/forget", {"id": memory_id})
        self.assertEqual(status, 200)
        self.assertEqual(self.memory.count(), 0)

    def test_search_returns_hits(self):
        self.store.create("Budget", "The quarterly budget is 5000 dollars.", "finance")
        state, data = self._post("/api/search", {"query": "quarterly budget"})
        self.assertEqual(state, 200)
        self.assertEqual(data["results"][0]["doc_id"], "budget")

    def test_model_failure_is_reported_not_crashed(self):
        status, data = self._post("/api/chat", {"message": "hello"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)
        # The server is still healthy afterwards.
        self.assertIn("Echo", self._get("/"))


if __name__ == "__main__":
    unittest.main()
