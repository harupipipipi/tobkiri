"""Generic opaque-frame asset server scoped to the resolved profile."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from blocks._common import error
from core_runtime.paths import resolve_pack_locations
from core_runtime.resolved_profile_scope import active_resolved_profile


def run(input_data: dict, context: dict) -> dict:
    """Serve an effective pack's built UI with restrictive containment headers."""
    del context
    data = input_data if isinstance(input_data, dict) else {}
    pack_id = str(data.get("pack_id") or "").strip()
    asset_path = str(data.get("asset_path") or "index.html").strip()
    plan = active_resolved_profile()
    if plan is None or pack_id not in plan.effective_pack_set:
        return error("isolated UI pack is not active", "PACK_NOT_ACTIVE")
    locations = resolve_pack_locations((pack_id,))
    if len(locations) != 1:
        return error("isolated UI pack is unavailable", "PACK_UNAVAILABLE")
    relative = Path(asset_path.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        return error("invalid isolated UI path", "INVALID_PATH")
    root = (locations[0].pack_subdir / "ui").resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return error("invalid isolated UI path", "INVALID_PATH")
    if not candidate.is_file():
        return error("isolated UI asset not found", "ASSET_NOT_FOUND")
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in {
        "application/javascript",
        "application/json",
    }:
        body: bytes | str = candidate.read_text(encoding="utf-8")
        content_type += "; charset=utf-8"
    else:
        body = candidate.read_bytes()
    return {
        "_binary": True,
        "status_code": 200,
        "content_type": content_type,
        "body": body,
        "headers": {
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self' http://127.0.0.1:* "
                "http://localhost:*; style-src 'self' 'unsafe-inline' "
                "http://127.0.0.1:* http://localhost:*; img-src 'self' data:; "
                "connect-src 'none'; frame-ancestors 'self'; "
                "base-uri 'none'; form-action 'none'"
            ),
            # Sandboxed frames have an opaque origin; assets remain loopback-only.
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    }
