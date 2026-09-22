"""Single-request conservative provider health entrypoint."""

from __future__ import annotations

import json
import sys

from .service import ProviderRegistryService


def main() -> int:
    """Map generic health resource reads to verified registry evidence."""
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or not isinstance(
            request.get("payload"), dict
        ):
            raise ValueError("request is invalid")
        operation = str(request.get("operation") or "")
        if operation not in {
            "get",
            "list",
            "health",
            "rumi_provider_registry_pack.provider-registry-health.generate",
            "rumi_provider_registry_pack.provider-registry-health.stream",
        }:
            raise ValueError("provider health operation is invalid")
        value = ProviderRegistryService().invoke("health", request["payload"])
        response = {"status": "ok", "value": value}
        code = 0
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
