from __future__ import annotations

import os


LEGACY_DEFAULT_LOCAL_URL = "http://127.0.0.1:8766"


def default_local_url() -> str:
    """Return the URL for the active local Defaults HTTP server."""
    host = str(os.environ.get("DEFAULTS_HTTP_HOST") or "127.0.0.1").strip()
    port = str(os.environ.get("DEFAULTS_HTTP_PORT") or "8766").strip()
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def resolved_local_url(value: object) -> str:
    """Preserve an edited URL while migrating the legacy fixed-port default."""
    current = str(value or "").strip()
    if not current or current == LEGACY_DEFAULT_LOCAL_URL:
        return default_local_url()
    return current
