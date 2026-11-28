import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from echo_agent.config import Config
from echo_agent.db import Database
from echo_agent.docs import DocStore
from echo_agent.memory import MemoryStore
from echo_agent.organize import analyze, apply
from echo_agent.qa import ask_from_docs
from echo_agent.research import (
    ResearchError,
    extract_text,
    parse_bing_html,
    parse_search_html,
    web_search,
)
from echo_agent.retrieval import Retriever
from echo_agent.social import draft_post, list_drafts, read_draft, set_status

from tests.fakes import FakeLLM


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.store = DocStore(self.cfg, self.db)
        self.memory = MemoryStore(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class SocialTests(Base):
    def test_draft_saved_and_approved(self):
        llm = FakeLLM({"content": "Small teams win with boring systems.", "tool_calls": []})
        draft = draft_post(self.cfg, llm, "linkedin", "boring systems", tone="friendly")
        self.assertTrue(draft.path.exists())
        self.assertEqual(draft.warning, "")
        listed = list_drafts(self.cfg)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["status"], "draft")
        self.assertEqual(listed[0]["platform"], "linkedin")

        set_status(self.cfg, draft.path.name, "approved")
        shown = read_draft(self.cfg, draft.path.name)
        self.assertEqual(shown["status"], "approved")
        self.assertIn("boring systems", shown["body"])

    def test_over_limit_warns(self):
        llm = FakeLLM({"content": "x" * 400, "tool_calls": []}, {"content": "y" * 400, "tool_calls": []})
        draft = draft_post(self.cfg, llm, "x", "a long thought")
        self.assertIn("Over the 280", draft.warning)

    def test_unknown_platform(self):
        with self.assertRaises(Exception):
            draft_post(self.cfg, FakeLLM(), "myspace", "hello")


class OrganizeTests(Base):
    def test_analyze_and_apply(self):
        self.store.create("Quarterly budget planning", "Budget planning for the quarterly finance review.")
        self.store.create("Gardening calendar", "Tomato seedlings, compost and watering schedule for spring.")
        report = analyze(self.cfg, self.db, self.store)
        self.assertEqual(report["total"], 2)
        self.assertEqual(len(report["untagged"]), 2)
        self.assertTrue(report["untagged"][0]["suggested_tags"])

        actions = apply(self.cfg, self.db, self.store)
        self.assertTrue(actions)
        docs = self.store.list_docs(limit=10)
        self.assertTrue(all(doc.tags for doc in docs))
        # At least one document is filed into a tag folder.
        self.assertTrue(any(Path(doc.path).parent != self.cfg.docs_dir for doc in docs))
        self.assertEqual(self.db.doc_count(), 2)


class QATests(Base):
    def test_answer_cites_retrieved_chunk(self):
        self.store.create(
            "Launch brief",
            "The launch date is 14 March. Pricing is twenty dollars per seat.",
            tags=["work"],
        )
        llm = FakeLLM({"content": "The launch date is 14 March [1].", "tool_calls": []})
        answer, hits = ask_from_docs(self.cfg, Retriever(self.cfg, self.db), llm, "When is the launch?")
        self.assertIn("14 March", answer)
        self.assertTrue(hits)
        self.assertEqual(hits[0].doc_id, "launch-brief")

    def test_empty_library(self):
        answer, hits = ask_from_docs(self.cfg, Retriever(self.cfg, self.db), FakeLLM(), "anything?")
        self.assertIn("couldn't find", answer)
        self.assertEqual(hits, [])


class ResearchParsingTests(unittest.TestCase):
    def test_parse_duckduckgo_results(self):
        markup = """
        <div class="result">
          <a rel="nofollow" class="result__a"
             href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">
             Example <b>page</b>
          </a>
          <a class="result__snippet">A short snippet about the page.</a>
        </div>
        """
        results = parse_search_html(markup)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/page")
        self.assertEqual(results[0].title, "Example page")
        self.assertIn("short snippet", results[0].snippet)

    def test_parse_bing_results(self):
        markup = """
        <ol id="b_results">
          <li class="b_algo">
            <h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9wYWdl"
              >Example <strong>page</strong></a></h2>
            <div class="b_caption"><p class="b_lineclamp2">A short snippet about the page.</p></div>
          </li>
        </ol>
        """
        results = parse_bing_html(markup)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/page")
        self.assertEqual(results[0].title, "Example page")
        self.assertIn("short snippet", results[0].snippet)

    def test_web_search_falls_back_to_duckduckgo(self):
        ddg_markup = (
            '<a class="result__a" href="https://example.com/fallback">Fallback page</a>'
            '<a class="result__snippet">Fallback snippet</a>'
        )

        def fake_fetch(url, data=None):
            if "bing.com" in url:
                raise OSError("blocked")
            return ddg_markup

        with patch("echo_agent.research._fetch", side_effect=fake_fetch):
            results = web_search("a query", limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/fallback")

    def test_web_search_reports_all_provider_failures(self):
        with patch("echo_agent.research._fetch", side_effect=OSError("offline")):
            with self.assertRaises(ResearchError) as caught:
                web_search("a query")
        self.assertIn("Bing", str(caught.exception))
        self.assertIn("DuckDuckGo", str(caught.exception))

    def test_extract_text_drops_scripts(self):
        title, text = extract_text(
            "<html><head><title>Hi</title><script>bad()</script></head>"
            "<body><p>Hello world</p><p>Second line</p></body></html>"
        )
        self.assertEqual(title, "Hi")
        self.assertIn("Hello world", text)
        self.assertNotIn("bad()", text)


if __name__ == "__main__":
    unittest.main()
