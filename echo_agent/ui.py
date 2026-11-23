"""A local web UI for Echo: one file, standard library only.

Run it with ``echo-ai ui``. The server binds to localhost by default and has no
authentication — it is meant for one person on one machine, so do not expose it
to a network you do not trust.

The page is deliberately server-rendered-by-hand and dependency free: no build
step, no bundler, no CDN. All untrusted text is inserted with ``textContent``,
so document contents can never inject markup into the interface.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List
from urllib.parse import parse_qs, urlparse

from . import heal
from .agent import Agent
from .config import Config
from .db import Database
from .docs import Doc, DocError, DocStore
from .llm import LLM, LLMError
from .memory import MemoryStore
from .qa import ask_from_docs
from .retrieval import Retriever
from .social import SocialError, list_drafts, read_draft, set_status


def _doc_payload(doc: Doc) -> dict:
    return {
        "id": doc.id,
        "title": doc.title,
        "body": doc.body,
        "tags": doc.tags,
        "words": doc.words,
        "updated_at": doc.updated_at,
        "path": doc.path,
    }


def _snapshot(cfg: Config, db: Database, store: DocStore, memory: MemoryStore) -> dict:
    docs = [
        {
            "id": doc.id,
            "title": doc.title,
            "tags": doc.tags,
            "words": doc.words,
            "updated_at": doc.updated_at,
        }
        for doc in store.list_docs(limit=200)
    ]
    memories = [
        {"id": item.id, "kind": item.kind, "text": item.text, "tags": item.tags}
        for item in memory.all(50)
    ]
    drafts = list_drafts(cfg)
    for draft in drafts:
        draft.pop("body", None)
    return {
        "config": {
            "home": str(cfg.home),
            "provider": cfg.provider,
            "model": cfg.model,
            "semantic": bool(cfg.embed_model),
        },
        "docs": docs,
        "memories": memories,
        "drafts": drafts,
        "counts": {"docs": len(docs), "memories": memory.count(), "drafts": len(drafts)},
    }


class _Workspace:
    """Everything the UI needs, owned by the thread that serves requests."""

    def __init__(self, cfg: Config, on_tool) -> None:
        self.db = Database(cfg.db_path)
        self.store = DocStore(cfg, self.db)
        self.memory = MemoryStore(self.db)
        self.agent = Agent(cfg, self.db, self.store, self.memory)
        self.agent.on_tool = on_tool

    def close(self) -> None:
        self.db.close()


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765) -> HTTPServer:
    cfg.ensure_dirs()
    trace: List[Dict[str, str]] = []
    opened: Dict[str, _Workspace] = {}

    def on_tool(name, arguments):
        trace.append(
            {
                "name": name,
                "arguments": ", ".join(str(value)[:80] for value in list(arguments.values())[:3]),
            }
        )

    def workspace() -> _Workspace:
        """Open the workspace on the thread that calls this.

        SQLite connections belong to the thread that created them, so the UI
        keeps its own rather than borrowing the CLI's main-thread connection.
        """
        if "ws" not in opened:
            opened["ws"] = _Workspace(cfg, on_tool)
        return opened["ws"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "EchoUI"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        # -- plumbing ------------------------------------------------------
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _html(self) -> None:
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _payload(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if length <= 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        # -- routes --------------------------------------------------------
        def do_GET(self):  # noqa: N802 - http.server API
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._html()
                return
            if parsed.path == "/api/state":
                try:
                    ws = workspace()
                    self._send(200, _snapshot(cfg, ws.db, ws.store, ws.memory))
                except (OSError, LLMError) as exc:
                    self._send(500, {"error": str(exc)})
                return
            if parsed.path == "/api/health":
                ws = workspace()
                self._send(200, {"problems": heal.check(cfg, ws.db, ws.store)})
                return
            if parsed.path == "/api/doc":
                doc_id = parse_qs(parsed.query).get("id", [""])[0]
                doc = workspace().store.get(doc_id)
                if doc is None:
                    self._send(404, {"error": "No document matches %r." % doc_id})
                    return
                self._send(200, {"doc": _doc_payload(doc)})
                return
            self._send(404, {"error": "Not found."})

        def do_POST(self):  # noqa: N802 - http.server API
            path = urlparse(self.path).path
            data = self._payload()
            try:
                self._post_route(path, data)
            except (DocError, LLMError, SocialError, ValueError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # a broken request must never kill the server
                self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

        def _post_route(self, path: str, data: dict) -> None:
            if path not in {
                "/api/chat",
                "/api/ask",
                "/api/search",
                "/api/doc/save",
                "/api/memory",
                "/api/memory/forget",
                "/api/outbox/show",
                "/api/outbox/approve",
            }:
                self._send(404, {"error": "Not found."})
                return
            ws = workspace()
            if path == "/api/chat":
                trace.clear()
                answer = ws.agent.chat(str(data.get("message") or ""))
                self._send(200, {"answer": answer, "trace": list(trace)})
                return
            if path == "/api/ask":
                ws.store.sync()
                answer, hits = ask_from_docs(
                    cfg,
                    Retriever(cfg, ws.db),
                    ws.agent.llm,
                    str(data.get("question") or ""),
                )
                sources = []
                seen = set()
                for hit in hits:
                    if hit.doc_id in seen:
                        continue
                    seen.add(hit.doc_id)
                    sources.append({"id": hit.doc_id, "title": hit.title})
                self._send(200, {"answer": answer, "sources": sources})
                return
            if path == "/api/search":
                ws.store.sync()
                hits = Retriever(cfg, ws.db).search(str(data.get("query") or ""), limit=10)
                self._send(200, {"results": [hit.as_dict() for hit in hits]})
                return
            if path == "/api/doc/save":
                doc_id = str(data.get("id") or "").strip()
                title = str(data.get("title") or "").strip()
                body = str(data.get("body") or "")
                tags = str(data.get("tags") or "")
                if doc_id:
                    doc = ws.store.update(doc_id, body=body, title=title or None, tags=tags)
                else:
                    doc = ws.store.create(title, body, tags)
                self._send(200, {"doc": _doc_payload(doc)})
                return
            if path == "/api/memory":
                item, created = ws.memory.add(
                    str(data.get("text") or ""),
                    kind=str(data.get("kind") or "note"),
                    source="ui",
                )
                self._send(
                    200,
                    {
                        "memory": {
                            "id": item.id,
                            "kind": item.kind,
                            "text": item.text,
                            "tags": item.tags,
                        },
                        "created": created,
                    },
                )
                return
            if path == "/api/memory/forget":
                memory_id = int(data.get("id") or 0)
                if not ws.memory.forget(memory_id):
                    self._send(404, {"error": "No memory with id %d." % memory_id})
                    return
                self._send(200, {"forgot": memory_id})
                return
            if path == "/api/outbox/show":
                self._send(200, {"draft": read_draft(cfg, str(data.get("name") or ""))})
                return
            if path == "/api/outbox/approve":
                path_ = set_status(cfg, str(data.get("name") or ""), "approved")
                self._send(200, {"approved": path_.name})
                return

    class Server(HTTPServer):
        """Open the workspace before serving and close it on the same thread."""

        def serve_forever(self, poll_interval: float = 0.5) -> None:
            workspace()
            try:
                super().serve_forever(poll_interval)
            finally:
                ws = opened.pop("ws", None)
                if ws is not None:
                    ws.close()

    httpd = Server((host, port), Handler)
    httpd.echo_config = cfg  # handy for callers and tests
    return httpd


def run(cfg: Config, host: str, port: int) -> None:
    httpd = serve(cfg, host=host, port=port)
    shown = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print("Echo UI on http://%s:%d  (Ctrl-C to stop)" % (shown, port))
    print("Workspace: %s  ·  model: %s" % (cfg.home, cfg.model))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Echo</title>
<style>
:root{
  --bg:#0e1116; --panel:#151a21; --panel-2:#1b222c; --edge:#252d39;
  --text:#e7ebf1; --muted:#8e98a8; --accent:#7fd7aa; --accent-ink:#0d1a14; --danger:#e08d8d;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
button,input,textarea,select{font:inherit;color:inherit}
.app{display:flex;height:100vh}
aside{width:232px;flex:none;background:var(--panel);border-right:1px solid var(--edge);
  display:flex;flex-direction:column;padding:20px 14px;gap:18px}
.brand{font-size:20px;font-weight:650;letter-spacing:.02em}
.brand span{color:var(--accent)}
.tagline{color:var(--muted);font-size:12px;margin-top:2px;line-height:1.4}
nav{display:flex;flex-direction:column;gap:4px}
nav button{all:unset;cursor:pointer;padding:9px 11px;border-radius:9px;color:var(--muted);
  font-size:14px;display:flex;justify-content:space-between;align-items:center}
nav button:hover{background:var(--panel-2);color:var(--text)}
nav button.on{background:var(--panel-2);color:var(--text);box-shadow:inset 2px 0 0 var(--accent)}
nav button b{font-weight:500;color:var(--muted);font-size:12px}
.meta{margin-top:auto;color:var(--muted);font-size:11.5px;line-height:1.6;
  border-top:1px solid var(--edge);padding-top:12px;word-break:break-all}
main{flex:1;min-width:0;display:flex;flex-direction:column}
.panel{display:none;flex:1;min-height:0;flex-direction:column}
.panel.on{display:flex}
.head{padding:16px 22px;border-bottom:1px solid var(--edge);display:flex;
  align-items:center;gap:12px;justify-content:space-between}
.head h1{font-size:15px;margin:0;font-weight:600}
.head p{margin:2px 0 0;color:var(--muted);font-size:12.5px}
.body{flex:1;min-height:0;overflow:auto;padding:20px 22px}
.row{display:flex;gap:10px;align-items:center}
.grow{flex:1}
input,textarea,select{background:var(--panel);border:1px solid var(--edge);border-radius:9px;
  padding:9px 11px;outline:none;width:100%}
input:focus,textarea:focus,select:focus{border-color:var(--accent)}
textarea{resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;line-height:1.6}
.btn{all:unset;cursor:pointer;background:var(--accent);color:var(--accent-ink);
  padding:9px 16px;border-radius:9px;font-weight:600;font-size:13.5px;text-align:center}
.btn:hover{filter:brightness(1.07)}
.btn.ghost{background:transparent;color:var(--text);border:1px solid var(--edge);font-weight:500}
.btn.tiny{padding:4px 10px;font-size:12px;border-radius:7px}
.btn[disabled]{opacity:.5;cursor:default}
.banner{display:none;margin:0 22px 0;padding:10px 12px;border-radius:9px;font-size:12.5px;
  background:#2a2320;border:1px solid #4a3a30;color:#f0c9a6}
.banner.on{display:block}
/* chat */
.chat{display:flex;flex-direction:column;height:100%}
.msgs{flex:1;overflow:auto;padding:20px 22px;display:flex;flex-direction:column;gap:16px}
.msg{max-width:760px;white-space:pre-wrap;overflow-wrap:anywhere}
.msg .who{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin-bottom:5px}
.msg.you{color:var(--muted)}
.msg.echo .text{background:var(--panel);border:1px solid var(--edge);border-radius:12px;padding:12px 14px}
.msg.err .text{background:#2a1e1e;border-color:#4a2f2f;color:var(--danger)}
.trace{margin-top:8px;color:var(--muted);font-size:11.5px;font-family:ui-monospace,Menlo,monospace}
.composer{padding:14px 22px 18px;border-top:1px solid var(--edge);display:flex;gap:10px;align-items:flex-end}
.composer textarea{min-height:52px;max-height:200px;font-family:inherit;font-size:14px}
/* docs */
.split{display:flex;height:100%;min-height:0}
.docs-list{width:280px;flex:none;border-right:1px solid var(--edge);overflow:auto;padding:12px}
.doc{border-radius:9px;padding:9px 11px;cursor:pointer}
.doc:hover{background:var(--panel)}
.doc.on{background:var(--panel-2)}
.doc .t{font-size:13.5px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.doc .s{font-size:11.5px;color:var(--muted);margin-top:2px}
.docs-main{flex:1;min-width:0;overflow:auto;padding:18px 20px;display:flex;flex-direction:column;gap:10px}
.docs-main .row{margin-bottom:2px}
.hint{color:var(--muted);font-size:12px}
.empty{color:var(--muted);font-size:13px;padding:10px 2px}
/* lists */
.item{background:var(--panel);border:1px solid var(--edge);border-radius:11px;
  padding:12px 14px;margin-bottom:10px}
.item .top{display:flex;justify-content:space-between;gap:12px;align-items:center}
.kind{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);
  background:rgba(127,215,170,.1);padding:2px 7px;border-radius:99px}
pre.draft{white-space:pre-wrap;background:var(--panel-2);border:1px solid var(--edge);
  border-radius:9px;padding:12px;margin:10px 0 0;font-size:13px}
.status{font-size:11.5px;color:var(--muted)}
@media (max-width:760px){
  aside{width:64px;padding:14px 8px}
  .brand .full,.tagline,nav button b,.meta{display:none}
  nav button{justify-content:center}
  .docs-list{width:190px}
}
</style>
</head>
<body>
<div class="app">
  <aside>
    <div>
      <div class="brand">Ec<span>h</span>o<span class="full"></span></div>
      <div class="tagline">local-first assistant</div>
    </div>
    <nav>
      <button data-tab="chat" class="on">Chat</button>
      <button data-tab="docs">Documents <b id="count-docs">0</b></button>
      <button data-tab="ask">Ask docs</button>
      <button data-tab="memory">Memory <b id="count-mem">0</b></button>
      <button data-tab="outbox">Outbox <b id="count-drafts">0</b></button>
    </nav>
    <div class="meta" id="meta">loading…</div>
  </aside>

  <main>
    <div class="banner" id="banner"></div>

    <section class="panel on" id="panel-chat">
      <div class="head">
        <div><h1>Chat</h1><p>Tools are enabled: documents, memory, research, drafts.</p></div>
        <button class="btn ghost tiny" id="new-chat">Clear view</button>
      </div>
      <div class="chat">
        <div class="msgs" id="msgs"></div>
        <div class="composer">
          <textarea id="chat-input" placeholder="Ask anything, or ask Echo to write, search, research…"></textarea>
          <button class="btn" id="send">Send</button>
        </div>
      </div>
    </section>

    <section class="panel" id="panel-docs">
      <div class="head">
        <div><h1>Documents</h1><p>Plain markdown in your workspace.</p></div>
        <button class="btn ghost tiny" id="new-doc">New document</button>
      </div>
      <div class="split">
        <div class="docs-list" id="docs-list"></div>
        <div class="docs-main">
          <div class="row">
            <input id="doc-title" placeholder="Title" />
          </div>
          <div class="row">
            <input id="doc-tags" placeholder="tags, comma separated" />
          </div>
          <textarea id="doc-body" placeholder="Markdown body" style="min-height:340px"></textarea>
          <div class="row">
            <button class="btn" id="save-doc">Save</button>
            <span class="hint" id="doc-status"></span>
          </div>
        </div>
      </div>
    </section>

    <section class="panel" id="panel-ask">
      <div class="head">
        <div><h1>Ask your documents</h1><p>Answers grounded in your library, with sources.</p></div>
      </div>
      <div class="chat">
        <div class="msgs" id="ask-msgs"></div>
        <div class="composer">
          <textarea id="ask-input" placeholder="What did I decide about pricing?"></textarea>
          <button class="btn" id="ask">Ask</button>
        </div>
      </div>
    </section>

    <section class="panel" id="panel-memory">
      <div class="head"><div><h1>Memory</h1><p>Durable facts, preferences, projects.</p></div></div>
      <div class="body">
        <div class="row" style="margin-bottom:16px">
          <input id="mem-text" placeholder="Something worth remembering" class="grow" />
          <select id="mem-kind" style="width:150px">
            <option value="note">note</option><option value="fact">fact</option>
            <option value="preference">preference</option><option value="person">person</option>
            <option value="project">project</option><option value="decision">decision</option>
            <option value="task">task</option>
          </select>
          <button class="btn" id="mem-save">Remember</button>
        </div>
        <div id="mem-list"></div>
      </div>
    </section>

    <section class="panel" id="panel-outbox">
      <div class="head"><div><h1>Outbox</h1><p>Drafts stay drafts until you approve them. Echo never publishes.</p></div></div>
      <div class="body"><div id="draft-list"></div></div>
    </section>
  </main>
</div>

<script>
const $ = (id) => document.getElementById(id);
let docs = [];
let currentId = null;

async function api(path, body) {
  const options = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : undefined;
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

function node(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function setTab(name) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("on", p.id === "panel-" + name));
  if (name === "docs") loadDocs();
}

document.querySelectorAll("nav button").forEach((button) => {
  button.addEventListener("click", () => setTab(button.dataset.tab));
});

/* ---- chat ---- */
function addMessage(container, role, text, trace) {
  const wrap = node("div", "msg " + role);
  wrap.appendChild(node("div", "who", role === "you" ? "you" : role === "err" ? "error" : "echo"));
  const bubble = node("div", "text", text);
  wrap.appendChild(bubble);
  if (trace && trace.length) {
    const t = node("div", "trace");
    trace.forEach((call) => t.appendChild(node("div", "", "→ " + call.name + "(" + call.arguments + ")")));
    wrap.appendChild(t);
  }
  container.appendChild(wrap);
  container.scrollTop = container.scrollHeight;
}

let chatBusy = false;
$("send").addEventListener("click", sendChat);
$("chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendChat(); }
});
$("new-chat").addEventListener("click", () => { $("msgs").replaceChildren(); });

async function sendChat() {
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text || chatBusy) return;
  addMessage($("msgs"), "you", text);
  input.value = "";
  chatBusy = true; $("send").disabled = true;
  try {
    const data = await api("/api/chat", { message: text });
    addMessage($("msgs"), "echo", data.answer, data.trace);
  } catch (error) {
    addMessage($("msgs"), "err", error.message);
  } finally {
    chatBusy = false; $("send").disabled = false; input.focus();
    refreshState();
  }
}

/* ---- ask ---- */
let askBusy = false;
$("ask").addEventListener("click", askDocs);
$("ask-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); askDocs(); }
});

async function askDocs() {
  const input = $("ask-input");
  const text = input.value.trim();
  if (!text || askBusy) return;
  addMessage($("ask-msgs"), "you", text);
  input.value = "";
  askBusy = true; $("ask").disabled = true;
  try {
    const data = await api("/api/ask", { question: text });
    addMessage($("ask-msgs"), "echo", data.answer);
    if (data.sources && data.sources.length) {
      const wrap = node("div", "trace");
      data.sources.forEach((s) => wrap.appendChild(node("div", "", "source · " + s.title + " (" + s.id + ")")));
      $("ask-msgs").appendChild(wrap);
    }
  } catch (error) {
    addMessage($("ask-msgs"), "err", error.message);
  } finally {
    askBusy = false; $("ask").disabled = false;
  }
}

/* ---- documents ---- */
async function loadDocs() {
  try {
    const data = await api("/api/state");
    docs = data.docs;
    renderDocList();
  } catch (error) {
    $("docs-list").replaceChildren(node("div", "empty", error.message));
  }
}

function renderDocList() {
  const list = $("docs-list");
  list.replaceChildren();
  if (!docs.length) {
    list.appendChild(node("div", "empty", "No documents yet."));
    return;
  }
  docs.forEach((doc) => {
    const item = node("div", "doc" + (doc.id === currentId ? " on" : ""));
    item.appendChild(node("div", "t", doc.title));
    const sub = doc.updated_at.slice(0, 10) + " · " + doc.words + " words";
    if (doc.tags.length) sub += " · #" + doc.tags.join(" #");
    item.appendChild(node("div", "s", sub));
    item.addEventListener("click", () => openDoc(doc.id));
    list.appendChild(item);
  });
}

async function openDoc(id) {
  try {
    const data = await api("/api/doc?id=" + encodeURIComponent(id));
    currentId = data.doc.id;
    $("doc-title").value = data.doc.title;
    $("doc-tags").value = data.doc.tags.join(", ");
    $("doc-body").value = data.doc.body;
    $("doc-status").textContent = data.doc.path;
    renderDocList();
  } catch (error) {
    $("doc-status").textContent = error.message;
  }
}

$("new-doc").addEventListener("click", () => {
  currentId = null;
  $("doc-title").value = "";
  $("doc-tags").value = "";
  $("doc-body").value = "";
  $("doc-status").textContent = "new document";
  $("doc-title").focus();
  renderDocList();
});

$("save-doc").addEventListener("click", async () => {
  const payload = {
    id: currentId || "",
    title: $("doc-title").value.trim(),
    tags: $("doc-tags").value,
    body: $("doc-body").value,
  };
  if (!payload.title) { $("doc-status").textContent = "A title is required."; return; }
  $("save-doc").disabled = true;
  try {
    const data = await api("/api/doc/save", payload);
    currentId = data.doc.id;
    $("doc-status").textContent = "saved · " + data.doc.updated_at;
    await refreshState();
    renderDocList();
  } catch (error) {
    $("doc-status").textContent = error.message;
  } finally {
    $("save-doc").disabled = false;
  }
});

/* ---- memory ---- */
$("mem-save").addEventListener("click", async () => {
  const text = $("mem-text").value.trim();
  if (!text) return;
  try {
    await api("/api/memory", { text: text, kind: $("mem-kind").value });
    $("mem-text").value = "";
    await refreshState();
  } catch (error) {
    alert(error.message);
  }
});

function renderMemories(memories) {
  const list = $("mem-list");
  list.replaceChildren();
  if (!memories.length) {
    list.appendChild(node("div", "empty", "Nothing remembered yet."));
    return;
  }
  memories.forEach((memory) => {
    const item = node("div", "item");
    const top = node("div", "top");
    top.appendChild(node("span", "kind", memory.kind));
    const forget = node("button", "btn ghost tiny", "Forget");
    forget.addEventListener("click", async () => {
      try { await api("/api/memory/forget", { id: memory.id }); await refreshState(); }
      catch (error) { alert(error.message); }
    });
    top.appendChild(forget);
    item.appendChild(top);
    item.appendChild(node("div", "", memory.text));
    list.appendChild(item);
  });
}

/* ---- outbox ---- */
function renderDrafts(drafts) {
  const list = $("draft-list");
  list.replaceChildren();
  if (!drafts.length) {
    list.appendChild(node("div", "empty", "No drafts. Ask the agent for one, or run echo-ai post."));
    return;
  }
  drafts.forEach((draft) => {
    const item = node("div", "item");
    const top = node("div", "top");
    const left = node("div");
    left.appendChild(node("div", "", draft.name));
    left.appendChild(node("div", "status", draft.platform + " · " + draft.status + " · " + draft.chars + " chars"));
    top.appendChild(left);
    const actions = node("div", "row");
    const show = node("button", "btn ghost tiny", "View");
    const approve = node("button", "btn tiny", "Approve");
    const pre = node("pre", "draft");
    show.addEventListener("click", async () => {
      try {
        const data = await api("/api/outbox/show", { name: draft.name });
        pre.textContent = data.draft.body;
        item.appendChild(pre);
        show.remove();
      } catch (error) { alert(error.message); }
    });
    approve.addEventListener("click", async () => {
      try { await api("/api/outbox/approve", { name: draft.name }); await refreshState(); }
      catch (error) { alert(error.message); }
    });
    actions.appendChild(show);
    actions.appendChild(approve);
    top.appendChild(actions);
    item.appendChild(top);
    list.appendChild(item);
  });
}

/* ---- state ---- */
async function refreshState() {
  try {
    const data = await api("/api/state");
    docs = data.docs;
    $("count-docs").textContent = data.counts.docs;
    $("count-mem").textContent = data.counts.memories;
    $("count-drafts").textContent = data.counts.drafts;
    $("meta").textContent = data.config.model + " · " + data.config.provider + "\\n" + data.config.home;
    renderMemories(data.memories);
    renderDrafts(data.drafts);
    if ($("panel-docs").classList.contains("on")) renderDocList();
  } catch (error) {
    $("meta").textContent = error.message;
  }
  try {
    const health = await api("/api/health");
    const banner = $("banner");
    if (health.problems.length) {
      banner.textContent = "Workspace needs repair: " + health.problems.join("; ") +
        "  — run: echo-ai repair";
      banner.classList.add("on");
    } else {
      banner.classList.remove("on");
    }
  } catch (error) {
    /* health is best-effort */
  }
}

refreshState();
</script>
</body>
</html>
"""
