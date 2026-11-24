"""Tools the agent can call, with JSON-schema definitions for the model."""

from __future__ import annotations

import json
from typing import Any, Dict

from . import organize, research, social
from . import browser as browser_mod
from .config import Config
from .db import Database
from .docs import DocStore
from .llm import LLM
from .memory import MemoryStore
from .retrieval import Retriever
from .util import trim


def _tool(name: str, description: str, properties: Dict[str, Any], required=()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
            },
        },
    }


TOOL_SPECS = [
    _tool(
        "create_doc",
        "Create a new markdown document in the user's library. Use for notes, drafts, plans and write-ups.",
        {
            "title": {"type": "string", "description": "Human readable title"},
            "content": {"type": "string", "description": "Markdown body"},
            "tags": {"type": "string", "description": "Comma-separated tags, e.g. 'work,ideas'"},
        },
        ["title"],
    ),
    _tool(
        "update_doc",
        "Replace the full body of an existing document. Use edit_doc for small changes.",
        {
            "doc": {"type": "string", "description": "Document id or exact title"},
            "content": {"type": "string", "description": "New markdown body"},
        },
        ["doc", "content"],
    ),
    _tool(
        "edit_doc",
        "Replace one exact snippet in a document. Fails if the snippet is not found.",
        {
            "doc": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        ["doc", "old_string", "new_string"],
    ),
    _tool(
        "append_to_doc",
        "Append markdown to the end of an existing document.",
        {
            "doc": {"type": "string"},
            "content": {"type": "string"},
        },
        ["doc", "content"],
    ),
    _tool(
        "read_doc",
        "Read a document's full text by id or title.",
        {"doc": {"type": "string"}},
        ["doc"],
    ),
    _tool(
        "list_docs",
        "List documents, newest first, optionally filtered by tag or a word in the title.",
        {
            "tag": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
    ),
    _tool(
        "search_docs",
        "Search document content. Use this before answering questions about the user's notes.",
        {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
        ["query"],
    ),
    _tool(
        "organize_docs",
        "Analyse the library for missing tags, duplicates, tag groups and stale docs. Set apply=true to add tags and file docs into folders.",
        {"apply": {"type": "boolean"}},
    ),
    _tool(
        "remember",
        "Save a durable fact, preference, person, project, decision or task to long-term memory.",
        {
            "text": {"type": "string", "description": "One self-contained sentence"},
            "kind": {"type": "string", "enum": ["fact", "preference", "person", "project", "decision", "task", "note"]},
            "tags": {"type": "string"},
        },
        ["text"],
    ),
    _tool(
        "recall",
        "Look up saved memories, newest first, optionally filtered by keywords.",
        {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
    ),
    _tool(
        "web_search",
        "Search the public web and return titles, URLs and snippets.",
        {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
        ["query"],
    ),
    _tool(
        "fetch_url",
        "Fetch a public web page and return its readable text.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    _tool(
        "research_topic",
        "Research a topic on the web and save a sourced markdown briefing in the library.",
        {
            "topic": {"type": "string"},
            "sources": {"type": "integer", "description": "Number of pages to read (1-6)"},
        },
        ["topic"],
    ),
    _tool(
        "draft_social_post",
        "Draft a social post (or thread) and save it to the outbox for the user to approve. Never publishes anything.",
        {
            "platform": {"type": "string", "description": "x, linkedin, bluesky, threads, instagram, facebook, reddit"},
            "topic": {"type": "string"},
            "notes": {"type": "string"},
            "tone": {"type": "string"},
            "thread": {"type": "integer", "description": "Number of posts; 0 or 1 for a single post"},
        },
        ["platform", "topic"],
    ),
    _tool("list_outbox", "List social drafts in the outbox with their status.", {}),
    _tool(
        "browser_start",
        "Start a local browser automation session (Playwright). Use before browser actions. Set headless=false to watch/intervene.",
        {
            "headless": {"type": "boolean"},
            "profile_dir": {"type": "string", "description": "Optional persistent browser profile dir to reuse logins."},
        },
    ),
    _tool("browser_status", "Get browser session status.", {}),
    _tool("browser_goto", "Navigate browser to a URL.", {"url": {"type": "string"}}, ["url"]),
    _tool("browser_snapshot", "Get page title/url/text and interactive elements with refs (e1,e2,...). Run after navigation or changes.", {"limit": {"type": "integer"}}),
    _tool("browser_click", "Click an element ref from browser_snapshot.", {"ref": {"type": "string"}}, ["ref"]),
    _tool("browser_fill", "Fill text into an element ref. Optionally submit with Enter.", {"ref": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["ref", "text"]),
    _tool("browser_press", "Press a keyboard key in the browser (Enter, Tab, Control+A etc).", {"key": {"type": "string"}}, ["key"]),
    _tool("browser_scroll", "Scroll the page vertically.", {"dy": {"type": "integer"}}),
    _tool("browser_screenshot", "Save a browser screenshot and return its path.", {"path": {"type": "string"}}),
    _tool("browser_close", "Close the browser session.", {}),
]


def _as_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ToolRunner:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        store: DocStore,
        memory: MemoryStore,
        llm: LLM = None,
    ):
        self.cfg = cfg
        self.db = db
        self.store = store
        self.memory = memory
        self.llm = llm or LLM(cfg)
        self.retriever = Retriever(cfg, db)

    def run(self, name: str, arguments: Any) -> str:
        if not isinstance(arguments, dict):
            arguments = {}
        handler = getattr(self, "_tool_" + name, None)
        if handler is None:
            return self._error("Unknown tool %r." % name)
        try:
            result = handler(**arguments)
            return json.dumps(result, ensure_ascii=False, default=str)
        except TypeError as exc:
            return self._error("Invalid arguments for %s: %s" % (name, exc))
        except Exception as exc:
            return self._error("%s: %s" % (type(exc).__name__, exc))

    @staticmethod
    def _error(message: str) -> str:
        return json.dumps({"error": message}, ensure_ascii=False)

    # -- documents ---------------------------------------------------------
    def _tool_create_doc(self, title: str, content: str = "", tags: str = ""):
        doc = self.store.create(title, content, tags)
        return {"id": doc.id, "title": doc.title, "tags": doc.tags, "path": doc.path}

    def _tool_update_doc(self, doc: str, content: str):
        updated = self.store.update(doc, body=content)
        return {"id": updated.id, "title": updated.title, "words": updated.words}

    def _tool_edit_doc(self, doc: str, old_string: str, new_string: str):
        updated = self.store.edit(doc, old_string, new_string)
        return {"id": updated.id, "title": updated.title, "updated_at": updated.updated_at}

    def _tool_append_to_doc(self, doc: str, content: str):
        updated = self.store.append(doc, content)
        return {"id": updated.id, "title": updated.title, "words": updated.words}

    def _tool_read_doc(self, doc: str):
        found = self.store.get(doc)
        if found is None:
            return {"error": "No document matches %r." % doc}
        return {
            "id": found.id,
            "title": found.title,
            "tags": found.tags,
            "updated_at": found.updated_at,
            "content": trim(found.body, 12000),
        }

    def _tool_list_docs(self, tag: str = "", query: str = "", limit: int = 30):
        docs = self.store.list_docs(tag=tag or None, limit=max(1, _as_int(limit, 30)))
        needle = (query or "").strip().lower()
        rows = []
        for doc in docs:
            if needle and needle not in (doc.title + " " + " ".join(doc.tags)).lower():
                continue
            rows.append(
                {
                    "id": doc.id,
                    "title": doc.title,
                    "tags": doc.tags,
                    "words": doc.words,
                    "updated_at": doc.updated_at,
                }
            )
        return {"count": len(rows), "documents": rows[: _as_int(limit, 30)]}

    def _tool_search_docs(self, query: str, limit: int = 8):
        hits = self.retriever.search(query, limit=max(1, min(_as_int(limit, 8), 20)))
        if not hits:
            return {"count": 0, "results": [], "note": "No matching documents."}
        return {"count": len(hits), "results": [hit.as_dict() for hit in hits]}

    def _tool_organize_docs(self, apply: bool = False):
        if _as_bool(apply):
            actions = organize.apply(self.cfg, self.db, self.store)
            report = organize.analyze(self.cfg, self.db, self.store)
            return {"applied": actions, "report": report}
        return organize.analyze(self.cfg, self.db, self.store)

    # -- memory ------------------------------------------------------------
    def _tool_remember(self, text: str, kind: str = "note", tags: str = ""):
        memory, created = self.memory.add(text, kind=kind, tags=tags, source="agent")
        return {"id": memory.id, "text": memory.text, "created": created}

    def _tool_recall(self, query: str = "", limit: int = 15):
        memories = self.memory.search(query, limit=max(1, _as_int(limit, 15)))
        return {
            "count": len(memories),
            "memories": [
                {"id": m.id, "kind": m.kind, "text": m.text, "tags": m.tags}
                for m in memories
            ],
        }

    # -- web ---------------------------------------------------------------
    def _tool_web_search(self, query: str, limit: int = 5):
        results = research.web_search(query, limit=max(1, min(_as_int(limit, 5), 10)))
        return {
            "count": len(results),
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet} for r in results
            ],
        }

    def _tool_fetch_url(self, url: str):
        page = research.fetch_page(url, max_chars=8000)
        return {"url": page.url, "title": page.title, "text": page.text}

    def _tool_research_topic(self, topic: str, sources: int = 4):
        doc, pages, failures = research.research(
            self.cfg,
            self.store,
            self.llm,
            topic,
            max_sources=max(1, min(_as_int(sources, 4), 6)),
        )
        return {
            "id": doc.id,
            "title": doc.title,
            "path": doc.path,
            "sources": [{"title": page.title, "url": page.url} for page in pages],
            "failures": failures,
        }

    # -- social ------------------------------------------------------------
    def _tool_draft_social_post(
        self,
        platform: str,
        topic: str,
        notes: str = "",
        tone: str = "professional",
        thread: int = 0,
    ):
        draft = social.draft_post(
            self.cfg,
            self.llm,
            platform=platform,
            topic=topic,
            notes=notes,
            tone=tone,
            thread=_as_int(thread, 0),
        )
        return {
            "path": str(draft.path),
            "platform": draft.platform,
            "characters": draft.chars,
            "limit": draft.limit,
            "warning": draft.warning,
            "content": draft.content,
            "note": "Saved as a draft. The user must review and approve it; nothing was published.",
        }

    def _tool_list_outbox(self):
        drafts = social.list_drafts(self.cfg)
        for draft in drafts:
            draft.pop("body", None)
        return {"count": len(drafts), "drafts": drafts}

    # -- browser -----------------------------------------------------------
    def _tool_browser_start(self, headless: bool = False, profile_dir: str = ""):
        return browser_mod.get_session().start(headless=_as_bool(headless), profile_dir=str(profile_dir or ""))

    def _tool_browser_status(self):
        return browser_mod.get_session().status()

    def _tool_browser_goto(self, url: str):
        return browser_mod.get_session().goto(url)

    def _tool_browser_snapshot(self, limit: int = 80):
        return browser_mod.get_session().snapshot(limit=_as_int(limit, 80))

    def _tool_browser_click(self, ref: str):
        return browser_mod.get_session().click(ref)

    def _tool_browser_fill(self, ref: str, text: str, submit: bool = False):
        return browser_mod.get_session().fill(ref, text, submit=_as_bool(submit))

    def _tool_browser_press(self, key: str):
        return browser_mod.get_session().press(key)

    def _tool_browser_scroll(self, dy: int = 800):
        return browser_mod.get_session().scroll(_as_int(dy, 800))

    def _tool_browser_screenshot(self, path: str = ""):
        return browser_mod.get_session().screenshot(path)

    def _tool_browser_close(self):
        return browser_mod.get_session().close()
