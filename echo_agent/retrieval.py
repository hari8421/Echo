"""Hybrid retrieval over indexed document chunks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from . import embeddings
from .config import Config
from .db import Database, load_vector
from .util import trim

STOPWORDS = {
    "the", "and", "for", "with", "what", "when", "where", "who", "why",
    "how", "does", "did", "was", "were", "are", "is", "a", "an", "of", "to",
    "in", "on", "my", "me", "i", "you", "your", "about", "tell", "summarize",
    "summarise", "explain", "please", "from", "that", "this", "it", "be",
    "can", "should", "would", "could", "have", "has", "had", "do", "not",
}


def fts_query(query: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_]+", query.lower())
    seen: List[str] = []
    for token in tokens:
        if token in STOPWORDS or len(token) < 2 or token in seen:
            continue
        seen.append(token)
    return " OR ".join('"%s"' % token for token in seen)


@dataclass
class Hit:
    doc_id: str
    title: str
    ord: int
    text: str
    score: float = 0.0

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "snippet": trim(self.text.replace("\n", " "), 300),
            "score": round(self.score, 4),
        }


class Retriever:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    def search(self, query: str, limit: int = 8) -> List[Hit]:
        query = (query or "").strip()
        if not query:
            return []
        hits: Dict[Tuple[str, int], Hit] = {}

        match = fts_query(query)
        rows = self.db.keyword_search(match, max(limit * 4, 24)) if match else []
        for pos, row in enumerate(rows):
            key = (row["doc_id"], int(row["ord"]))
            title = row["title"] or self._title(row["doc_id"])
            hits[key] = Hit(
                doc_id=row["doc_id"],
                title=title,
                ord=int(row["ord"]),
                text=row["text"],
                score=1.0 / (60 + pos),
            )

        for doc_id, ord_, cos in self._semantic(query, limit * 4):
            key = (doc_id, ord_)
            if key in hits:
                hits[key].score += cos
            else:
                hits[key] = Hit(
                    doc_id=doc_id,
                    title=self._title(doc_id),
                    ord=ord_,
                    text=self._chunk_text(doc_id, ord_),
                    score=cos,
                )

        ranked = sorted(hits.values(), key=lambda hit: hit.score, reverse=True)
        return ranked[:limit]

    def _title(self, doc_id: str) -> str:
        row = self.db.doc(doc_id)
        return row["title"] if row else doc_id

    def _chunk_text(self, doc_id: str, ord_: int) -> str:
        row = self.db.conn.execute(
            "SELECT text FROM chunks WHERE doc_id=? AND ord=?", (doc_id, ord_)
        ).fetchone()
        return row["text"] if row else ""

    def _semantic(self, query: str, limit: int) -> List[Tuple[str, int, float]]:
        if not self.cfg.embed_model:
            return []
        rows = self.db.chunks_with_embeddings()
        if not rows:
            return []
        query_vectors = embeddings.embed_texts(self.cfg, [query])
        if not query_vectors:
            return []
        query_vector = query_vectors[0]
        scored: List[Tuple[str, int, float]] = []
        for row in rows:
            vector = load_vector(row["embedding"])
            score = embeddings.cosine(query_vector, vector)
            if score > 0.15:
                scored.append((row["doc_id"], int(row["ord"]), score))
        scored.sort(key=lambda item: item[2], reverse=True)
        return scored[:limit]
