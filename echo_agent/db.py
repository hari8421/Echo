"""SQLite storage: document index, chunks, memories and chat transcript."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    word_count INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks (
    doc_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding TEXT,
    PRIMARY KEY (doc_id, ord)
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'note',
    text TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    doc_id UNINDEXED,
    ord UNINDEXED,
    title,
    text,
    tokenize = 'unicode61'
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn = connect(self.path)
        self.conn.executescript(SCHEMA)
        self.fts = self._init_fts()
        self.conn.commit()

    def _init_fts(self) -> bool:
        try:
            self.conn.executescript(FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            return False

    # -- documents ---------------------------------------------------------
    def upsert_doc(
        self,
        doc_id: str,
        title: str,
        path: str,
        tags: str,
        created_at: str,
        updated_at: str,
        word_count: int,
        mtime: float,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO docs (id, title, path, tags, created_at, updated_at, word_count, mtime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                path=excluded.path,
                tags=excluded.tags,
                updated_at=excluded.updated_at,
                word_count=excluded.word_count,
                mtime=excluded.mtime
            """,
            (doc_id, title, path, tags, created_at, updated_at, word_count, mtime),
        )
        self.conn.commit()

    def update_doc_path(self, doc_id: str, path: str) -> None:
        self.conn.execute("UPDATE docs SET path=? WHERE id=?", (path, doc_id))
        self.conn.commit()

    def delete_doc(self, doc_id: str) -> None:
        self.conn.execute("DELETE FROM docs WHERE id=?", (doc_id,))
        self.conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        if self.fts:
            self.conn.execute("DELETE FROM chunks_fts WHERE doc_id=?", (doc_id,))
        self.conn.commit()

    def doc(self, doc_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()

    def doc_by_title(self, title: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM docs WHERE lower(title)=lower(?) LIMIT 1", (title,)
        ).fetchone()

    def docs(self) -> List[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM docs ORDER BY updated_at DESC").fetchall()
        )

    def replace_chunks(
        self,
        doc_id: str,
        title: str,
        chunks: Iterable[str],
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        chunks = list(chunks)
        self.conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        if self.fts:
            self.conn.execute("DELETE FROM chunks_fts WHERE doc_id=?", (doc_id,))
        for ord_, text in enumerate(chunks):
            embedding = None
            if embeddings and ord_ < len(embeddings) and embeddings[ord_]:
                embedding = _dump_vector(embeddings[ord_])
            self.conn.execute(
                "INSERT INTO chunks (doc_id, ord, text, embedding) VALUES (?, ?, ?, ?)",
                (doc_id, ord_, text, embedding),
            )
            if self.fts:
                self.conn.execute(
                    "INSERT INTO chunks_fts (doc_id, ord, title, text) VALUES (?, ?, ?, ?)",
                    (doc_id, ord_, title, text),
                )
        self.conn.commit()

    def keyword_search(self, match_query: str, limit: int) -> List[sqlite3.Row]:
        if self.fts:
            try:
                return list(
                    self.conn.execute(
                        """
                        SELECT doc_id, ord, title, text, bm25(chunks_fts) AS rank
                        FROM chunks_fts
                        WHERE chunks_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (match_query, limit),
                    ).fetchall()
                )
            except sqlite3.OperationalError:
                pass
        # Portable fallback: LIKE over all chunks.
        terms = [t for t in match_query.replace('"', " ").split() if t]
        if not terms:
            return []
        clause = " AND ".join("lower(text) LIKE ?" for _ in terms)
        params: List[Any] = ["%" + t.lower() + "%" for t in terms]
        params.append(limit)
        return list(
            self.conn.execute(
                "SELECT doc_id, ord, text, '' AS title, 0 AS rank FROM chunks "
                "WHERE " + clause + " LIMIT ?",
                params,
            ).fetchall()
        )

    def chunks_with_embeddings(self) -> List[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT doc_id, ord, text, embedding FROM chunks "
                "WHERE embedding IS NOT NULL"
            ).fetchall()
        )

    # -- memories ----------------------------------------------------------
    def add_memory(
        self, text: str, kind: str, tags: str, source: str, created_at: str
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO memories (kind, text, tags, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, text, tags, source, created_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def find_memory(self, text: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM memories WHERE lower(text)=lower(?) LIMIT 1", (text.strip(),)
        ).fetchone()

    def touch_memory(self, memory_id: int, tags: str, created_at: str) -> None:
        self.conn.execute(
            "UPDATE memories SET tags=?, created_at=? WHERE id=?",
            (tags, created_at, memory_id),
        )
        self.conn.commit()

    def memories(self, limit: int = 50) -> List[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )

    def memory(self, memory_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM memories WHERE id=?", (memory_id,)
        ).fetchone()

    def forget_memory(self, memory_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def memory_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        return int(row["n"]) if row else 0

    def doc_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM docs").fetchone()
        return int(row["n"]) if row else 0

    # -- transcript --------------------------------------------------------
    def add_message(self, session: str, role: str, content: str, created_at: str) -> None:
        self.conn.execute(
            "INSERT INTO messages (session, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session, role, content, created_at),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _dump_vector(vector: List[float]) -> str:
    return json.dumps([round(float(v), 6) for v in vector])


def load_vector(raw: str) -> List[float]:
    try:
        return [float(v) for v in json.loads(raw)]
    except (TypeError, ValueError):
        return []
