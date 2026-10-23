"""Freeze explicitly selected legacy handler fixtures before runtime deletion."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any, Callable


def record(handler: Callable[[Any], Any], inputs: list[Any], *, source: bytes,
           handler_name: str) -> dict[str, Any]:
    """Run reviewed inputs; failures are explicit fixtures rather than omissions."""
    cases = []
    for value in inputs:
        try:
            output = handler(value)
            json.dumps(output, allow_nan=False)
            result = {"ok": output}
        except Exception as error:
            # Exception text can contain credentials or user paths; class only.
            result = {"error_class": type(error).__name__}
        cases.append({"input": value, "expect": result})
    return {"format": "tobkiri.parity-fixtures/1", "handler": handler_name,
            "source_sha256": hashlib.sha256(source).hexdigest(), "cases": cases}


def main() -> None:
    """Record a caller-selected importable handler without discovering providers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handler", required=True, help="module:function")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.read_bytes()
    if hashlib.sha256(source).hexdigest() != args.source_sha256:
        raise SystemExit("legacy source differs from the reviewed recording pin")
    module, function = args.handler.split(":", 1)
    handler = getattr(importlib.import_module(module), function)
    evidence = record(handler, json.loads(args.inputs.read_text()), source=source,
                      handler_name=args.handler)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, allow_nan=False,
                                     sort_keys=True, indent=2) + "\n")
    print(json.dumps({"cases": len(evidence["cases"]), "source_sha256": evidence["source_sha256"]}))


if __name__ == "__main__":
    main()
