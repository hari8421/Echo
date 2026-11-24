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
import re
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
        sync_playwright = _require_playwright()
        pw = sync_playwright().start()
        profile_dir = (profile_dir or "").strip()
        if profile_dir:
            path = Path(profile_dir).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(path),
                headless=bool(headless),
                viewport={"width": 1280, "height": 900},
            )
            browser = context.browser
            page = context.pages[0] if context.pages else context.new_page()
            self.state = BrowserState(pw, browser, context, page, bool(headless), str(path))
        else:
            browser = pw.chromium.launch(headless=bool(headless))
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            self.state = BrowserState(pw, browser, context, page, bool(headless), "")
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
        for obj, label in [(state.context, "context"), (state.browser, "browser"), (state.playwright, "playwright")]:
            try:
                if label == "playwright":
                    obj.stop()
                else:
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
