"""Keep the document library tidy: tags, folders, duplicates and stale notes."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

from .config import Config
from .db import Database
from .docs import DocStore
from .util import slugify

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "will",
    "your", "about", "into", "when", "what", "were", "they", "them", "then",
    "than", "there", "their", "which", "while", "would", "could", "should",
    "been", "being", "also", "more", "most", "some", "such", "only", "just",
    "over", "under", "after", "before", "because", "between", "each", "other",
    "these", "those", "where", "very", "much", "many", "make", "made", "like",
    "using", "used", "use", "can", "not", "you", "are", "was", "how", "why",
    "who", "all", "any", "our", "out", "but", "its", "it's", "we", "i",
}

STALE_DAYS = 90


def keywords(text: str, limit: int = 4) -> List[str]:
    counts: Dict[str, int] = {}
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text.lower()):
        word = word.strip("-_")
        if word in STOPWORDS or len(word) < 4:
            continue
        counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, count in ranked[:limit] if count > 0]


def _word_set(text: str) -> set:
    words = {
        word
        for word in re.findall(r"[a-z][a-z0-9]{3,}", text.lower())
        if word not in STOPWORDS
    }
    return words


def _parse_time(value: str):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def analyze(cfg: Config, db: Database, store: DocStore) -> dict:
    store.sync()
    docs = store.list_docs(limit=2000)
    bodies: Dict[str, str] = {}
    for doc in docs:
        full = store.get(doc.id)
        bodies[doc.id] = full.body if full else ""

    untagged = []
    for doc in docs:
        if not doc.tags:
            suggestions = keywords(doc.title + "\n" + bodies.get(doc.id, ""), 3)
            untagged.append(
                {
                    "id": doc.id,
                    "title": doc.title,
                    "suggested_tags": suggestions,
                }
            )

    duplicates = []
    signatures = {
        doc.id: _word_set(doc.title + "\n" + bodies.get(doc.id, "")) for doc in docs
    }
    for i, first in enumerate(docs):
        if len(signatures[first.id]) < 8:
            continue
        for second in docs[i + 1 :]:
            other = signatures[second.id]
            if len(other) < 8:
                continue
            union = signatures[first.id] | other
            if not union:
                continue
            score = len(signatures[first.id] & other) / len(union)
            if score >= 0.8:
                duplicates.append(
                    {
                        "a": first.title,
                        "a_id": first.id,
                        "b": second.title,
                        "b_id": second.id,
                        "similarity": round(score, 2),
                    }
                )

    groups: Dict[str, List[str]] = {}
    for doc in docs:
        for tag in doc.tags:
            groups.setdefault(tag, []).append(doc.title)

    cutoff = datetime.now().astimezone() - timedelta(days=STALE_DAYS)
    stale = []
    for doc in docs:
        parsed = _parse_time(doc.updated_at)
        if parsed is not None and parsed < cutoff:
            stale.append({"id": doc.id, "title": doc.title, "updated": doc.updated_at})

    return {
        "total": len(docs),
        "untagged": untagged,
        "duplicates": duplicates,
        "groups": dict(sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))),
        "stale": stale,
    }


def apply(cfg: Config, db: Database, store: DocStore) -> List[str]:
    """Add suggested tags, then file tagged root documents into tag folders."""
    report = analyze(cfg, db, store)
    actions: List[str] = []
    for item in report["untagged"]:
        tags = item.get("suggested_tags") or []
        if not tags:
            continue
        try:
            store.add_tags(item["id"], tags[:2])
            actions.append("tagged %s with %s" % (item["id"], ", ".join(tags[:2])))
        except Exception as exc:  # keep going across the library
            actions.append("could not tag %s (%s)" % (item["id"], exc))

    # Re-read after tagging, then move top-level docs into a folder per primary tag.
    for doc in store.list_docs(limit=2000):
        if not doc.tags:
            continue
        source = Path(doc.path)
        if source.parent != cfg.docs_dir:
            continue
        folder = slugify(doc.tags[0])
        if not folder:
            continue
        moved = store.move(doc, folder)
        if moved is not None and str(moved) != str(source):
            actions.append("filed %s under %s/" % (doc.id, folder))

    store.sync()
    return actions


def format_report(report: dict) -> str:
    lines = ["Library: %d document(s)" % report["total"]]

    lines.append("")
    lines.append("Untagged (%d)" % len(report["untagged"]))
    if not report["untagged"]:
        lines.append("  everything already has tags")
    for item in report["untagged"]:
        suggested = ", ".join(item["suggested_tags"]) or "no obvious keywords"
        lines.append("  %-28s -> %s" % (item["title"][:28], suggested))

    lines.append("")
    lines.append("Possible duplicates (%d)" % len(report["duplicates"]))
    if not report["duplicates"]:
        lines.append("  none found")
    for pair in report["duplicates"]:
        lines.append(
            "  %s  ≈  %s  (%d%% overlap)"
            % (pair["a"], pair["b"], round(pair["similarity"] * 100))
        )

    lines.append("")
    lines.append("By tag")
    if not report["groups"]:
        lines.append("  no tags yet")
    for tag, titles in report["groups"].items():
        lines.append("  #%-16s %d doc(s)" % (tag, len(titles)))

    lines.append("")
    lines.append("Untouched for %d+ days (%d)" % (STALE_DAYS, len(report["stale"])))
    if not report["stale"]:
        lines.append("  none")
    for item in report["stale"]:
        lines.append("  %s (updated %s)" % (item["title"], item["updated"][:10]))
    return "\n".join(lines)
