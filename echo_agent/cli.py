"""echo-ai command line interface."""

from __future__ import annotations

import argparse
import os
import platform
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from . import heal, ui
from . import desktop
from .agent import Agent
from .config import Config
from .db import Database
from .docs import DocError, DocStore
from .llm import LLM, LLMError
from .memory import MemoryStore
from .organize import analyze, apply as organize_apply, format_report
from .qa import ask_from_docs
from .research import ResearchError, research
from .retrieval import Retriever
from .social import SocialError, draft_post, list_drafts, read_draft, set_status
from .util import trim


class CLIError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="echo-ai",
        description="Local-first personal assistant for docs, memory, research and social drafts.",
    )
    parser.add_argument("--home", help="Override ECHO_HOME for this run.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Create the workspace and show next steps.").set_defaults(func=cmd_init)
    doctor = sub.add_parser("doctor", help="Check the model server, database and workspace.")
    doctor.add_argument("--fix", action="store_true", help="Repair workspace problems it finds.")
    doctor.set_defaults(func=cmd_doctor)

    sub.add_parser("repair", help="Heal the workspace: folders, index, database.").set_defaults(func=cmd_repair)

    chat = sub.add_parser("chat", help="Talk to the agent with tools enabled.")
    chat.set_defaults(func=cmd_chat)

    ui_cmd = sub.add_parser("ui", help="Serve the local web interface.")
    ui_cmd.add_argument("--port", type=int, default=None)
    ui_cmd.add_argument("--host", default=None, help="Bind address (default: localhost only).")
    ui_cmd.set_defaults(func=cmd_ui)

    app_cmd = sub.add_parser("app", help="Launch Echo as a standalone desktop window (requires .[desktop]).")
    app_cmd.add_argument("--port", type=int, default=None)
    app_cmd.add_argument("--host", default=None)
    app_cmd.set_defaults(func=cmd_app)

    cfg_cmd = sub.add_parser("config", help="Show or update configuration in $ECHO_HOME/.env.")
    cfg_cmd.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="Set config value (provider,model,base_url,api_key,embed_model,temperature,max_tool_steps,auto_memory,system_prompt,ui_host,ui_port)")
    cfg_cmd.set_defaults(func=cmd_config)

    sessions = sub.add_parser("sessions", help="List chat sessions.")
    sessions.add_argument("--limit", type=int, default=20)
    sessions.set_defaults(func=cmd_sessions)

    ask = sub.add_parser("ask", help="Answer a question from your documents.")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--limit", type=int, default=6)
    ask.set_defaults(func=cmd_ask)

    new = sub.add_parser("new", help="Create a document.")
    new.add_argument("title", nargs="+")
    new.add_argument("--content")
    new.add_argument("--tag", action="append", default=[])
    new.set_defaults(func=cmd_new)

    edit = sub.add_parser("edit", help="Edit a document (opens $EDITOR if no flags).")
    edit.add_argument("doc")
    edit.add_argument("--content", help="Replace the whole body.")
    edit.add_argument("--append", help="Append markdown.")
    edit.add_argument("--old", help="Exact text to replace (with --new).")
    edit.add_argument("--new", dest="replacement", help="Replacement text.")
    edit.add_argument("--title")
    edit.add_argument("--tag", action="append", default=[])
    edit.set_defaults(func=cmd_edit)

    read = sub.add_parser("read", help="Print a document.")
    read.add_argument("doc")
    read.set_defaults(func=cmd_read)

    ls = sub.add_parser("list", help="List documents.")
    ls.add_argument("--tag")
    ls.add_argument("--limit", type=int, default=40)
    ls.set_defaults(func=cmd_list)

    find = sub.add_parser("find", help="Search document content.")
    find.add_argument("query", nargs="+")
    find.add_argument("--limit", type=int, default=8)
    find.set_defaults(func=cmd_find)

    rm = sub.add_parser("archive", help="Move a document to the archive.")
    rm.add_argument("doc")
    rm.set_defaults(func=cmd_archive)

    remember = sub.add_parser("remember", help="Save something to long-term memory.")
    remember.add_argument("text", nargs="+")
    remember.add_argument("--kind", default="note")
    remember.add_argument("--tag", action="append", default=[])
    remember.set_defaults(func=cmd_remember)

    memories = sub.add_parser("memories", help="List or search memories, or forget one.")
    memories.add_argument("query", nargs="*")
    memories.add_argument("--kind")
    memories.add_argument("--forget", type=int, metavar="ID")
    memories.add_argument("--limit", type=int, default=30)
    memories.set_defaults(func=cmd_memories)

    organize = sub.add_parser("organize", help="Suggest tags/folders, find duplicates.")
    organize.add_argument("--apply", action="store_true", help="Apply tags and file docs into folders.")
    organize.set_defaults(func=cmd_organize)

    research_cmd = sub.add_parser("research", help="Research a topic and save a sourced briefing.")
    research_cmd.add_argument("topic", nargs="+")
    research_cmd.add_argument("-n", "--sources", type=int, default=4)
    research_cmd.set_defaults(func=cmd_research)

    post = sub.add_parser("post", help="Draft a social post into the outbox.")
    post.add_argument("topic", nargs="+")
    post.add_argument("-p", "--platform", default="linkedin")
    post.add_argument("--notes", default="")
    post.add_argument("--tone", default="professional")
    post.add_argument("--thread", type=int, default=0)
    post.set_defaults(func=cmd_post)

    outbox = sub.add_parser("outbox", help="Review social drafts.")
    outbox.add_argument("--show", metavar="NAME")
    outbox.add_argument("--approve", metavar="NAME")
    outbox.set_defaults(func=cmd_outbox)

    index = sub.add_parser("index", help="Re-sync the document index.")
    index.add_argument("--rebuild", action="store_true")
    index.set_defaults(func=cmd_index)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    if getattr(args, "home", None):
        os.environ["ECHO_HOME"] = args.home

    db = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        store = DocStore(cfg, db)
        memory = MemoryStore(db)
        if db.recovered_from:
            print(
                "note: the database was unreadable; moved it to %s and rebuilding the index."
                % db.recovered_from,
                file=sys.stderr,
            )
            store.rebuild()
        return args.func(args, cfg, db, store, memory)
    except (DocError, LLMError, ResearchError, SocialError, CLIError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as exc:
        print("error: database problem: %s" % exc, file=sys.stderr)
        print("Try `echo-ai repair` (your markdown files are untouched).", file=sys.stderr)
        return 1
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    finally:
        if db is not None:
            db.close()


# -- helpers ---------------------------------------------------------------
def _read_input(args, initial: str = "") -> str:
    content = getattr(args, "content", None)
    if content is not None:
        return content
    if not sys.stdin.isatty():
        return sys.stdin.read()
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        raise CLIError(
            "No $EDITOR set. Pass --content \"...\" or set $EDITOR (e.g. export EDITOR=vim)."
        )
    handle, path = tempfile.mkstemp(prefix="echo-", suffix=".md", text=True)
    os.close(handle)
    try:
        Path(path).write_text(initial, encoding="utf-8")
        code = subprocess.call(shlex.split(editor) + [path])
        if code != 0:
            raise CLIError("Editor exited with status %d." % code)
        return Path(path).read_text(encoding="utf-8")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _print_doc(doc, body: bool = True) -> None:
    print("%s  (%s)" % (doc.title, doc.id))
    meta = ["updated %s" % doc.updated_at, "%d words" % doc.words]
    if doc.tags:
        meta.append("tags: " + ", ".join(doc.tags))
    print("  " + " · ".join(meta))
    print("  " + doc.path)
    if body:
        print()
        print(doc.body or "(empty document)")


# -- commands --------------------------------------------------------------
def cmd_init(args, cfg, db, store, memory) -> int:
    print("Echo workspace ready.\n")
    print(cfg.summary())
    print("\nindex:    %s" % ("SQLite FTS5" if db.fts else "SQLite LIKE fallback"))
    print("database: %s" % cfg.db_path)
    print("\nNext steps:")
    print("  1. Install a local model:  ollama pull %s" % cfg.model)
    print("  2. Verify everything:      echo-ai doctor")
    print("  3. Create a first note:    echo-ai new \"My first note\"")
    print("  4. Start the assistant:    echo-ai chat")
    print("     or the web UI:          echo-ai ui")
    return 0


def cmd_doctor(args, cfg, db, store, memory) -> int:
    print("Echo doctor\n")
    failures = 0
    print("[ok] Python %s" % platform.python_version())
    if sys.version_info < (3, 9):
        print("[!!] Python 3.9+ is required")
        failures += 1
    for label, path in (("home", cfg.home), ("docs", cfg.docs_dir), ("outbox", cfg.outbox_dir)):
        print("[ok] %s: %s" % (label, path))
    print("[ok] search index: %s" % ("FTS5" if db.fts else "LIKE fallback"))
    print("[ok] %d document(s), %d memor(ies)" % (db.doc_count(), memory.count()))
    if not cfg.embed_model:
        print("[--] semantic search disabled (set ECHO_EMBED_MODEL to enable)")

    llm = LLM(cfg)
    try:
        info = llm.ping()
        print("[ok] %s endpoint reachable: %s" % (cfg.provider, cfg.base_url))
        if cfg.provider == "ollama":
            if info.get("model_present"):
                print("[ok] model available: %s" % cfg.model)
            else:
                print("[!!] model %r is not pulled yet" % cfg.model)
                print("     run: ollama pull %s" % cfg.model)
                failures += 1
    except LLMError as exc:
        print("[!!] model server unreachable")
        print("     " + str(exc).replace("\n", "\n     "))
        failures += 1

    problems = heal.check(cfg, db, store)
    if problems:
        print()
        for problem in problems:
            print("[!!] " + problem)
        if getattr(args, "fix", False):
            actions = heal.repair(cfg, db, store)
            print()
            for action in actions or ["nothing needed repair"]:
                print("[fixed] " + action)
            problems = heal.check(cfg, db, store)
        else:
            print("     run: echo-ai doctor --fix   (or: echo-ai repair)")
    else:
        print("[ok] workspace consistent")
    if problems:
        failures += len(problems)

    print("\n%s" % ("Ready." if failures == 0 else "%d issue(s) to fix." % failures))
    return 0 if failures == 0 else 1


def cmd_repair(args, cfg, db, store, memory) -> int:
    problems = heal.check(cfg, db, store)
    if not problems:
        print("Workspace is already healthy: %s" % cfg.home)
        return 0
    print("Found %d problem(s):" % len(problems))
    for problem in problems:
        print("  - " + problem)
    actions = heal.repair(cfg, db, store)
    print("\nRepairs:")
    for action in actions or ["nothing needed repair"]:
        print("  - " + action)
    remaining = heal.check(cfg, db, store)
    if remaining:
        print("\nStill broken:")
        for problem in remaining:
            print("  ! " + problem)
        return 1
    print("\nWorkspace healthy again. Documents were not modified.")
    return 0


def cmd_ui(args, cfg, db, store, memory) -> int:
    host = args.host or cfg.ui_host
    port = args.port if args.port is not None else cfg.ui_port
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "warning: the UI has no login. Only bind to %s on a network you trust." % host,
            file=sys.stderr,
        )
    try:
        ui.run(cfg, host=host, port=port)
    except OSError as exc:
        raise CLIError("cannot listen on %s:%d (%s). Try --port %d." % (host, port, exc, port + 1))
    return 0


