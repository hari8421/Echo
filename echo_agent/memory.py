"""Long-term memory: durable facts, preferences, people and decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .db import Database
from .util import now, normalize_tags

KINDS = {"fact", "preference", "person", "project", "decision", "note", "task"}


@dataclass
class Memory:
    id: int
    text: str
    kind: str
    tags: List[str]
    source: str
    created_at: str


def _to_memory(row) -> Memory:
    return Memory(
        id=int(row["id"]),
        text=row["text"],
        kind=row["kind"],
        tags=normalize_tags(row["tags"]),
        source=row["source"],
        created_at=row["created_at"],
    )


class MemoryStore:
    def __init__(self, db: Database):
        self.db = db

    def add(
        self, text: str, kind: str = "note", tags=None, source: str = ""
    ) -> tuple:
        """Store a memory; returns (Memory, created_bool). Deduplicates by text."""
        text = re.sub(r"\s+", " ", (text or "").strip())
        if not text:
            raise ValueError("Cannot remember an empty note.")
        kind = (kind or "note").strip().lower()
        if kind not in KINDS:
            kind = "note"
        tag_list = normalize_tags(tags)
        existing = self.db.find_memory(text)
        stamp = now()
        if existing:
            merged = normalize_tags(list(normalize_tags(existing["tags"])) + tag_list)
            self.db.touch_memory(int(existing["id"]), ", ".join(merged), stamp)
            row = self.db.memory(int(existing["id"]))
            return _to_memory(row), False
        memory_id = self.db.add_memory(text, kind, ", ".join(tag_list), source, stamp)
        return (
            Memory(
                id=memory_id,
                text=text,
                kind=kind,
                tags=tag_list,
                source=source,
                created_at=stamp,
            ),
            True,
        )

    def all(self, limit: int = 50) -> List[Memory]:
        return [_to_memory(row) for row in self.db.memories(limit)]

    def search(self, query: str = "", kind: Optional[str] = None, limit: int = 20) -> List[Memory]:
        rows = self.db.memories(500)
        tokens = [
            token
            for token in re.findall(r"[A-Za-z0-9_]+", (query or "").lower())
            if len(token) > 1
        ]
        results: List[Memory] = []
        for row in rows:
            memory = _to_memory(row)
            if kind and memory.kind != kind:
                continue
            haystack = (memory.text + " " + " ".join(memory.tags)).lower()
            if tokens and not all(token in haystack for token in tokens):
                continue
            results.append(memory)
            if len(results) >= limit:
                break
        return results

    def forget(self, memory_id: int) -> bool:
        return self.db.forget_memory(memory_id)

    def count(self) -> int:
        return self.db.memory_count()
