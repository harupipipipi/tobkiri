"""Single-request provider registry process entrypoint."""

from __future__ import annotations

import json
import sys

from .service import ProviderRegistryService


def main() -> int:
    """Return a redacted, path-free result envelope."""
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or not isinstance(
            request.get("payload"), dict
        ):
            raise ValueError("request is invalid")
        value = ProviderRegistryService().invoke(
            str(request.get("operation") or ""), request["payload"]
        )
        response = {"status": "ok", "value": value}
        code = 0
    except PermissionError:
        response = {
            "status": "denied",
            "error_code": "denied",
            "diagnostics": ["provider registry request denied"],
        }
        code = 3
    except KeyError:
        response = {
            "status": "unavailable",
            "error_code": "unknown",
            "diagnostics": ["provider registry item is unknown"],
        }
        code = 2
    except Exception as exc:
        response = {
            "status": "unavailable",
            "error_code": type(exc).__name__,
            "diagnostics": [type(exc).__name__],
        }
        code = 2
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