def cmd_app(args, cfg, db, store, memory) -> int:
    host = args.host or cfg.ui_host
    port = args.port if args.port is not None else cfg.ui_port
    try:
        desktop.run(cfg, host=host, port=port)
    except desktop.DesktopError as exc:
        raise CLIError(str(exc))
    except OSError as exc:
        raise CLIError("cannot listen on %s:%d (%s). Try --port %d." % (host, port, exc, port + 1))
    return 0


def cmd_config(args, cfg, db, store, memory) -> int:
    env_path = cfg.home / ".env"
    key_map = {
        "provider": "ECHO_PROVIDER", "model": "ECHO_MODEL", "base_url": "ECHO_BASE_URL", "api_key": "ECHO_API_KEY",
        "embed_model": "ECHO_EMBED_MODEL", "temperature": "ECHO_TEMPERATURE", "max_tool_steps": "ECHO_MAX_TOOL_STEPS",
        "auto_memory": "ECHO_AUTO_MEMORY", "system_prompt": "ECHO_SYSTEM_PROMPT", "ui_host": "ECHO_UI_HOST", "ui_port": "ECHO_UI_PORT",
    }
    updates = {}
    for item in args.set:
        if "=" not in item:
            raise CLIError("Invalid --set %r, expected KEY=VALUE." % item)
        k, _, v = item.partition("=")
        k = k.strip().lower()
        if k not in key_map:
            raise CLIError("Unknown config key %r." % k)
        updates[k] = v.strip()
    if updates:
        existing = {}
        if env_path.exists():
            for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                ek, _, ev = line.partition("=")
                existing[ek.strip()] = ev.strip()
        for k, v in updates.items():
            existing[key_map[k]] = v
        env_path.write_text("".join("%s=%s\n" % (k, existing[k]) for k in sorted(existing)), encoding="utf-8")
        print("Saved %d setting(s) to %s" % (len(updates), env_path))
    print(cfg.summary())
    if cfg.api_key:
        print("api_key:  (set)")
    return 0


