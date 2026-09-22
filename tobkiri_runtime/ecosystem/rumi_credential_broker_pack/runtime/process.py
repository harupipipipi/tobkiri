"""Single-request credential broker process entrypoint."""

from __future__ import annotations

import json
import sys

from .service import CredentialBrokerService


def main() -> int:
    """Invoke without emitting credential material in failure diagnostics."""
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or not isinstance(
            request.get("payload"),
            dict,
        ):
            raise ValueError("request is invalid")
        operation = str(request.get("operation") or "")
        if operation == "resolve":
            raise PermissionError(
                "credential resolution requires the bound Host broker channel"
            )
        value = CredentialBrokerService().invoke(operation, request["payload"])
        response = {"status": "ok", "value": value}
        code = 0
    except PermissionError:
        response = {
            "status": "denied",
            "error_code": "denied",
            "diagnostics": ["credential request denied"],
        }
        code = 3
    except KeyError:
        response = {
            "status": "unavailable",
            "error_code": "not_configured",
            "diagnostics": ["credential is not configured"],
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
