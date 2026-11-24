import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from echo_agent import ui
from echo_agent.config import Config
from echo_agent.db import Database
from echo_agent.docs import DocStore
from echo_agent.memory import MemoryStore


class SessionUITests(unittest.TestCase):
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

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path, payload):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_state_has_sessions_and_config(self):
        state = self._get("/api/state")
        self.assertIn("sessions", state)
        self.assertIn("config", state)
        self.assertEqual(state["config"]["provider"], "ollama")

    def test_config_save_and_read(self):
        data = self._post("/api/config", {"model": "llama3.1", "temperature": 0.2, "auto_memory": False})
        self.assertTrue(data["saved"])
        self.assertEqual(data["model"], "llama3.1")
        env = (self.cfg.home / ".env").read_text(encoding="utf-8")
        self.assertIn("ECHO_MODEL=llama3.1", env)
        self.assertIn("ECHO_AUTO_MEMORY=0", env)

    def test_new_and_delete_session(self):
        new = self._post("/api/chat/new", {})
        self.assertIn("session", new)
        state = self._get("/api/state")
        ids = [s["id"] for s in state["sessions"]]
        self.assertIn(new["session"], ids)
        deleted = self._post("/api/chat/delete", {"session": new["session"]})
        self.assertEqual(deleted["deleted"], new["session"])


if __name__ == "__main__":
    unittest.main()
