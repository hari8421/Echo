"""Headless backend entry point for the native Echo app.

The Electron shell starts this process on a random localhost port and talks to
it over Echo's existing JSON API. It uses the same workspace, tools and model
configuration as ``echo-ai ui``; nothing is duplicated here.
"""

from __future__ import annotations

import argparse

from echo_agent.browser import ensure_browser_installed, install_chromium_now
from echo_agent.config import Config
from echo_agent.ui import run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Echo desktop backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--install-browser",
        action="store_true",
        help="install Playwright's Chromium (used by the frozen sidecar)",
    )
    args = parser.parse_args(argv)

    if args.install_browser:
        return install_chromium_now()
    if not args.port:
        parser.error("--port is required unless --install-browser is used")

    cfg = Config.from_env()
    cfg.ui_host = args.host
    cfg.ui_port = args.port
    # Browser tools are optional, but the packaged app should be ready to use
    # them on first launch. Download Chromium in the background if it is missing.
    ensure_browser_installed(home=cfg.home, enabled=cfg.browser_auto_install)
    run(cfg, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
