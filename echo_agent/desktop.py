"""Optional desktop wrapper for Echo UI.

This keeps Echo local-first: it starts the existing localhost UI server and
opens it in a native window via pywebview when installed.
"""

from __future__ import annotations

import threading
import time
import urllib.request

from .config import Config
from .ui import serve


class DesktopError(Exception):
    pass


def _wait_ready(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status < 500:
                    return
        except Exception as exc:
            last = exc
            time.sleep(0.2)
    raise DesktopError("UI server did not become ready: %s" % last)


def run(cfg: Config, host: str = "127.0.0.1", port: int = 8765) -> None:
    try:
        import webview  # type: ignore
    except Exception as exc:
        raise DesktopError(
            "Desktop app requires pywebview. Install with: pip install -e .[desktop]"
        ) from exc

    httpd = serve(cfg, host=host, port=port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    shown = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = "http://%s:%d" % (shown, port)
    try:
        _wait_ready(url)
        webview.create_window("Echo", url, width=1280, height=860, min_size=(980, 700))
        webview.start()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
