"""Fail-closed, data-only ``rumi.pack.v3`` manifest loading."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .canonical import canonical_json, content_identity
from .models import ContractResult, ContractStatus
from .semver import parse_version, validate_version_range

SCHEMA_PATH = Path(__file__).parents[2] / "schemas" / "pack_manifest_v3.schema.json"
_MANIFEST_CONTRACT_ID = "rumi.resource.pack.manifest.v3"
_MANIFEST_VERSION = "3.0.0"
_MANIFEST_PROVIDER_ID = "core.manifest.loader"


class _DuplicateKeyError(ValueError):
    """Raised when JSON text repeats a key in the same object."""


@dataclass(frozen=True)
class ManifestDiagnostic:
    """Actionable manifest validation diagnostic."""

    path: str
    message: str


def load_manifest(path: Path) -> ContractResult[Mapping[str, Any]]:
    """Read and validate a manifest without importing or executing pack code."""
    try:
        manifest = _decode_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return _invalid_result(f"$: manifest read failed: {type(exc).__name__}")
    except UnicodeError as exc:
        return _invalid_result(f"$: manifest decoding failed: {type(exc).__name__}")
    except json.JSONDecodeError as exc:
        return _invalid_result(
            f"$: invalid JSON at line {exc.lineno}, column {exc.colno}"
        )
    except _DuplicateKeyError as exc:
        return _invalid_result(f"$: {exc}")
    except ValueError as exc:
        return _invalid_result(f"$: {exc}")

    try:
        schema = _decode_json(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        SchemaError,
        ValueError,
    ) as exc:
        return _invalid_result(
            f"$: manifest schema unavailable: {type(exc).__name__}"
        )

    errors = sorted(
        validator.iter_errors(manifest),
        key=lambda error: (
            tuple(str(item) for item in error.absolute_path),
            error.message,
        ),
    )
    diagnostics = tuple(
        ManifestDiagnostic(
            path=_format_path(error.absolute_path),
            message=error.message,
        )
        for error in errors
    )
    if not diagnostics:
        diagnostics = _semantic_diagnostics(manifest)
    if diagnostics:
        return _invalid_result(
            *(f"{item.path}: {item.message}" for item in diagnostics)
        )

    normalized = dict(manifest)
    normalized["content_identity"] = content_identity(manifest)
    return ContractResult(
        status=ContractStatus.OK,
        contract_id=_MANIFEST_CONTRACT_ID,
        version=_MANIFEST_VERSION,
        provider_instance_id=_MANIFEST_PROVIDER_ID,
        value=normalized,
    )


def _decode_json(text: str) -> Any:
    """Decode strict JSON while rejecting duplicate keys and non-finite values."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is forbidden: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKeyError(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    return json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def _format_path(parts: Iterable[object]) -> str:
    """Render a deterministic JSON path for a validation diagnostic."""
    return "$" + "".join(f"[{item!r}]" for item in parts)


def _invalid_result(*diagnostics: str) -> ContractResult[Mapping[str, Any]]:
    """Return a typed invalid-manifest result."""
    return ContractResult(
        status=ContractStatus.INVALID_MANIFEST,
        contract_id=_MANIFEST_CONTRACT_ID,
        version=_MANIFEST_VERSION,
        provider_instance_id=_MANIFEST_PROVIDER_ID,
        diagnostics=tuple(diagnostics),
    )