def cmd_sessions(args, cfg, db, store, memory) -> int:
    rows = db.sessions(max(1, args.limit))
    if not rows:
        print("No chat sessions yet.")
        return 0
    for row in rows:
        print("%s  %s  %s" % (row["id"], row["updated_at"][:16].replace("T", " "), trim(row["title"], 60)))
    return 0


def cmd_chat(args, cfg, db, store, memory) -> int:
    if not sys.stdin.isatty():
        raise CLIError("Chat needs an interactive terminal. Use `echo-ai ask` instead.")
    agent = Agent(cfg, db, store, memory)

    def show_tool(name, arguments):
        preview = ", ".join(
            "%s=%s" % (key, trim(str(value).replace("\n", " "), 40))
            for key, value in list(arguments.items())[:3]
        )
        print("  · %s(%s)" % (name, preview))

    agent.on_tool = show_tool
    print("Echo — %s via %s" % (cfg.model, cfg.provider))
    print("Home: %s  ·  %d docs  ·  %d memories" % (cfg.home, db.doc_count(), memory.count()))
    print("Commands: /help, /new <title>, /ask <question>, /remember <note>, "
          "/memories, /docs, /exit")

    while True:
        try:
            line = input("\nyou › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"/exit", "/quit", "/q"}:
            break
        if line == "/help":
            print("/new <title>      create a document")
            print("/ask <question>   answer from your docs")
            print("/remember <note>  save a memory")
            print("/memories         recent memories")
            print("/docs             recent documents")
            print("/exit             leave (saves session memories)")
            continue
        if line.startswith("/new "):
            doc = store.create(line[5:].strip())
            print("created %s — %s" % (doc.title, doc.id))
            continue
        if line.startswith("/ask "):
            try:
                answer, hits = ask_from_docs(cfg, agent.retriever, agent.llm, line[5:].strip())
            except LLMError as exc:
                print("error: %s" % exc)
                continue
            print("\necho › " + answer)
            _print_sources(hits)
            continue
        if line.startswith("/remember "):
            item, created = memory.add(line[10:].strip(), source="chat")
            print("%s memory #%d" % ("saved" if created else "updated", item.id))
            continue
        if line == "/memories":
            _print_memories(memory.all(10))
            continue
        if line == "/docs":
            _print_docs(store.list_docs(limit=10))
            continue

        try:
            answer = agent.chat(line)
        except LLMError as exc:
            print("error: %s" % exc)
            continue
        print("\necho › " + answer)

    if cfg.auto_memory and agent.history:
        print("\nUpdating long-term memory…")
        try:
            stored = agent.extract_memories()
        except Exception as exc:  # never lose the session over this
            print("  memory update skipped: %s" % exc)
            stored = []
        if stored:
            for item in stored:
                print("  saved [%s] %s" % (item.kind, trim(item.text, 140)))
        else:
            print("  nothing new worth keeping")
    print("\nSession saved. See you next time.")
    return 0


def cmd_ask(args, cfg, db, store, memory) -> int:
    store.sync()
    retriever = Retriever(cfg, db)
    answer, hits = ask_from_docs(cfg, retriever, LLM(cfg), " ".join(args.question), args.limit)
    print(answer)
    _print_sources(hits)
    return 0


def cmd_new(args, cfg, db, store, memory) -> int:
    title = " ".join(args.title).strip()
    body = _read_input(args, initial="")
    doc = store.create(title, body, tags=args.tag)
    print("Created %s" % doc.id)
    _print_doc(doc, body=False)
    return 0


def cmd_edit(args, cfg, db, store, memory) -> int:
    doc = store.get(args.doc)
    if doc is None:
        raise DocError("No document matches %r." % args.doc)
    if args.old is not None:
        if args.replacement is None:
            raise CLIError("--old requires --new.")
        doc = store.edit(doc.id, args.old, args.replacement)
    elif args.append is not None:
        doc = store.append(doc.id, args.append)
    elif args.content is not None:
        doc = store.update(doc.id, body=args.content)
    else:
        body = _read_input(args, initial=doc.body)
        doc = store.update(doc.id, body=body)
    if args.title:
        doc = store.update(doc.id, title=args.title)
    if args.tag:
        doc = store.add_tags(doc.id, args.tag)
    print("Updated %s" % doc.id)
    _print_doc(doc, body=False)
    return 0


def cmd_read(args, cfg, db, store, memory) -> int:
    doc = store.get(args.doc)
    if doc is None:
        raise DocError("No document matches %r." % args.doc)
    _print_doc(doc)
    return 0


def cmd_list(args, cfg, db, store, memory) -> int:
    docs = store.list_docs(tag=args.tag, limit=max(1, args.limit))
    if not docs:
        print("No documents yet. Create one with: echo-ai new \"Title\"")
        return 0
    _print_docs(docs)
    return 0


def cmd_find(args, cfg, db, store, memory) -> int:
    store.sync()
    hits = Retriever(cfg, db).search(" ".join(args.query), limit=max(1, args.limit))
    if not hits:
        print("No matches.")
        return 0
    for index, hit in enumerate(hits, start=1):
        print("%d. %s  (%s)" % (index, hit.title, hit.doc_id))
        print("   " + trim(hit.text.replace("\n", " "), 240))
    return 0


def cmd_archive(args, cfg, db, store, memory) -> int:
    path = store.archive(args.doc)
    print("Archived to %s" % path)
    return 0


def cmd_remember(args, cfg, db, store, memory) -> int:
    item, created = memory.add(" ".join(args.text), kind=args.kind, tags=args.tag, source="cli")
    print("%s memory #%d [%s] %s" % ("Saved" if created else "Updated", item.id, item.kind, item.text))
    return 0


def cmd_memories(args, cfg, db, store, memory) -> int:
    if args.forget is not None:
        if memory.forget(args.forget):
            print("Forgot memory #%d" % args.forget)
            return 0
        raise CLIError("No memory with id %d." % args.forget)
    items = memory.search(" ".join(args.query), kind=args.kind, limit=args.limit)
    if not items:
        print("No memories match.")
        return 0
    _print_memories(items)
    return 0


def cmd_organize(args, cfg, db, store, memory) -> int:
    if args.apply:
        actions = organize_apply(cfg, db, store)
        print("Applied %d change(s):" % len(actions))
        for action in actions:
            print("  - " + action)
        print()
    report = analyze(cfg, db, store)
    print(format_report(report))
    if not args.apply:
        print("\nRun with --apply to add suggested tags and file docs into tag folders.")
    return 0


def cmd_research(args, cfg, db, store, memory) -> int:
    topic = " ".join(args.topic).strip()
    print("Researching %r — searching and reading %d source(s)…\n" % (topic, args.sources))
    doc, pages, failures = research(cfg, store, LLM(cfg), topic, max_sources=args.sources)
    print(doc.body)
    print("\nSaved as %s (%s)" % (doc.id, doc.path))
    if failures:
        print("Skipped %d unreadable source(s)." % len(failures))
    return 0


def cmd_post(args, cfg, db, store, memory) -> int:
    topic = " ".join(args.topic).strip()
    draft = draft_post(
        cfg,
        LLM(cfg),
        platform=args.platform,
        topic=topic,
        notes=args.notes,
        tone=args.tone,
        thread=args.thread,
    )
    print(draft.content)
    print("\n%s · %d chars · limit %d" % (draft.platform, draft.chars, draft.limit))
    if draft.warning:
        print("warning: %s" % draft.warning)
    print("Saved: %s" % draft.path)
    print("Review it, then approve with: echo-ai outbox --approve %s" % draft.path.name)
    return 0


def cmd_outbox(args, cfg, db, store, memory) -> int:
    if args.show:
        draft = read_draft(cfg, args.show)
        print("%s  [%s]" % (draft["title"], draft["status"]))
        print("%s · %s" % (draft["platform"], draft["name"]))
        print()
        print(draft["body"])
        return 0
    if args.approve:
        path = set_status(cfg, args.approve, "approved")
        print("Approved %s" % path.name)
        print("Copy it into the platform. Echo does not publish on its own.")
        return 0

    drafts = list_drafts(cfg)
    if not drafts:
        print("Outbox is empty. Draft something with: echo-ai post -p x \"topic\"")
        return 0
    for draft in drafts:
        print("%-34s %-9s %-8s %4d chars  %s" % (
            draft["name"],
            draft["platform"],
            draft["status"],
            draft["chars"],
            trim(draft["topic"], 60),
        ))
    print("\nShow one:    echo-ai outbox --show NAME")
    print("Approve one: echo-ai outbox --approve NAME")
    return 0


def cmd_index(args, cfg, db, store, memory) -> int:
    if args.rebuild:
        changed = store.rebuild()
        print("Rebuilt index: %d document(s)." % changed)
    else:
        changed = store.sync()
        print("Indexed %d changed document(s)." % changed)
    print("%d document(s) total in %s" % (db.doc_count(), cfg.docs_dir))
    return 0


# -- small printers --------------------------------------------------------
def _print_docs(docs) -> None:
    for doc in docs:
        tags = ("  #" + " #".join(doc.tags)) if doc.tags else ""
        print("%-30s %-26s %s%s" % (doc.id, trim(doc.title, 26), doc.updated_at[:10], tags))


def _print_memories(items) -> None:
    for item in items:
        tags = ("  #" + " #".join(item.tags)) if item.tags else ""
        print("[%d] %-10s %s%s" % (item.id, item.kind, item.text, tags))


def _print_sources(hits) -> None:
    if not hits:
        return
    seen = set()
    print("\nSources:")
    for hit in hits:
        if hit.doc_id in seen:
            continue
        seen.add(hit.doc_id)
        print("  - %s (%s)" % (hit.title, hit.doc_id))


if __name__ == "__main__":
    raise SystemExit(main())
