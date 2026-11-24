from __future__ import annotations

import os
from typing import Literal

SurfaceResult = Literal["disabled", "browser", "webview", "webview_unavailable"]


def open_desktop_surface(url: str, title: str = "Tobkiri") -> SurfaceResult:
    """Open the defaultspack shell without coupling the pack to one UI runtime."""
    if os.environ.get("RUMI_DEFAULTSPACK_OPEN_BROWSER", "1") == "0":
        return "disabled"

    surface = os.environ.get("RUMI_DEFAULTSPACK_SURFACE", "webview").strip().lower()
    if surface == "browser":
        browser_debug_allowed = os.environ.get("RUMI_DEFAULTSPACK_ALLOW_BROWSER_DEBUG") == "1"
        if not browser_debug_allowed:
            return "disabled"

        import webbrowser

        webbrowser.open(url)
        return "browser"

    if surface == "webview":
        try:
            import webview  # type: ignore[import-not-found]
        except Exception:
            return "webview_unavailable"

        window = webview.create_window(title, url)
        webview.start()
        return "webview" if window is not None else "webview_unavailable"

    return "disabled"
