# Echo desktop app

A native Electron window for the Echo agent, built for **macOS** (Apple Silicon
and Intel) and **Windows** (x64). It is a new UI, not the embedded browser page:
native menus, shortcuts, session list, and a renderer that talks to Echo's JSON
API over a local IPC bridge.

The agent itself is unchanged. The app starts Echo's HTTP API on a random
localhost port and bundles it as a small Python sidecar, so the released app
does **not** require Python, Node, or a browser on the user's machine. The model
runs wherever you already run it — LM Studio, Ollama, or an OpenAI-compatible
endpoint.

## What is in here

```text
desktop/
├── src/main/            Electron process, backend lifecycle, attachments, native menus, preload bridge
├── src/renderer/        Native UI (HTML/CSS/JS, no bundler or CDN)
├── scripts/             Sidecar build and icon generation
├── build/               App icons
└── tests/               Node unit tests for the backend lifecycle
```

The Python side is `echo_agent/sidecar.py`; it calls the existing
`echo_agent.ui` server. No API routes are duplicated.

## Development

Requires Node 20+ and Python 3.9+. With LM Studio already running on
`http://127.0.0.1:1234/v1`:

```bash
cd desktop
npm install
npm start
```

On first launch the app detects LM Studio and its loaded models, then writes the
choice to `~/.echo/.env`. If you already have an Echo config, the app skips
setup. Override the workspace or Python interpreter with:

```bash
ECHO_HOME=~/.echo-work ECHO_PYTHON=python3.13 npm start
```

In development the app runs `python -m echo_agent.sidecar` from the repository
root. In packaged builds it runs `resources/backend/echo-sidecar`.

## Conversation and browser behavior

- The chat pane is height-constrained, so long conversations scroll inside the
  window instead of stretching it.
- Context is compacted before it exceeds `ECHO_CONTEXT_CHARS` (14,000 characters
  by default). Older turns are replaced in the model prompt with a short
  summary; the full transcript stays in the session database and the UI. Set
  `ECHO_CONTEXT_SUMMARY_CHARS` to tune the summary size.
- Transient model failures are retried with exponential backoff
  (`ECHO_LLM_RETRIES`, `ECHO_LLM_RETRY_DELAY`). All of these values are editable
  in Settings.
- Backend startup installs Playwright's Chromium in the background if it is
  missing. Progress is shown in the Browser view, with an **Install browser**
  button to retry. Set `ECHO_BROWSER_AUTO_INSTALL=0` to disable this.
- The paperclip in the chat composer attaches text, Markdown, CSV, JSON and code
  files (up to 12 files, 2 MB each). Their contents are added to that question's
  prompt only; they are not copied into the workspace. Web search uses Bing with
  DuckDuckGo as a fallback, so it keeps working when one provider blocks scraping.

## File attachments

Click **Attach** in the chat composer, pick one or more text files, then ask your
question. Attached files appear as removable chips above the composer and are
sent with that message. Binary files and files over 2 MB are rejected with a
message in the banner. Attachments are bounded to 60,000 characters total and
20,000 per file before the agent's normal context compaction applies.

## Tests and checks

```bash
cd desktop
npm test    # Node tests for config, IPC API proxy, backend resolution
npm run check
cd .. && python3 -m unittest discover -s tests -t . -v
```

## Build a standalone app

Install the Python build dependencies once:

```bash
python3 -m pip install pyinstaller playwright
```

Playwright is bundled into the sidecar so the packaged app can install Chromium
on first launch; the browser binary itself is not part of the download.

Then, **on the target platform**:

```bash
cd desktop
npm run build:mac        # macOS .dmg + .zip for this Mac's architecture
npm run build:mac:arm64  # explicit Apple Silicon build
npm run build:mac:x64    # explicit Intel build (run on an Intel Mac)
npm run build:win        # Windows .exe installer + portable .exe
```

The sidecar is built first into `dist/backend`, then electron-builder packages
it into `dist/app`. PyInstaller cannot cross-compile the sidecar, so each macOS
architecture is built on a native runner; the CI workflow does this for both.
Windows executables cannot be produced on macOS; use the GitHub Actions
workflow below (or a Windows machine) for that.

Unsigned macOS builds work, but Gatekeeper will ask you to open them via
right-click → Open on first launch. Notarization requires an Apple Developer
certificate and signing environment variables.

## GitHub Actions

`.github/workflows/build-desktop.yml` builds Apple Silicon macOS, Intel macOS
and Windows on native runners (`workflow_dispatch` or `v*` tags). Download the
DMG/ZIP and EXE files from the workflow run's artifacts.

## Native shortcuts

| Shortcut | Action |
| --- | --- |
| `Cmd/Ctrl+N` | New chat |
| `Cmd/Ctrl+Shift+N` | New document |
| `Cmd/Ctrl+1` … `6` | Switch views |
| `Cmd/Ctrl+,` | Settings |
| `Cmd/Ctrl+Shift+O` | Open workspace folder |
| `Cmd/Ctrl+R` | Refresh |
| `Enter` / `Shift+Enter` | Send message / newline |