def _semantic_diagnostics(manifest: Any) -> tuple[ManifestDiagnostic, ...]:
    """Validate deterministic cross-field invariants after schema success."""
    if not isinstance(manifest, Mapping):
        return (ManifestDiagnostic("$", "manifest must be an object"),)

    diagnostics: list[ManifestDiagnostic] = []
    provided = manifest["contracts"]["provides"]
    required = manifest["contracts"]["requires"]
    for index, contract in enumerate(provided):
        try:
            parse_version(contract["version"])
        except ValueError as exc:
            diagnostics.append(
                ManifestDiagnostic(
                    path=(
                        f"$['contracts']['provides'][{index}]['version']"
                    ),
                    message=str(exc),
                )
            )
    for index, contract in enumerate(required):
        try:
            validate_version_range(contract["version_range"])
        except ValueError as exc:
            diagnostics.append(
                ManifestDiagnostic(
                    path=(
                        f"$['contracts']['requires'][{index}]"
                        "['version_range']"
                    ),
                    message=str(exc),
                )
            )
    provider_ids = [item["provider_instance_id"] for item in provided]
    _append_duplicate_diagnostics(
        diagnostics,
        provider_ids,
        "$['contracts']['provides']",
        "provider instance ID",
    )
    _append_duplicate_diagnostics(
        diagnostics,
        [
            (
                item["id"],
                item["version_range"],
                item["cardinality"],
                item["optional"],
                item.get("instance_key"),
            )
            for item in required
        ],
        "$['contracts']['requires']",
        "contract requirement",
    )
    _append_duplicate_diagnostics(
        diagnostics,
        [item["id"] for item in manifest["entrypoints"]],
        "$['entrypoints']",
        "entrypoint ID",
    )
    _append_duplicate_diagnostics(
        diagnostics,
        [item["id"] for item in manifest["resources"]],
        "$['resources']",
        "resource ID",
    )
    _append_duplicate_diagnostics(
        diagnostics,
        [item["namespace"] for item in manifest["storage"]],
        "$['storage']",
        "storage namespace",
    )
    _append_duplicate_diagnostics(
        diagnostics,
        [
            (item["capability"], item["access"])
            for item in manifest["permissions"]
        ],
        "$['permissions']",
        "permission",
    )
    aliases = manifest["migration"]["compatibility_aliases"]
    _append_duplicate_diagnostics(
        diagnostics,
        [item["legacy_id"] for item in aliases],
        "$['migration']['compatibility_aliases']",
        "legacy compatibility ID",
    )

    cardinalities: dict[str, str] = {}
    for index, contract in enumerate(provided):
        identifier_match = re.search(r"\.v([1-9][0-9]*)$", contract["id"])
        version_major = int(contract["version"].split(".", maxsplit=1)[0])
        if identifier_match and int(identifier_match.group(1)) != version_major:
            diagnostics.append(
                ManifestDiagnostic(
                    path=f"$['contracts']['provides'][{index}]",
                    message=(
                        "contract ID major does not match provided version major"
                    ),
                )
            )
        prior = cardinalities.setdefault(contract["id"], contract["cardinality"])
        if prior != contract["cardinality"]:
            diagnostics.append(
                ManifestDiagnostic(
                    path=f"$['contracts']['provides'][{index}]['cardinality']",
                    message="providers for one contract must share cardinality",
                )
            )

    provided_set = {item["id"] for item in provided}
    for index, entrypoint in enumerate(manifest["entrypoints"]):
        if entrypoint["contract_id"] not in provided_set:
            diagnostics.append(
                ManifestDiagnostic(
                    path=f"$['entrypoints'][{index}]['contract_id']",
                    message=(
                        "entrypoint references a contract not provided by this pack"
                    ),
                )
            )

    diagnostics.sort(key=lambda item: (item.path, item.message))
    return tuple(diagnostics)


def _append_duplicate_diagnostics(
    diagnostics: list[ManifestDiagnostic],
    values: list[Any],
    path: str,
    label: str,
) -> None:
    """Append stable diagnostics for repeated semantic identities."""
    encoded = [canonical_json(value) for value in values]
    duplicates = sorted({value for value in encoded if encoded.count(value) > 1})
    for duplicate in duplicates:
        diagnostics.append(
            ManifestDiagnostic(
                path=path,
                message=f"duplicate {label}: {duplicate.decode('utf-8')}",
            )
        )
