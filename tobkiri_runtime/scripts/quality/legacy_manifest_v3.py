"""Offline-only, data-only ``rumi.pack.v3`` manifest loading."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # Desktop runtime keeps third-party bootstrap minimal.
    Draft202012Validator = None  # type: ignore[assignment,misc]
    FormatChecker = None  # type: ignore[assignment,misc]

from core_runtime.global_contracts.canonical import content_identity
from core_runtime.global_contracts.models import ContractResult, ContractStatus

SCHEMA_PATH = Path(__file__).parents[2] / "schemas" / "pack_manifest_v3.schema.json"


@dataclass(frozen=True)
class ManifestDiagnostic:
    """Actionable manifest validation diagnostic."""

    path: str
    message: str


def load_manifest(path: Path) -> ContractResult[Mapping[str, Any]]:
    """Read and validate a manifest without importing or executing pack code."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return ContractResult(
            ContractStatus.INVALID_MANIFEST,
            diagnostics=(f"$: {exc}",),
        )
    if Draft202012Validator is None or FormatChecker is None:
        return ContractResult(
            ContractStatus.INVALID_MANIFEST,
            diagnostics=("$: JSON Schema validation is unavailable",),
        )
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(manifest),
        key=lambda error: list(error.absolute_path),
    )
    diagnostics = tuple(
        ManifestDiagnostic(
            path="$" + "".join(f"[{item!r}]" for item in error.absolute_path),
            message=error.message,
        )
        for error in errors
    )
    semantic_diagnostics = _semantic_diagnostics(manifest) if not diagnostics else ()
    diagnostics += semantic_diagnostics
    if diagnostics:
        return ContractResult(
            ContractStatus.INVALID_MANIFEST,
            diagnostics=tuple(
                f"{diagnostic.path}: {diagnostic.message}"
                for diagnostic in diagnostics
            ),
        )
    normalized = dict(manifest)
    normalized["content_identity"] = content_identity(manifest)
    return ContractResult(ContractStatus.OK, value=normalized)


def _semantic_diagnostics(
    manifest: Mapping[str, Any],
) -> tuple[ManifestDiagnostic, ...]:
    """Validate cross-field invariants not expressible in portable JSON Schema."""
    diagnostics: list[ManifestDiagnostic] = []
    provided = manifest["contracts"]["provides"]
    contract_ids = [item["id"] for item in provided]
    provider_ids = [item["provider_instance_id"] for item in provided]
    _append_duplicate_diagnostics(diagnostics, contract_ids, "contract ID")
    _append_duplicate_diagnostics(diagnostics, provider_ids, "provider instance ID")

    for index, contract in enumerate(provided):
        identifier_match = re.search(r"\.v([1-9][0-9]*)$", contract["id"])
        version_major = int(contract["version"].split(".", maxsplit=1)[0])
        if identifier_match and int(identifier_match.group(1)) != version_major:
            diagnostics.append(
                ManifestDiagnostic(
                    path=f"$['contracts']['provides'][{index}]",
                    message="contract ID major does not match provided version major",
                )
            )

    entrypoint_contract_ids = [
        entrypoint["contract_id"] for entrypoint in manifest["entrypoints"]
    ]
    entrypoint_counts = Counter(entrypoint_contract_ids)
    for contract_id in sorted(
        value for value, count in entrypoint_counts.items() if count > 1
    ):
        diagnostics.append(
            ManifestDiagnostic(
                path="$['entrypoints']",
                message=f"duplicate entrypoint contract ID: {contract_id}",
            )
        )

    provided_set = set(contract_ids)
    entrypoint_set = set(entrypoint_contract_ids)
    for index, entrypoint in enumerate(manifest["entrypoints"]):
        if entrypoint["contract_id"] not in provided_set:
            diagnostics.append(
                ManifestDiagnostic(
                    path=f"$['entrypoints'][{index}]['contract_id']",
                    message="entrypoint references a contract not provided by this pack",
                )
            )
    for contract_id in sorted(provided_set - entrypoint_set):
        diagnostics.append(
            ManifestDiagnostic(
                path="$['entrypoints']",
                message=f"missing entrypoint for provided contract: {contract_id}",
            )
        )
    return tuple(diagnostics)


def _append_duplicate_diagnostics(
    diagnostics: list[ManifestDiagnostic],
    values: list[str],
    label: str,
) -> None:
    """Append deterministic diagnostics for repeated semantic identities."""
    duplicates = sorted(value for value in set(values) if values.count(value) > 1)
    for duplicate in duplicates:
        diagnostics.append(
            ManifestDiagnostic(
                path="$['contracts']['provides']",
                message=f"duplicate {label}: {duplicate}",
            )
        )
