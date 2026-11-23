"""Self-healing for the Echo workspace.

Everything here is about reaching a usable state again without asking the user
to delete anything:

* ``check`` lists the problems it can see.
* ``repair`` fixes them: create folders, drop leftover temp files, rebuild the
  full-text index, and re-sync the index with the markdown files on disk.

Documents are never deleted by a repair. The worst case is a rebuilt index,
which costs a re-read of the files, not the files themselves.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List

from .config import Config
from .db import Database
from .docs import DocStore
from .util import TMP_SUFFIX

DIRECTORIES = ("home", "docs", "outbox", "archive")


def _dirs(cfg: Config):
    return (
        ("home", cfg.home),
        ("docs", cfg.docs_dir),
        ("outbox", cfg.outbox_dir),
        ("archive", cfg.archive_dir),
    )


def _temp_files(cfg: Config) -> List[Path]:
    if not cfg.docs_dir.exists():
        return []
    return [p for p in cfg.docs_dir.rglob("*" + TMP_SUFFIX) if p.is_file()]


def check(cfg: Config, db: Database, store: DocStore) -> List[str]:
    """Return human-readable problems without changing anything."""
    problems: List[str] = []
    for label, path in _dirs(cfg):
        if not path.is_dir():
            problems.append("%s folder missing: %s" % (label, path))

    if db.recovered_from:
        problems.append(
            "database was unreadable and quarantined to %s" % db.recovered_from
        )
    if not db.integrity():
        problems.append("database failed its integrity check")
    if not db.fts:
        problems.append("SQLite FTS5 unavailable; keyword search uses the LIKE fallback")

    temps = _temp_files(cfg)
    if temps:
        problems.append("%d leftover temporary file(s) in docs" % len(temps))

    if cfg.docs_dir.is_dir():
        on_disk = {p.stem for p in cfg.docs_dir.rglob("*.md") if not p.name.startswith(".")}
        indexed = {row["id"] for row in db.docs()}
        missing = on_disk - indexed
        stale = indexed - on_disk
        if missing:
            problems.append("%d document(s) on disk are not indexed" % len(missing))
        if stale:
            problems.append("%d index entr(ies) have no matching file" % len(stale))
    return problems


def repair(cfg: Config, db: Database, store: DocStore) -> List[str]:
    """Fix what ``check`` reports. Returns the actions actually taken."""
    actions: List[str] = []

    try:
        cfg.ensure_dirs()
    except OSError as exc:
        actions.append("could not create folders: %s" % exc)
        return actions

    if db.recovered_from:
        actions.append(
            "moved the unreadable database to %s and started a fresh index"
            % db.recovered_from
        )
    if not db.integrity():
        try:
            db.conn.execute("REINDEX")
            db.conn.commit()
            actions.append("reindexed the database")
        except sqlite3.DatabaseError as exc:
            actions.append("database could not be reindexed: %s" % exc)

    if not db.fts and db.rebuild_fts():
        actions.append("rebuilt the full-text index")

    for path in _temp_files(cfg):
        try:
            path.unlink()
            actions.append("removed leftover temporary file %s" % path.name)
        except OSError:
            pass

    try:
        changed = store.sync()
    except (OSError, sqlite3.DatabaseError) as exc:
        actions.append("re-index failed: %s" % exc)
        return actions
    if changed:
        actions.append("re-indexed %d document(s)" % changed)
    return actions
