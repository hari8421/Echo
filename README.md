# Echo

A local-first personal assistant for your day-to-day work: it writes and edits
documents, answers questions from them, remembers what matters, keeps the
library tidy, researches topics on the web, and drafts social posts for you to
approve.

Everything lives in a folder you own (`~/.echo` by default): plain markdown
documents, a SQLite index, a memory table, and a social-draft outbox. No account,
no cloud database, no telemetry. There is a CLI and a small local web UI.

## What it does

| Area | What you get |
| --- | --- |
| **Documents** | Create, read, edit, append, tag, archive and search markdown files. `$EDITOR` support. |
| **Ask your docs** | Hybrid keyword + optional semantic retrieval, grounded answers with citations, never invented sources. |
| **Memory** | Durable facts, preferences, people, projects and decisions in SQLite. Saved explicitly or extracted from chats. |
| **Organize** | Suggests tags for untagged docs, finds near-duplicates, groups by tag, flags stale notes, and can file docs into tag folders. |
| **Research** | Web search, page reading, and a sourced markdown briefing saved to your library. |
| **Social drafts** | Platform-aware posts and threads (X, LinkedIn, Bluesky, Threads, Instagram, Facebook, Reddit) saved to an outbox. **Nothing is published automatically.** |
| **Browser agent** | Optional Playwright tools for local browser control: open pages, snapshot refs, click/fill/press/scroll/screenshot. Can act on your behalf when you command it. |
| **Chat agent** | A tool-calling assistant with persistent sessions, docs/memory/research/social/browser tools. |
| **Web UI / App** | Local dashboard for chat, docs, ask, browser, memory, outbox, settings; can run as web UI or standalone desktop window. |
| **Self-healing** | Retries transient model failures, writes documents atomically, and repairs a damaged index or workspace. |

## Requirements

