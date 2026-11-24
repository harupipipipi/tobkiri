"""Observe real schema-selfcheck errors before replacing one private kernel.

CI-only reference DATA, never production cache contents or validation results.
The original exception objects supply paths, contexts, causes and identity facts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

from bundle_catalog_oracle import ORACLE_COMMIT, SOURCE_HASHES
from schema_kernel_observation import encoded, meta_nodes, node_digest, observe

MAX_BYTES = 4 * 1024 * 1024
ORIGINAL_SCHEMA_COUNT = 42
ORIGINAL_SCHEMA_INVENTORY_SHA256 = (
    "2311b39b1f946c1ba45a9d87d57fde15c28a6a79afef1a44a5b98f3603b63008"
)


def run(source: Path, output: Path) -> None:
    """Capture the pinned original's real schema-selfcheck behavior in CI."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("schema kernel oracle is restricted to Linux GitHub Actions")
    if (
        not source.is_absolute()
        or source.resolve(strict=True) != source
        or not output.is_absolute()
        or output.suffix != ".json"
        or output.parent.resolve(strict=True) != output.parent
        or os.path.lexists(output)
        or source in output.parents
    ):
        raise ValueError("physical source and new external JSON output required")
    for name, expected in SOURCE_HASHES.items():
        path = source / "tobkiri_protocol" / name
        if (
            path.resolve(strict=True) != path
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("original protocol source differs: " + name)
    if importlib.metadata.version("jsonschema") != "4.26.0":
        raise ValueError("original locked validator version differs")
    if any(
        name == "tobkiri_protocol" or name.startswith("tobkiri_protocol.")
        for name in sys.modules
    ):
        raise ValueError("original protocol was imported before source admission")
    sys.path.insert(0, str(source))
    from tobkiri_protocol.canonical import strict_loads

    if (
        Path(sys.modules["tobkiri_protocol.canonical"].__file__).resolve(strict=True)
        != source / "tobkiri_protocol/canonical.py"
    ):
        raise ValueError("original strict parser escaped the pinned source")
    from jsonschema import Draft202012Validator, exceptions
    from jsonschema_specifications import REGISTRY

    known_nodes = meta_nodes()

    cases = []
    originals = sorted((source / "tobkiri_protocol/schemas").glob("*.schema.json"))
    if len(originals) != ORIGINAL_SCHEMA_COUNT:
        raise ValueError("complete original protocol schema inventory changed")
    inputs = []
    inventory = []
    for path in originals:
        if path.resolve(strict=True) != path or not path.is_file():
            raise ValueError("original schema is not a physical source file")
        raw = path.read_bytes()
        inventory.append(
            {
                "name": path.name,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        inputs.append(
            (
                "original/" + path.name,
                strict_loads(raw),
                hashlib.sha256(raw).hexdigest(),
            )
        )
    if node_digest(inventory) != ORIGINAL_SCHEMA_INVENTORY_SHA256:
        raise ValueError("original complete schema byte inventory differs")
    curated = [
        ("boolean-true", True),
        ("boolean-false", False),
        ("empty-object", {}),
        ("null-schema", None),
        ("unknown-type", {"type": "not-a-type"}),
        ("required-string", {"required": "field"}),
        ("properties-array", {"properties": []}),
        ("bool-number", {"minimum": True}),
        ("string-number", {"minimum": "0"}),
        ("bad-regex", {"pattern": "["}),
        ("array-items", {"items": []}),
        ("bad-defs-node", {"$defs": {"bad": 0}}),
        ("zero-multiple", {"multipleOf": 0}),
        ("negative-length", {"minLength": -1}),
        ("fractional-count", {"minItems": 1.5}),
        ("integral-float-count", {"minItems": 1.0}),
        ("large-exact-count", {"minItems": 9007199254740993}),
        ("first-violation-order-a", {"required": 1, "type": "bad"}),
        ("first-violation-order-b", {"type": "bad", "required": 1}),
        ("local-reference", {"$defs": {"x": {"type": "string"}}, "$ref": "#/$defs/x"}),
        (
            "escaped-reference",
            {"$defs": {"a~/b": {"type": "integer"}}, "$ref": "#/$defs/a~0~1b"},
        ),
        ("unknown-format", {"format": "unrecognized-source-format"}),
    ]
    inputs.extend(("boundary/" + name, value, None) for name, value in curated)
    for name, schema, source_hash in inputs:
        try:
            Draft202012Validator.check_schema(schema)
        except exceptions.SchemaError as error:
            result = {"valid": False, "error": observe(error, schema, known_nodes)}
        else:
            result = {"valid": True, "error": None}
        cases.append(
            {
                "name": name,
                "schema": schema,
                "source_sha256": source_hash,
                "expected": result,
            }
        )
    result = {
        "format": "tobkiri.schema-kernel-reference/1",
        "oracle_commit": ORACLE_COMMIT,
        "jsonschema_version": "4.26.0",
        "original_schema_count": ORIGINAL_SCHEMA_COUNT,
        "original_schema_inventory_sha256": ORIGINAL_SCHEMA_INVENTORY_SHA256,
        "meta_schema": Draft202012Validator.META_SCHEMA,
        "meta_registry": [
            {"uri": uri, "schema": REGISTRY.contents(uri)} for uri in sorted(REGISTRY)
        ],
        "meta_format_checker_names": sorted(
            Draft202012Validator.FORMAT_CHECKER.checkers
        ),
        "cases": cases,
        "scope": "original schema-selfcheck reference only; no replacement kernel or Wasm budget proof",
        "production_enabled": False,
    }
    raw = encoded(result) + b"\n"
    if len(raw) > MAX_BYTES:
        raise ValueError("original schema reference exceeds four MiB")
    with output.open("xb") as stream:
        stream.write(raw)
    print(
        json.dumps(
            {
                "schema_kernel_reference_cases": len(cases),
                "original_schemas": ORIGINAL_SCHEMA_COUNT,
                "bytes": len(raw),
                "production_enabled": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    run(**vars(parser.parse_args()))
