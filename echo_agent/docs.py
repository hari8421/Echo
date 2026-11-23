"""Markdown document store: file IO, frontmatter, indexing and searching."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from . import embeddings
from .config import Config
from .db import Database
from .util import now, normalize_tags, slugify, word_count

MARKER = "\n---\n"


class DocError(Exception):
    pass


@dataclass
class Doc:
    id: str
    title: str
    body: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    path: str = ""
    words: int = 0


def parse_frontmatter(text: str) -> Tuple[dict, str, List[str]]:
    meta = {}
    body = text
    if text.startswith("---\n"):
        end = text.find(MARKER, 4)
        if end != -1:
            header = text[4:end]
            body = text[end + len(MARKER) :]
            for line in header.splitlines():
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                meta[key.strip().lower()] = value.strip()
    tags = normalize_tags(meta.get("tags", ""))
    return meta, body.lstrip("\n"), tags


def render_doc(title: str, tags: List[str], created: str, updated: str, body: str) -> str:
    header = [
        "---",
        "title: %s" % title.replace("\n", " ").strip(),
        "tags: %s" % ", ".join(tags),
        "created: %s" % created,
        "updated: %s" % updated,
        "---",
        "",
    ]
    return "\n".join(header) + body.strip() + "\n"


def chunk_text(body: str, size: int = 1000, overlap: int = 150) -> List[str]:
    """Split markdown into overlapping, paragraph-aware chunks."""
    body = body.strip()
    if not body:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    chunks: List[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > size:
            if current:
                chunks.append(current.strip())
                current = ""
            words = paragraph.split()
            buf = ""
            for word in words:
                candidate = (buf + " " + word).strip()
                if len(candidate) > size and buf:
                    chunks.append(buf.strip())
                    buf = buf[-overlap:] + " " + word if overlap else word
                else:
                    buf = candidate
            if buf.strip():
                chunks.append(buf.strip())
            continue
        candidate = (current + "\n\n" + paragraph).strip()
        if len(candidate) > size and current:
            chunks.append(current.strip())
            tail = current[-overlap:] if overlap else ""
            current = (tail + "\n\n" + paragraph).strip()
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks or [body[:size]]


class DocStore:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        cfg.ensure_dirs()

    # -- indexing ----------------------------------------------------------
    def sync(self, force: bool = False) -> int:
        """Index new/changed files and forget removed ones. Returns changed count."""
        self.cfg.ensure_dirs()
        seen = set()
        changed = 0
        paths = sorted(
            p for p in self.cfg.docs_dir.rglob("*.md") if not p.name.startswith(".")
        )
        for path in paths:
            doc_id = path.stem
            if doc_id in seen:
                continue
            seen.add(doc_id)
            if self._index_path(path, doc_id, force=force):
                changed += 1
        for row in self.db.docs():
            if row["id"] not in seen:
                self.db.delete_doc(row["id"])
                changed += 1
        return changed

    def _index_path(self, path: Path, doc_id: str, force: bool = False) -> bool:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            mtime = path.stat().st_mtime
        except OSError:
            return False
        meta, body, tags = parse_frontmatter(text)
        title = (meta.get("title") or doc_id.replace("-", " ").title()).strip()
        row = self.db.doc(doc_id)
        if row and not force and abs(float(row["mtime"]) - mtime) < 1e-6:
            if row["path"] != str(path):
                self.db.update_doc_path(doc_id, str(path))
            return False
        created = meta.get("created") or (row["created_at"] if row else now())
        updated = meta.get("updated") or (row["updated_at"] if row else now())
        self.db.upsert_doc(
            doc_id=doc_id,
            title=title,
            path=str(path),
            tags=", ".join(tags),
            created_at=created,
            updated_at=updated,
            word_count=word_count(body),
            mtime=mtime,
        )
        chunks = chunk_text(body)
        vectors = embeddings.embed_texts(self.cfg, chunks)
        self.db.replace_chunks(doc_id, title, chunks, vectors)
        return True

    def rebuild(self) -> int:
        for row in self.db.docs():
            self.db.delete_doc(row["id"])
        return self.sync(force=True)

    # -- lookup ------------------------------------------------------------
    def resolve(self, ref: str) -> Optional[Doc]:
        self.sync()
        ref = (ref or "").strip()
        if not ref:
            return None
        row = self.db.doc(ref) or self.db.doc_by_title(ref)
        if row is None:
            slug = slugify(ref)
            row = self.db.doc(slug) or self.db.doc_by_title(slug)
        if row is None:
            return None
        return self._load(row)

    def get(self, doc_id: str) -> Optional[Doc]:
        return self.resolve(doc_id)

    def _load(self, row) -> Optional[Doc]:
        path = Path(row["path"])
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        meta, body, tags = parse_frontmatter(text)
        return Doc(
            id=row["id"],
            title=meta.get("title") or row["title"],
            body=body,
            tags=tags or normalize_tags(row["tags"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            path=str(path),
            words=word_count(body),
        )

    def list_docs(self, tag: Optional[str] = None, limit: int = 100) -> List[Doc]:
        self.sync()
        docs: List[Doc] = []
        for row in self.db.docs():
            tags = normalize_tags(row["tags"])
            if tag and tag.lower().strip("-") not in tags:
                continue
            docs.append(
                Doc(
                    id=row["id"],
                    title=row["title"],
                    tags=tags,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    path=row["path"],
                    words=int(row["word_count"]),
                )
            )
            if len(docs) >= limit:
                break
        return docs

    # -- mutations ---------------------------------------------------------
    def create(self, title: str, body: str = "", tags=None) -> Doc:
        title = (title or "").strip()
        if not title:
            raise DocError("A document needs a title.")
        self.cfg.ensure_dirs()
        doc_id = self._unique_id(slugify(title))
        stamp = now()
        clean_tags = normalize_tags(tags)
        path = self.cfg.docs_dir / (doc_id + ".md")
        path.write_text(
            render_doc(title, clean_tags, stamp, stamp, body or ""), encoding="utf-8"
        )
        self._index_path(path, doc_id, force=True)
        return Doc(
            id=doc_id,
            title=title,
            body=(body or "").strip(),
            tags=clean_tags,
            created_at=stamp,
            updated_at=stamp,
            path=str(path),
            words=word_count(body or ""),
        )

    def _unique_id(self, base: str) -> str:
        candidate = base
        n = 2
        while (self.cfg.docs_dir / (candidate + ".md")).exists() or self.db.doc(candidate):
            candidate = "%s-%d" % (base, n)
            n += 1
        return candidate

    def update(
        self,
        ref: str,
        body: Optional[str] = None,
        title: Optional[str] = None,
        tags=None,
    ) -> Doc:
        doc = self.resolve(ref)
        if doc is None:
            raise DocError("No document matches %r." % ref)
        path = Path(doc.path)
        new_title = (title or doc.title).strip()
        new_tags = doc.tags if tags is None else normalize_tags(tags)
        new_body = doc.body if body is None else body.strip()
        path.write_text(
            render_doc(new_title, new_tags, doc.created_at, now(), new_body),
            encoding="utf-8",
        )
        self._index_path(path, doc.id, force=True)
        updated = self.get(doc.id)
        if updated is None:
            raise DocError("Document vanished during update.")
        return updated

    def append(self, ref: str, content: str) -> Doc:
        doc = self.resolve(ref)
        if doc is None:
            raise DocError("No document matches %r." % ref)
        addition = content.strip()
        body = doc.body.rstrip()
        body = (body + "\n\n" + addition).strip() if body else addition
        return self.update(doc.id, body=body)

    def edit(self, ref: str, old: str, new: str) -> Doc:
        doc = self.resolve(ref)
        if doc is None:
            raise DocError("No document matches %r." % ref)
        if old not in doc.body:
            raise DocError("That exact text is not present in %s." % doc.title)
        return self.update(doc.id, body=doc.body.replace(old, new, 1))

    def add_tags(self, ref: str, tags) -> Doc:
        doc = self.resolve(ref)
        if doc is None:
            raise DocError("No document matches %r." % ref)
        merged = doc.tags + [t for t in normalize_tags(tags) if t not in doc.tags]
        return self.update(doc.id, tags=merged)

    def archive(self, ref: str) -> Path:
        doc = self.resolve(ref)
        if doc is None:
            raise DocError("No document matches %r." % ref)
        self.cfg.ensure_dirs()
        source = Path(doc.path)
        stamp = now().replace(":", "").replace("+", "_")
        target = self.cfg.archive_dir / ("%s-%s.md" % (doc.id, stamp))
        shutil.move(str(source), str(target))
        self.db.delete_doc(doc.id)
        return target

    def move(self, doc: Doc, folder: str) -> Optional[Path]:
        """Move a document into docs/<folder>/ and keep the index in sync."""
        folder_slug = slugify(folder)
        if not folder_slug:
            return None
        target_dir = self.cfg.docs_dir / folder_slug
        target_dir.mkdir(parents=True, exist_ok=True)
        source = Path(doc.path)
        target = target_dir / source.name
        if source.resolve() == target.resolve():
            return source
        if target.exists() and target.resolve() != source.resolve():
            return None
        shutil.move(str(source), str(target))
        self.db.update_doc_path(doc.id, str(target))
        return target
