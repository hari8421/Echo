# Echo

A local-first personal assistant for your day-to-day work: it writes and edits
documents, answers questions from them, remembers what matters, keeps the
library tidy, researches topics on the web, and drafts social posts for you to
approve.

Everything lives in a folder you own (`~/.echo` by default): plain markdown
documents, a SQLite index, a memory table, and a social-draft outbox. No account,
no cloud database, no telemetry.

## What it does

| Area | What you get |
| --- | --- |
| **Documents** | Create, read, edit, append, tag, archive and search markdown files. `$EDITOR` support. |
| **Ask your docs** | Hybrid keyword + optional semantic retrieval, grounded answers with citations, never invented sources. |
| **Memory** | Durable facts, preferences, people, projects and decisions in SQLite. Saved explicitly or extracted from chats. |
| **Organize** | Suggests tags for untagged docs, finds near-duplicates, groups by tag, flags stale notes, and can file docs into tag folders. |
| **Research** | Web search, page reading, and a sourced markdown briefing saved to your library. |
| **Social drafts** | Platform-aware posts and threads (X, LinkedIn, Bluesky, Threads, Instagram, Facebook, Reddit) saved to an outbox. **Nothing is published automatically.** |
| **Chat agent** | A tool-calling assistant that can use all of the above in one conversation. |

## Requirements

- Python 3.9+
- A local model via [Ollama](https://ollama.com) (default), or any OpenAI-compatible endpoint

No Python packages are required at runtime; Echo uses only the standard library.

## Quickstart

```bash
# 1. Local model (recommended: a model that supports tool calling)
ollama pull llama3.2

# 2. Install the CLI
pip install -e .

# 3. Create the workspace and verify everything
echo-ai init
echo-ai doctor

# 4. Start using it
echo-ai new "Project kickoff"          # opens $EDITOR
echo-ai ask "What did I decide about pricing?"
echo-ai chat
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

## Commands

```text
echo-ai init                       create the workspace
echo-ai doctor                     check model server, index, workspace
echo-ai chat                       interactive agent with tools

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

Use `echo-ai --help` or `echo-ai <command> --help` for details.

## Inside the chat agent

`echo-ai chat` runs a tool loop: the model asks for a tool, Echo executes it,
and the result goes back to the model until it answers. Tools available:

- `create_doc`, `update_doc`, `edit_doc`, `append_to_doc`, `read_doc`, `list_docs`, `search_docs`
- `organize_docs`
- `remember`, `recall`
- `web_search`, `fetch_url`, `research_topic`
- `draft_social_post`, `list_outbox`

Chat slash commands: `/help`, `/new <title>`, `/ask <question>`,
`/remember <note>`, `/memories`, `/docs`, `/exit`.

On exit, Echo asks the model to extract a few durable memories from the
session (disable with `ECHO_AUTO_MEMORY=0`). Tool calling requires a capable
model; `llama3.1`, `qwen2.5`, `mistral-nemo` and similar work well.

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
- Managed with `remember`, `memories`, `memories --forget`, or the agent's tools

Secrets should not go into memory. It is a plain SQLite file, not encrypted.

## Research

`echo-ai research` searches DuckDuckGo's HTML endpoint, reads up to `-n` pages,
and asks the model for a 350–550 word briefing with inline `[S1]`-style
attribution. Echo appends the real source links itself, so citations always
resolve. Reports are saved with `research` plus a topic tag.

Web access is required; a failed search or unreadable pages fail with a clear
message instead of a fabricated report.

## Social posting is draft-only by design

Echo does **not** have platform credentials and cannot publish. It writes
platform-fit drafts to the outbox:

- Checks the platform character limit, retries once if the draft is too long,
  and warns you if it still is
- Keeps hashtags/emoji restrained unless your notes ask for them
- Saves `status: draft`; `--approve` flips it to `status: approved`

You copy the text into the platform (or wire up your own posting automation)
after reviewing it. This keeps an LLM from posting publicly on your behalf
without a human check.

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
| `ECHO_AUTO_MEMORY` | `1` | Extract memories when a chat ends |

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

## Development

```bash
python -m unittest discover -s tests -t . -v
```

The suite covers the document store, memory, retrieval/Q&A, organizing, social
drafts, search HTML parsing, tool dispatch and the agent loop, using a scripted
fake LLM (no network or model required).

Layout:

```text
echo_agent/
├── cli.py          commands and chat REPL
├── agent.py        system prompt, tool loop, memory extraction
├── tools.py        tool schemas + dispatch
├── docs.py         markdown store, frontmatter, chunking
├── db.py           SQLite schema and access
├── retrieval.py    hybrid retrieval
├── qa.py           grounded answers with citations
├── memory.py       long-term memory
├── organize.py     tagging, duplicates, folders
├── research.py     search + page extraction + briefings
├── social.py       platform drafts and outbox
├── llm.py          Ollama / OpenAI-compatible client
├── embeddings.py   optional embeddings
├── config.py       environment configuration
└── util.py         shared helpers
```

## Limitations

- Single-user and local by design: no sync, no multi-user permissions.
- Chat replies are generated locally, so speed and quality depend on the model
  and machine.
- Research depends on DuckDuckGo's HTML endpoint and may be rate-limited; there
  is no paid search API key.
- Social publishing is manual (see above).
- No OCR/PDF ingestion yet: the library is markdown-first, though text files can
  be pasted in.
