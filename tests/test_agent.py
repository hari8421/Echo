import tempfile
import unittest
from pathlib import Path

from echo_agent.agent import Agent
from echo_agent.config import Config
from echo_agent.db import Database
from echo_agent.docs import DocStore
from echo_agent.memory import MemoryStore
from echo_agent.tools import ToolRunner

from tests.fakes import FakeLLM


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name), max_tool_steps=4)
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.store = DocStore(self.cfg, self.db)
        self.memory = MemoryStore(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_chat_runs_tool_then_answers(self):
        llm = FakeLLM(
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "create_doc",
                        "arguments": {
                            "title": "Kyoto trip",
                            "content": "Visit in April.",
                            "tags": "travel",
                        },
                    }
                ],
            },
            {"content": "Saved your Kyoto trip plan.", "tool_calls": []},
        )
        agent = Agent(self.cfg, self.db, self.store, self.memory, llm=llm)
        answer = agent.chat("Plan my Kyoto trip")
        self.assertIn("Saved", answer)
        doc = self.store.get("kyoto-trip")
        self.assertIsNotNone(doc)
        self.assertEqual(doc.tags, ["travel"])
        # The tool result must be fed back to the model.
        second_call_messages = llm.calls[1]["messages"]
        self.assertTrue(any(m.get("role") == "tool" for m in second_call_messages))

    def test_tool_errors_are_reported_to_model(self):
        runner = ToolRunner(self.cfg, self.db, self.store, self.memory, FakeLLM())
        result = runner.run("read_doc", {"doc": "missing"})
        self.assertIn("error", result)

    def test_extract_memories(self):
        llm = FakeLLM(
            {
                "content": '[{"text": "User is planning a Kyoto trip in April", "kind": "project", "tags": "travel"}]',
                "tool_calls": [],
            }
        )
        agent = Agent(self.cfg, self.db, self.store, self.memory, llm=llm)
        agent.history = [
            {"role": "user", "content": "I am planning a Kyoto trip in April."},
            {"role": "assistant", "content": "Nice, I saved the plan."},
        ]
        stored = agent.extract_memories()
        self.assertEqual(len(stored), 1)
        self.assertEqual(self.memory.count(), 1)
        self.assertEqual(stored[0].kind, "project")

    def test_tool_dispatch_creates_and_searches(self):
        runner = ToolRunner(self.cfg, self.db, self.store, self.memory, FakeLLM())
        created = runner.run(
            "create_doc",
            {"title": "Budget", "content": "The quarterly budget is 5000 dollars.", "tags": "finance"},
        )
        self.assertIn("budget", created)
        found = runner.run("search_docs", {"query": "quarterly budget"})
        self.assertIn("Budget", found)
        read = runner.run("read_doc", {"doc": "budget"})
        self.assertIn("5000", read)

    def test_memory_tools(self):
        runner = ToolRunner(self.cfg, self.db, self.store, self.memory, FakeLLM())
        runner.run("remember", {"text": "Prefers dark mode", "kind": "preference"})
        recalled = runner.run("recall", {"query": "dark"})
        self.assertIn("Prefers dark mode", recalled)


if __name__ == "__main__":
    unittest.main()
