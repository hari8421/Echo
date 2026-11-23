"""The agent loop: system prompt, tool calling, transcript and memory extraction."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Dict, List, Optional

from .config import Config
from .db import Database
from .docs import DocStore
from .llm import LLM, LLMError
from .memory import Memory, MemoryStore
from .retrieval import Retriever
from .tools import TOOL_SPECS, ToolRunner
from .util import now, trim


def build_system_prompt(cfg: Config, db: Database, memory: MemoryStore) -> str:
    today = datetime.now().astimezone().strftime("%A, %d %B %Y")
    lines = [
        "You are Echo, a local-first personal assistant running on the user's machine.",
        "Today is %s. The user's workspace is %s." % (today, cfg.home),
        "You can read and write their markdown document library, keep long-term "
        "memory, research the web, and draft social posts.",
        "",
        "How to work:",
        "- Call tools instead of guessing about the user's documents or memories.",
        "- Search or read before answering questions about their notes, and cite "
        "document titles.",
        "- Prefer appending to a related existing document over creating duplicates.",
        "- Save anything worth keeping as a markdown document with useful tags.",
        "- Store durable facts and preferences with remember. Never store passwords, "
        "API keys or other secrets.",
        "- For social posts always use draft_social_post. It writes to an outbox for "
        "review; you cannot publish, so never claim you did.",
        "- Be concise and specific. Ask one short question only if the request is "
        "genuinely ambiguous.",
        "- Tools return JSON. On an {\"error\": ...} result, read it, change your "
        "arguments or choose another tool. Never repeat an identical failing call.",
        "- Work in small steps so a failure is cheap: create or search first, then "
        "edit. If a step fails, continue from what you already know rather than "
        "starting over.",
        "",
        "Library: %d document(s). Memory: %d item(s)." % (db.doc_count(), memory.count()),
    ]
    recent = memory.all(12)
    if recent:
        lines.append("")
        lines.append("Relevant long-term memory (newest first):")
        for item in recent:
            lines.append("- [%s] %s" % (item.kind, trim(item.text, 240)))
    return "\n".join(lines)


class Agent:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        store: DocStore,
        memory: MemoryStore,
        llm: Optional[LLM] = None,
        on_tool=None,
    ):
        self.cfg = cfg
        self.db = db
        self.store = store
        self.memory = memory
        self.llm = llm or LLM(cfg)
        self.runner = ToolRunner(cfg, db, store, memory, self.llm)
        self.retriever = Retriever(cfg, db)
        self.on_tool = on_tool
        self.session = "chat-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        self.history: List[dict] = []

    def chat(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        self.store.sync()
        self.history.append({"role": "user", "content": text})
        self.db.add_message(self.session, "user", text, now())

        seen_calls: Dict[str, int] = {}
        for _step in range(self.cfg.max_tool_steps):
            messages = [
                {"role": "system", "content": build_system_prompt(self.cfg, self.db, self.memory)}
            ] + self._trimmed_history()
            response = self.llm.chat(messages, tools=TOOL_SPECS)
            calls = response.get("tool_calls") or []
            if calls:
                self.history.append(
                    {
                        "role": "assistant",
                        "content": response.get("content") or "",
                        "tool_calls": calls,
                    }
                )
                stuck = ""
                for call in calls:
                    arguments = call.get("arguments") or {}
                    signature = "%s:%s" % (
                        call.get("name", ""),
                        json.dumps(arguments, sort_keys=True, default=str),
                    )
                    seen_calls[signature] = seen_calls.get(signature, 0) + 1
                    if seen_calls[signature] > 2:
                        stuck = call.get("name") or "?"
                        self.history.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id", ""),
                                "name": call.get("name", ""),
                                "content": json.dumps(
                                    {
                                        "error": "Identical call already ran twice; "
                                        "not executed again. Answer with what you have."
                                    }
                                ),
                            }
                        )
                        break
                    if self.on_tool:
                        self.on_tool(call.get("name", "?"), arguments)
                    output = self.runner.run(call.get("name", ""), arguments)
                    self.history.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "name": call.get("name", ""),
                            "content": output,
                        }
                    )
                if stuck:
                    answer = (
                        "I stopped because `%s` repeated the same call without "
                        "progress. Nothing was lost — tell me what to try instead, "
                        "or narrow the request." % stuck
                    )
                    self.history.append({"role": "assistant", "content": answer})
                    self.db.add_message(self.session, "assistant", answer, now())
                    return answer
                continue

            answer = (response.get("content") or "").strip()
            if not answer:
                answer = "The model returned an empty reply. Try again or rephrase."
            self.history.append({"role": "assistant", "content": answer})
            self.db.add_message(self.session, "assistant", answer, now())
            return answer

        return (
            "I stopped after %d tool steps without finishing. Try a narrower "
            "request." % self.cfg.max_tool_steps
        )

    def _trimmed_history(self) -> List[dict]:
        history = self.history[-40:]
        while history and history[0].get("role") != "user":
            history.pop(0)
        return history

    def extract_memories(self, limit: int = 5) -> List[Memory]:
        """After a session, ask the model which durable facts are worth keeping."""
        conversation = [
            message
            for message in self.history
            if message.get("role") in {"user", "assistant"} and message.get("content")
        ]
        if not conversation:
            return []
        transcript = "\n".join(
            ("User: " if message["role"] == "user" else "Echo: ")
            + trim(message["content"], 800)
            for message in conversation[-10:]
        )
        prompt = (
            "Extract durable memories from this conversation. Return ONLY a JSON "
            "array (max %d) of objects with keys text, kind, tags. kind is one of "
            "fact, preference, person, project, decision, task, note. Only include "
            "things worth remembering for months: who the user is, stable "
            "preferences, ongoing projects, recurring people, decisions and "
            "commitments. Skip transient requests and anything secret. If nothing "
            "qualifies, return [].\n\nConversation:\n%s" % (limit, transcript)
        )
        try:
            response = self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": "You extract structured memory. Output JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ]
            )
        except LLMError:
            return []
        items = _parse_json_array(response.get("content") or "")
        stored: List[Memory] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            memory, _created = self.memory.add(
                text,
                kind=str(item.get("kind") or "note"),
                tags=item.get("tags") or "",
                source="chat",
            )
            stored.append(memory)
        return stored


def _parse_json_array(text: str) -> List[dict]:
    text = text.strip()
    if not text:
        return []
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []
