"""Optional browser automation for Echo.

Uses Playwright when installed: `pip install -e .[browser]` then
`python -m playwright install chromium`.

Design goals:
- local-first, optional dependency
- explicit actions, visible results
- no hidden credentials; uses existing browser profile only if user requests
- dangerous social actions are possible but should be user-directed
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


class BrowserError(Exception):
    pass


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:
        raise BrowserError(
            "Browser automation requires Playwright. Install with: pip install -e .[browser] "
            "and then: python -m playwright install chromium"
        ) from exc
    return sync_playwright


# Playwright's sync API binds an asyncio loop to the thread that starts it and
# refuses a second start on that thread. Echo's HTTP server handles every
# request on one thread, so keep a single manager for that thread and reuse it
# across browser_start / stop / tool calls. Never stop it between sessions.
_PW_LOCAL = threading.local()


def _get_playwright():
    pw = getattr(_PW_LOCAL, "pw", None)
    if pw is None:
        sync_playwright = _require_playwright()
        _prepare_browsers_env(_playwright_package_dir())
        pw = sync_playwright().start()
        _PW_LOCAL.pw = pw
    return pw


@dataclass
class BrowserState:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    headless: bool
    profile_dir: str = ""


class BrowserSession:
    def __init__(self):
        self.state: Optional[BrowserState] = None

    @property
    def is_open(self) -> bool:
        return self.state is not None

    def start(self, headless: bool = False, profile_dir: str = "") -> Dict[str, Any]:
        if self.state is not None:
            return self.status()
        pw = _get_playwright()
        profile_dir = (profile_dir or "").strip()
        resolved_profile = ""
        context = None
        browser = None
        try:
            if profile_dir:
                path = Path(profile_dir).expanduser()
                path.mkdir(parents=True, exist_ok=True)
                resolved_profile = str(path)
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=resolved_profile,
                    headless=bool(headless),
                    viewport={"width": 1280, "height": 900},
                )
                browser = context.browser
                page = context.pages[0] if context.pages else context.new_page()
            else:
                browser = pw.chromium.launch(headless=bool(headless))
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
        except Exception as exc:
            # A half-started session must not leave the shared Playwright loop
            # unusable for the next request. Close whatever was created.
            for obj in (context, browser):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:
                    pass
            message = str(exc)
            if "executable doesn't exist" in message.lower():
                message = (
                    "Chromium is not installed yet. Wait for the background install in the "
                    "Browser view, or click Install browser. " + message
                )
            raise BrowserError("Cannot start browser: " + message[:500]) from exc
        self.state = BrowserState(pw, browser, context, page, bool(headless), resolved_profile)
        return self.status()

    def _page(self):
        if self.state is None:
            raise BrowserError("Browser is not running. Call browser_start first.")
        return self.state.page

    def status(self) -> Dict[str, Any]:
        if self.state is None:
            return {"running": False}
        page = self.state.page
        return {
            "running": True,
            "headless": self.state.headless,
            "profile_dir": self.state.profile_dir,
            "url": page.url,
            "title": page.title() if not page.is_closed() else "",
        }

    def close(self) -> Dict[str, Any]:
        if self.state is None:
            return {"running": False, "closed": False}
        state = self.state
        self.state = None
        errors = []
        # Keep the shared Playwright manager alive: restarting its sync loop on
        # this thread would raise "using Playwright Sync API inside the asyncio loop".
        for obj, label in [(state.context, "context"), (state.browser, "browser")]:
            try:
                if obj is not None:
                    obj.close()
            except Exception as exc:
                errors.append("%s: %s" % (label, exc))
        return {"running": False, "closed": True, "errors": errors}

    def goto(self, url: str, wait_until: str = "domcontentloaded") -> Dict[str, Any]:
        page = self._page()
        url = (url or "").strip()
        if not url:
            raise BrowserError("url is required")
        if not re.match(r"^https?://", url):
            url = "https://" + url
        resp = page.goto(url, wait_until=wait_until, timeout=30000)
        page.wait_for_timeout(500)
        return {
            "url": page.url,
            "title": page.title(),
            "status": resp.status if resp else None,
        }

    def snapshot(self, limit: int = 80) -> Dict[str, Any]:
        page = self._page()
        # Collect visible interactive elements with simple refs.
        js = r"""
(() => {
  const sel = 'a,button,input,textarea,select,[role="button"],[contenteditable="true"]';
  const nodes = Array.from(document.querySelectorAll(sel));
  const visible = nodes.filter(n => {
    const r = n.getBoundingClientRect();
    const s = window.getComputedStyle(n);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  }).slice(0, 200);
  return visible.map((n, i) => {
    n.setAttribute('data-echo-ref', 'e' + (i+1));
    const r = n.getBoundingClientRect();
    const label = (n.innerText || n.value || n.getAttribute('aria-label') || n.getAttribute('placeholder') || n.getAttribute('name') || '').trim().replace(/\s+/g,' ').slice(0,140);
    return {
      ref: 'e' + (i+1),
      tag: n.tagName.toLowerCase(),
      type: n.getAttribute('type') || '',
      role: n.getAttribute('role') || '',
      label,
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)
    };
  });
})()
"""
        elements = page.evaluate(js)
        text = page.inner_text("body")
        text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
        return {
            "url": page.url,
            "title": page.title(),
            "text": text[:8000],
            "elements": elements[: max(1, min(int(limit or 80), 200))],
        }

    def _locator_for_ref(self, ref: str):
        page = self._page()
        ref = (ref or "").strip()
        if not ref:
            raise BrowserError("ref is required")
        return page.locator('[data-echo-ref="%s"]' % ref.replace('"', ""))

    def click(self, ref: str) -> Dict[str, Any]:
        loc = self._locator_for_ref(ref)
        if loc.count() == 0:
            raise BrowserError("No element for ref %r. Run browser_snapshot again." % ref)
        loc.first.click(timeout=10000)
        page = self._page()
        page.wait_for_timeout(500)
        return {"clicked": ref, "url": page.url, "title": page.title()}

    def fill(self, ref: str, text: str, submit: bool = False) -> Dict[str, Any]:
        loc = self._locator_for_ref(ref)
        if loc.count() == 0:
            raise BrowserError("No element for ref %r. Run browser_snapshot again." % ref)
        loc.first.click(timeout=10000)
        try:
            loc.first.fill(text or "", timeout=10000)
        except Exception:
            loc.first.press("Control+A")
            loc.first.type(text or "", timeout=10000)
        if submit:
            loc.first.press("Enter")
        page = self._page()
        page.wait_for_timeout(400)
        return {"filled": ref, "submit": bool(submit), "url": page.url}

    def press(self, key: str) -> Dict[str, Any]:
        page = self._page()
        key = (key or "").strip()
        if not key:
            raise BrowserError("key is required")
        page.keyboard.press(key)
        page.wait_for_timeout(300)
        return {"pressed": key, "url": page.url}

    def scroll(self, dy: int = 800) -> Dict[str, Any]:
        page = self._page()
        page.mouse.wheel(0, int(dy))
        page.wait_for_timeout(300)
        return {"scrolled": int(dy), "url": page.url}

    def screenshot(self, path: str = "") -> Dict[str, Any]:
        page = self._page()
        if path:
            out = Path(path).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
        else:
            out = Path(".echo-browser-" + str(int(time.time())) + ".png")
        page.screenshot(path=str(out), full_page=False)
        return {"path": str(out.resolve()), "url": page.url}

    def eval_js(self, script: str) -> Dict[str, Any]:
        page = self._page()
        script = (script or "").strip()
        if not script:
            raise BrowserError("script is required")
        result = page.evaluate(script)
        try:
            encoded = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            encoded = str(result)
        return {"result": json.loads(encoded) if encoded.startswith("{") or encoded.startswith("[") or encoded.startswith('"') else encoded}


_SESSION = BrowserSession()


def get_session() -> BrowserSession:
    return _SESSION


# -- Chromium bootstrap ----------------------------------------------------
# Playwright's Python package is bundled with the desktop sidecar. Its browser
# binary is large and platform-specific, so it is installed into the user's
# Playwright cache the first time the backend starts instead of bloating the
# app download.

# Reentrant: ensure_browser_installed() updates status while holding the lock.
_INSTALL_LOCK = threading.RLock()
_INSTALL_THREAD: Optional[threading.Thread] = None
_INSTALL = {
    "status": "idle",  # idle | installing | installed | failed | skipped
    "message": "",
    "log": "",
}
_CACHED_EXECUTABLE: Optional[Path] = None


def _home() -> Path:
    return Path(os.environ.get("ECHO_HOME", "~/.echo")).expanduser()


def _default_browsers_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _playwright_browsers_root(package_dir: Path) -> Path:
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override and override != "0":
        return Path(override).expanduser()
    if override == "0":
        return package_dir / "driver" / "package" / ".local-browsers"
    return _default_browsers_root()


def _playwright_package_dir() -> Path:
    import playwright  # type: ignore

    return Path(playwright.__file__).resolve().parent


def _prepare_browsers_env(package_dir: Path) -> None:
    """Keep Playwright's driver and our installer looking at the same browsers.

    Playwright's pipe transport sets ``PLAYWRIGHT_BROWSERS_PATH=0`` when frozen,
    which points at ``.local-browsers`` inside PyInstaller's temporary folder.
    That folder is deleted on exit and the CLI installer does not use it, so the
    driver would never find the Chromium we installed. Export the real cache
    path instead whenever the sidecar is frozen.
    """
    if not getattr(sys, "frozen", False):
        return
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override and override != "0":
        return
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_default_browsers_root())


def _chromium_executable(refresh: bool = False) -> tuple:
    """Return (path, error) without starting Playwright's node driver.

    Starting the driver just to read ``executable_path`` is slow and unreliable
    from a one-file sidecar. The browser cache layout and revision are available
    statically in Playwright's ``browsers.json``.
    """
    global _CACHED_EXECUTABLE
    if not refresh and _CACHED_EXECUTABLE is not None:
        return _CACHED_EXECUTABLE, ""
    try:
        package_dir = _playwright_package_dir()
        _prepare_browsers_env(package_dir)
        manifest = package_dir / "driver" / "package" / "browsers.json"
        revision = None
        if manifest.is_file():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for item in data.get("browsers", []):
                if item.get("name") == "chromium":
                    revision = str(item.get("revision") or "")
                    break
        if not revision:
            return None, "Cannot find the Chromium revision in Playwright's browsers.json"
        root = _playwright_browsers_root(package_dir)
        if sys.platform == "darwin":
            folder = "chrome-mac-arm64" if platform.machine() == "arm64" else "chrome-mac"
            relative = "%s/%s/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" % (
                "chromium-" + revision, folder
            )
        elif sys.platform == "win32":
            relative = "chromium-%s/chrome-win/chrome.exe" % revision
        else:
            relative = "chromium-%s/chrome-linux/chrome" % revision
        _CACHED_EXECUTABLE = root / relative
        return _CACHED_EXECUTABLE, ""
    except Exception as exc:
        return None, "Playwright is not installed (%s)" % exc


def _set_install(status: str, message: str = "", log_path: Path = None) -> None:
    with _INSTALL_LOCK:
        _INSTALL["status"] = status
        _INSTALL["message"] = message
        if log_path is not None:
            _INSTALL["log"] = str(log_path)


def _install_command() -> List[str]:
    if getattr(sys, "frozen", False):
        # The sidecar executable knows how to run the bundled Playwright CLI.
        return [sys.executable, "--install-browser"]
    return [sys.executable, "-m", "playwright", "install", "chromium"]


def _stream_command(command: List[str], record) -> int:
    record("Running: " + " ".join(command))
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    ) as process:
        for line in process.stdout or []:
            record(line)
        return process.wait()


def install_chromium_now() -> int:
    """Run the Playwright browser installer in this process (frozen sidecars)."""
    from playwright.__main__ import main  # type: ignore

    _prepare_browsers_env(_playwright_package_dir())
    original = sys.argv[:]
    try:
        sys.argv = ["playwright", "install", "chromium"]
        main()
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = original


def _install_worker(home: Path) -> None:
    log_path = home / "browser-install.log"
    lines: List[str] = []

    def record(line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        lines.append(line)
        if len(lines) > 60:
            lines.pop(0)
        _set_install("installing", line[-300:], log_path)

    try:
        home.mkdir(parents=True, exist_ok=True)
        path, error = _chromium_executable()
        if "Playwright is not installed" in error and not getattr(sys, "frozen", False):
            record("Installing the Playwright package…")
            code = _stream_command(
                [sys.executable, "-m", "pip", "install", "--user", "playwright"], record
            )
            if code != 0:
                record("Retrying without --user…")
                code = _stream_command(
                    [sys.executable, "-m", "pip", "install", "playwright"], record
                )
            if code != 0:
                raise RuntimeError("Could not install the Playwright package.")
        if "Playwright is not installed" not in error or not getattr(sys, "frozen", False):
            code = _stream_command(_install_command(), record)
            if code != 0:
                raise RuntimeError("Browser installer exited with %d" % code)
        path, error = _chromium_executable(refresh=True)
        if error or path is None or not path.exists():
            raise RuntimeError(error or "Playwright did not install a Chromium binary.")
        _set_install("installed", "Chromium ready: %s" % path, log_path)
    except Exception as exc:
        _set_install("failed", str(exc), log_path)
    finally:
        try:
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass


def ensure_browser_installed(home: Path = None, enabled: bool = True, wait: bool = False) -> bool:
    """Install Chromium in the background when it is missing.

    Returns True when a usable Chromium is already present. Installation never
    blocks the backend's HTTP server; poll ``browser_status()`` for progress.
    """
    global _INSTALL_THREAD
    home = home or _home()
    if not enabled:
        _set_install("skipped", "Automatic browser installation is disabled.")
        return False
    path, error = _chromium_executable()
    if path is not None and path.exists():
        _set_install("installed", "Chromium ready: %s" % path)
        return True

    with _INSTALL_LOCK:
        if _INSTALL["status"] == "installing" and _INSTALL_THREAD is not None:
            thread = _INSTALL_THREAD
        else:
            _set_install("installing", "Installing Chromium in the background…", home / "browser-install.log")
            _INSTALL_THREAD = threading.Thread(
                target=_install_worker, args=(home,), name="echo-browser-install", daemon=True
            )
            _INSTALL_THREAD.start()
            thread = _INSTALL_THREAD
    if wait:
        thread.join()
    return _INSTALL["status"] == "installed"


def browser_status() -> Dict[str, Any]:
    """Session state plus browser installation diagnostics for the native UI."""
    status = get_session().status()
    path, error = _chromium_executable()
    installed = path is not None and path.exists()
    with _INSTALL_LOCK:
        install = dict(_INSTALL)
    if installed and install["status"] != "installing":
        install["status"] = "installed"
        install["message"] = "Chromium ready: %s" % path
    elif not installed and install["status"] in {"idle", "installed"}:
        install["status"] = "not-installed"
        install["message"] = error or "Chromium has not been installed yet."
    status.update(
        {
            "installed": installed,
            "chromium_path": str(path) if path else "",
            "install": install,
        }
    )
    return status
