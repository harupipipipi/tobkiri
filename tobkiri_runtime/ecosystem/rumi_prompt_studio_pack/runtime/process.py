"""Single-request stdio process entrypoint for Prompt Studio contracts."""

from __future__ import annotations

import json
import sys

from .service import PromptStudioService


def main() -> int:
    """Read one request, invoke locally, and emit a non-lossy JSON envelope."""
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        operation = str(request.get("operation") or "")
        payload = request.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        value = PromptStudioService().invoke(operation, payload)
        response = {"status": "ok", "value": value}
        code = 0
    except PermissionError as exc:
        response = {
            "status": "denied",
            "error_code": "denied",
            "diagnostics": [_safe_message(exc)],
        }
        code = 3
    except Exception as exc:
        response = {
            "status": "unavailable",
            "error_code": type(exc).__name__,
            "diagnostics": [_safe_message(exc)],
        }
        code = 2
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return code


def _safe_message(exc: Exception) -> str:
    """Return bounded diagnostics without exposing filesystem locations."""
    allowed = {
        "KeyError",
        "PromptWriteConflict",
        "RuntimeError",
        "ValueError",
    }
    if type(exc).__name__ not in allowed:
        return type(exc).__name__
    message = " ".join(str(exc).split())[:500]
    return f"{type(exc).__name__}: {message}"


if __name__ == "__main__":
    raise SystemExit(main())