- Python 3.9+
- A local model via [Ollama](https://ollama.com) (default), or any OpenAI-compatible endpoint

No Python packages are required at runtime; Echo uses only the standard library.
That includes the web UI (no Node, no bundler, no CDN).

## Quickstart

```bash
# 1. Local model (recommended: a model that supports tool calling)
ollama pull llama3.2

# 2. Install the CLI
pip install -e .

# Optional extras
pip install -e .[browser]   # browser automation (Playwright)
python -m playwright install chromium
pip install -e .[desktop]   # standalone desktop window (pywebview)

# 3. Create the workspace and verify everything
echo-ai init
echo-ai doctor

# 4. Start using it
echo-ai new "Project kickoff"          # opens $EDITOR
echo-ai ask "What did I decide about pricing?"
echo-ai chat
echo-ai ui                             # http://127.0.0.1:8765
echo-ai app                            # standalone desktop window
```

Without installing, use `python -m echo_agent ...` with the same arguments.

### Using an OpenAI-compatible endpoint

```bash
export ECHO_PROVIDER=openai
export ECHO_BASE_URL=https://api.openai.com/v1
export ECHO_API_KEY=...
export ECHO_MODEL=gpt-4o-mini
```

Any host that implements `/chat/completions` and `/models` works (including
local gateways such as LM Studio or vLLM, if their base URL is set).

## Design decisions

These are the choices that shape everything else in the project. Each one has a
cost; the costs are listed rather than hidden.

**1. Local-first files, not a hosted database.**
Your documents are ordinary `.md` files in `~/.echo/docs`, readable and
editable without Echo. SQLite (`echo.db`) holds only derived state: the index,
chunks, memories and transcript. Deleting the database loses nothing but the
index and memories — never a document. *Cost:* no sync, no multi-device access,
no concurrent editing.

**2. Zero runtime dependencies.**
Only the Python standard library. `pip install -e .` cannot pull a supply-chain
problem into your personal notes, and the tool keeps working years from now.
*Cost:* hand-written HTTP clients, HTML-to-text extraction and UI, instead of
battle-tested libraries.

**3. A model-agnostic client, not a framework.**
`llm.py` normalizes Ollama and OpenAI-compatible tool calls into one internal
shape (`{"content", "tool_calls": [{"id", "name", "arguments"}]}`). LangChain-style
frameworks were avoided because they hide retries, token accounting and prompt
shape — the things you need to control in an assistant that runs on your laptop.
*Cost:* new providers must be added by hand (~30 lines each).

**4. One agent loop, with tools as the only side-effect channel.**
The model never gets direct access to the filesystem. It asks for a named tool
with JSON arguments, Echo validates and runs it, and the JSON result goes back
into the conversation. This is what makes failures recoverable: a bad argument
becomes an error message the model can read, not a crash.

**5. Keyword search first, semantic search optional.**
SQLite FTS5 (BM25) is always available and needs no model. Embeddings are an
opt-in upgrade (`ECHO_EMBED_MODEL`) that degrades back to keyword search on any
failure. *Cost:* without an embedding model, "how did I feel about the launch"
finds less than "launch retrospective" does.

**6. The document file is the source of truth.**
The index is disposable and can be rebuilt with `echo-ai index --rebuild` or
`echo-ai repair`. Ids are the filename stem, so links between docs and
`echo-ai read <id>` stay stable even if the database is gone.

**7. Draft-only social posting.**
Echo has no platform credentials and cannot publish. Drafts land in `outbox/`
with `status: draft`; `--approve` changes a frontmatter field the human flips
after reading. This is a deliberate refusal: an LLM should not be able to post
publicly on your behalf without a human check, and credential storage in a
local script is a security liability.

**8. Memory is inspectable, not a vector soup.**
Memories are rows with `kind`, `text`, `tags`, `source` and a timestamp,
deduplicated case-insensitively. You can list, search and forget them. *Cost:*
retrieval is keyword-based, so very indirect recollections will not surface.

**9. A local web UI in one file, deliberately unauthenticated.**
`echo-ai ui` serves a single page from the standard library and binds to
`127.0.0.1` only. Adding accounts to a single-user local tool would suggest a
security guarantee it does not have; instead the UI documents that it is local.

**10. Self-healing over prompting the user to fix things.**
Failures are expected (laptop sleeps, model server restarts, a half-written
file). Echo retries transient model errors, writes documents atomically, and
rebuilds a corrupt index from the markdown files without asking. *Cost:* a
little extra code on every path, and repairs that are conservative rather than
clever.

## How the agent works

### One turn

```text
echo-ai chat / POST /api/chat
        │
        ▼
Agent.chat(text)
  ├─ store.sync()                    index changed files first
  ├─ history += user message         persisted to messages table
  └─ loop (max ECHO_MAX_TOOL_STEPS):
        messages = [system prompt] + trimmed history
        response = LLM.chat(messages, tools=TOOL_SPECS)
        ├─ tool_calls?  → append assistant turn
        │                 run each tool through ToolRunner
        │                 append {"role": "tool", ...} results
        │                 loop again
        └─ no tool calls → final answer, save to transcript, return
```

The system prompt (`agent.py: build_system_prompt`) carries the date, workspace
path, library and memory counts, the twelve newest memories, and a short policy:
call tools instead of guessing, cite document titles, prefer appending over
duplicating, never store secrets, never claim to have published, and treat a
tool `{"error": ...}` as information rather than something to repeat.

History is trimmed to the last 40 messages and re-anchored to the first `user`
turn, so a tool result is never sent without the assistant call that produced
it. The full transcript is still in SQLite.

### Tools

Fifteen tools, defined once in `tools.py` as JSON Schema and dispatched by name:

- Documents: `create_doc`, `update_doc`, `edit_doc`, `append_to_doc`, `read_doc`, `list_docs`, `search_docs`
- Library: `organize_docs`
- Memory: `remember`, `recall`
- Web: `web_search`, `fetch_url`, `research_topic`
- Social: `draft_social_post`, `list_outbox`

`ToolRunner.run` never raises at the model: unknown tools, wrong argument types
and tool exceptions all become `{"error": "..."}`. That is the core recovery
mechanism — the model sees the failure and can correct itself.

Two loop guards keep a confused model from burning your afternoon: the step
budget (`ECHO_MAX_TOOL_STEPS`, default 12), and a duplicate-call guard that stops
after the same tool call with identical arguments has run twice.

### Indexing and retrieval

`DocStore.sync()` walks `docs/**/*.md`, compares `mtime` against the `docs`
table, and re-indexes changed files: frontmatter is parsed, the body is split
into overlapping ~1000-character paragraph-aware chunks, embeddings are computed
when configured, and rows are written to `chunks` plus the `chunks_fts` virtual
table. Files that disappeared drop out of the index; renamed files are found
again under their new ids.

`Retriever.search` merges two signals: BM25 rank from FTS5, and cosine
similarity over cached embeddings (only chunks that have a vector). Scores are
summed, so a chunk that both matches keywords and is semantically close wins.
With no embedding model, the semantic half returns nothing and the keyword half
still works.

`qa.ask_from_docs` retrieves the top chunks, numbers them, and instructs the
model to answer *only* from those excerpts, citing `[n]`. If retrieval is empty
it says so without calling the model at all, which is why Echo does not
hallucinate sources.

### Memory lifecycle

1. `echo-ai remember`, `POST /api/memory`, or the `remember` tool writes a row
   (deduplicated by text, tags merged, timestamp refreshed).
2. The newest twelve memories are injected into every chat system prompt.
3. On chat exit (CLI), Echo asks the model for up to five durable memories as a
   JSON array and stores them with `source: chat`. Set `ECHO_AUTO_MEMORY=0` to
   turn this off. Failure here is non-fatal and never blocks closing a session.

### Research

`research()` queries DuckDuckGo's HTML endpoint, extracts readable text from up
to `-n` pages with a small `HTMLParser`, and asks the model for a 350–550 word
briefing with inline `[S1]` citations. Echo then appends the real URLs itself,
so a citation always resolves to a page that was actually read. A failed search
or unreadable pages produce a clear error instead of an invented report.

## Self-healing

Failures are handled at four levels, cheapest first.

| Failure | What Echo does |
| --- | --- |
| Model server briefly unreachable, timeout, 429/5xx | Retries with exponential backoff (`ECHO_LLM_RETRIES`, default 2 attempts after the first, `ECHO_LLM_RETRY_DELAY` seconds). A 400-class error like a bad model name is **not** retried. |
| Tool called with wrong or missing arguments | `ToolRunner` returns `{"error": ...}` to the model, which can retry with corrected arguments. It never crashes the session. |
| Model repeats the same failing tool call | The duplicate-call guard refuses the third identical call and returns a plain explanation, keeping the conversation intact. |
| Process killed mid-write | Documents are written to a temp file and `os.replace`d, so a file is either the old version or the new one — never truncated. |
| Model call still failing after retries | The error is shown with an actionable hint (`ollama serve`, `ollama pull …`, or the HTTP status). The chat session survives; the next message continues the same history. |
| `echo.db` unreadable/corrupt | On open it is moved to `echo.db.corrupt-<timestamp>` and a fresh index is built from your markdown files. A notice is printed, and the old file is kept for inspection. |
| Index out of sync, stray temp files, missing folders, FTS table lost | `echo-ai repair` (or `echo-ai doctor --fix`) recreates folders, removes leftover temp files, rebuilds FTS from the stored chunks, and re-syncs the index. The web UI shows a banner when the checks fail. |

Repairs are conservative by design: they rebuild derived state and never delete
or rewrite a document. Run them any time:

```bash
echo-ai doctor          # check, including the model server
echo-ai doctor --fix    # check, then repair what it found
echo-ai repair          # repair and report, exit 1 if anything remains broken
```

## Web UI / Standalone App

```bash
echo-ai ui                      # http://127.0.0.1:8765
echo-ai ui --port 9000
echo-ai ui --host 0.0.0.0       # only on a network you trust
echo-ai app                     # standalone desktop window (needs pywebview)
```

Tabs/views: **Chat** (persistent sessions, tool trace), **Documents**,
**Ask docs**, **Browser** (Playwright status/goto/snapshot/click/fill/press),
**Memory**, **Outbox**, and **Settings**. Settings can save model/provider/prompt
to `$ECHO_HOME/.env` and test model connectivity.

### Browser automation (act on your behalf)

- Install: `pip install -e .[browser] && python -m playwright install chromium`
- Agent tools: `browser_start`, `browser_goto`, `browser_snapshot`, `browser_click`, `browser_fill`, `browser_press`, `browser_scroll`, `browser_screenshot`, `browser_close`.
- Use a persistent `profile_dir` to reuse logins/cookies locally.
- Echo can make posts/react/click when you command it; be explicit for irreversible actions. Prefer reviewing important posts before submitting.

The page is one embedded HTML file with vanilla JavaScript; every piece of
document or model text is inserted with `textContent`, so content cannot inject
markup. There is no login: keep the default localhost binding unless you control
the network.

## Commands

```text
echo-ai init                       create the workspace
echo-ai doctor [--fix]             check model server, index, workspace; --fix repairs
echo-ai repair                     heal folders, index and database
echo-ai chat                       interactive agent with tools
echo-ai ui [--host H] [--port N]   local web interface
echo-ai config [--set K=V ...]     show/save config in $ECHO_HOME/.env
echo-ai sessions [--limit N]       list chat sessions

echo-ai new "Title" [--tag work]   create a doc (body from $EDITOR/stdin/--content)
echo-ai read <id|title>            print a doc
echo-ai edit <id|title>            edit (opens $EDITOR) or use --content/--append/--old/--new
echo-ai list [--tag work]          list docs
echo-ai find "phrase"              search doc content
echo-ai archive <id|title>         move a doc out of the library (reversible)

echo-ai ask "question"             answer from your docs, with sources
echo-ai remember "fact" --kind preference --tag work
echo-ai memories [query]           list/search memories
echo-ai memories --forget 12       delete a memory

echo-ai organize [--apply]         tag suggestions, duplicates, stale docs; --apply files docs
echo-ai research "topic" -n 4      researched briefing saved as a doc

echo-ai post -p linkedin "topic"   draft into the outbox
echo-ai post -p x "topic" --thread 4
echo-ai outbox                     list drafts
echo-ai outbox --show NAME         read one
echo-ai outbox --approve NAME      mark approved for manual posting

echo-ai index [--rebuild]          re-sync/rebuild the search index
```

Chat slash commands: `/help`, `/new <title>`, `/ask <question>`,
`/remember <note>`, `/memories`, `/docs`, `/exit`.

Use `echo-ai --help` or `echo-ai <command> --help` for details.

## Where things live

```text
$ECHO_HOME/                 default ~/.echo
├── docs/                   your markdown documents (subfolders allowed)
│   └── work/...            created by `organize --apply`
├── outbox/                 social drafts, frontmatter includes status: draft|approved
├── archive/                archived documents
└── echo.db                 SQLite: index, chunks, memories, transcript
```

Documents are ordinary markdown with frontmatter:

```markdown
---
title: Launch brief
tags: work, launch
created: 2026-03-01T09:00:00+00:00
updated: 2026-03-02T11:30:00+00:00
---

Body goes here.
```

You can edit files in any editor and run `echo-ai index`; deleted files drop out
of the index automatically. File names are the document ids.

## Memory model

Memory is intentionally simple and inspectable:

- Kinds: `fact`, `preference`, `person`, `project`, `decision`, `task`, `note`
- Deduplicated by text (case-insensitive)
- Stored with tags, source and timestamp in `echo.db`
- Managed with `remember`, `memories`, `memories --forget`, the agent's tools, or the UI

## Social posting is draft-only by design

Echo does **not** have platform credentials and cannot publish. It writes
platform-fit drafts to the outbox:

- Checks the platform character limit, retries once if the draft is too long,
  and warns you if it still is
- Keeps hashtags/emoji restrained unless your notes ask for them
- Saves `status: draft`; `--approve` flips it to `status: approved`

You copy the text into the platform (or wire up your own posting automation)
after reviewing it.

## Configuration

Set environment variables, or put them in `.env` (project root) or
`$ECHO_HOME/.env`. See `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ECHO_HOME` | `~/.echo` | Workspace folder |
| `ECHO_PROVIDER` | `ollama` | `ollama` or `openai` |
| `ECHO_MODEL` | `llama3.2` | Chat model |
| `ECHO_BASE_URL` | provider default | Model server URL |
| `ECHO_API_KEY` | empty | Required for `openai` providers |
| `ECHO_EMBED_MODEL` | empty | Enables semantic search when set |
| `ECHO_TEMPERATURE` | `0.3` | Generation temperature |
| `ECHO_MAX_TOOL_STEPS` | `12` | Tool-call budget per chat turn |
| `ECHO_LLM_RETRIES` | `2` | Retries for transient model failures |
| `ECHO_LLM_RETRY_DELAY` | `1` | Base backoff in seconds (doubles per retry) |
| `ECHO_AUTO_MEMORY` | `1` | Extract memories when a chat ends |
| `ECHO_SYSTEM_PROMPT` | empty | Extra instructions appended to the agent system prompt |
| `ECHO_UI_HOST` | `127.0.0.1` | Default host for `echo-ai ui` |
| `ECHO_UI_PORT` | `8765` | Default port for `echo-ai ui` |

### Optional semantic search

Keyword search works out of the box (SQLite FTS5). For semantic retrieval as
well:

```bash
ollama pull nomic-embed-text
export ECHO_EMBED_MODEL=nomic-embed-text
echo-ai index --rebuild      # computes and stores chunk embeddings
```

Embeddings are cached in the database; without the model, retrieval falls back
to keyword-only automatically.

## Important details to consider

- **The UI has no authentication.** Default binding is `127.0.0.1`; binding to
  `0.0.0.0` exposes your whole document library and the agent's write tools to
  anyone who can reach the port. Use SSH port-forwarding instead of exposing it.
- **`echo.db` is not encrypted.** Documents are plaintext markdown too. Use
  disk-level encryption (FileVault, LUKS, BitLocker) if that matters, and do not
  put secrets in documents or memory — they are sent to the model as context.
- **Everything goes to your model provider.** With `ECHO_PROVIDER=openai`,
  your documents, memories and retrieved excerpts leave your machine. Ollama
  keeps prompts local. Choose accordingly.
- **Model quality is the ceiling.** Tool calling needs a capable model;
  `llama3.1`, `qwen2.5` and `mistral-nemo` work well. Small models sometimes
  emit malformed tool JSON; Echo turns that into an error the model can see, but
  it cannot make a weak model reason well.
- **Prompt size grows with retrieval.** `ask` sends up to `--limit` chunks
  (~1000 chars each) plus the question; chat sends memory plus the last 40
  messages. Long sessions cost more tokens and run slower.
- **`sync()` is incremental but not free.** Every command that reads documents
  stats each `.md` file. Thousands of documents make CLI commands noticeably
  slower; `find` and `ask` still only read the index.
- **Only one writer should run at a time.** SQLite handles concurrent reads, but
  the CLI and UI both re-index and write documents; running heavy writes from
  two processes at once can produce stale index entries, which the next `sync`
  or `repair` fixes.
- **Archiving is a move, not a delete.** `archive` moves the file to `archive/`
  with a timestamp, and it leaves the index. Nothing in Echo deletes a document;
  deleting the file yourself is the only way, and the index follows on the next
  sync.
- **Research depends on DuckDuckGo's HTML endpoint.** It can rate-limit or change
  markup; there is no paid search API key. Failures surface as errors rather
  than fabricated briefings.
- **Semantic search needs a consistent embedding model.** Changing
  `ECHO_EMBED_MODEL` without `echo-ai index --rebuild` leaves old vectors that
  cannot be compared with new query vectors; mismatched lengths score zero
  rather than erroring, so search silently gets worse until you rebuild.
- **Auto-memory is an LLM judgement call.** It can store something you consider
  noise. Review with `echo-ai memories` and remove with `--forget`.
- **Backups are your job.** The workspace is a directory; copy it (or put it in
  a synced folder) and you have a complete backup of documents, memory and
  index.

## Development

```bash
python -m unittest discover -s tests -t . -v
```

The suite covers the document store, memory, retrieval/Q&A, organizing, social
drafts, search HTML parsing, tool dispatch, the agent loop, retries and
workspace repair, and the UI's JSON API — using a scripted fake LLM (no network
or model required).

Layout:

```text
echo_agent/
├── cli.py          commands and chat REPL
├── ui.py           local web UI (server + single-page client)
├── agent.py        system prompt, tool loop, memory extraction
├── tools.py        tool schemas + dispatch
├── docs.py         markdown store, frontmatter, chunking
├── db.py           SQLite schema, access, corruption quarantine
├── retrieval.py    hybrid retrieval
├── qa.py           grounded answers with citations
├── memory.py       long-term memory
├── organize.py     tagging, duplicates, folders
├── research.py     search + page extraction + briefings
├── social.py       platform drafts and outbox
├── heal.py         workspace checks and repair
├── llm.py          model client, error classification, retries
├── embeddings.py   optional embeddings
├── config.py       environment configuration
└── util.py         shared helpers (including atomic writes)
```

## Limitations

- Single-user and local by design: no sync, no multi-user permissions, no
  authentication on the web UI.
- Chat replies are generated locally, so speed and quality depend on the model
  and machine.
- Research depends on DuckDuckGo's HTML endpoint and may be rate-limited.
- Social publishing is manual (see above).
- No OCR/PDF ingestion yet: the library is markdown-first, though text files can
  be pasted in.
