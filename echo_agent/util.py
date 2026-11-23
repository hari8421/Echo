"""Small shared helpers."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Union

TMP_SUFFIX = ".echo-tmp"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str, fallback: str = "untitled") -> str:
    slug = text.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:60] or fallback


def normalize_tags(tags: Union[str, Iterable[str], None]) -> List[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        raw = re.split(r"[,\n]", tags.strip().strip("[]"))
    else:
        raw = list(tags)
    result: List[str] = []
    for tag in raw:
        cleaned = re.sub(r"\s+", "-", str(tag).strip().lower())
        cleaned = cleaned.strip("-")
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a crash never truncates a document."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + TMP_SUFFIX)
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))
