"""Minimal PackVM operation implementation for the Example Echo Pack.

This file is the only code artifact the Pack carries.  The Host materializes
it into an isolated guest interpreter and calls ``tobkiri_packvm_invoke``
for each declared Contract operation.  Only the Python standard library is
importable inside the guest; declare real dependencies as Pack requirements
in ``pack-source.v1.json`` instead.
"""

from __future__ import annotations

from typing import Any, Mapping

OPERATION_ID = "example.echo"


def tobkiri_packvm_invoke(
    operation_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the submitted text verbatim for the echo operation."""
    if operation_id != OPERATION_ID:
        return {
            "error": {
                "code": "example.echo.unknown_operation",
                "message": f"unsupported operation: {operation_id}",
            }
        }
    return {"text": payload.get("text", "")}


if __name__ == "__main__":
    raise SystemExit("PackVM implementations are invoked by the Host")
