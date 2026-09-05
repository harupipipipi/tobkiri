"""Fail-closed JSON Schema and semantic validation for v4 documents."""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical import canonical_digest, canonical_json, strict_loads
from .errors import ProtocolError, SchemaValidationError
from .ids import (
    validate_artifact_digest,
    validate_canonical_id,
    validate_contract_id,
    validate_opaque_reference,
)

try:
    from jsonschema import Draft202012Validator, FormatChecker, RefResolver
except ImportError:  # pragma: no cover - dependency is declared by the project.
    Draft202012Validator = None  # type: ignore[assignment,misc]
    FormatChecker = None  # type: ignore[assignment,misc]
    RefResolver = None  # type: ignore[assignment,misc]


SCHEMA_DIR = Path(__file__).with_name("schemas")
SCHEMA_ALIASES = {
    "pack": "pack_manifest_v4.schema.json",
    "pack_manifest": "pack_manifest_v4.schema.json",
    "contract": "contract_v4.schema.json",
    "request": "request_frame_v1.schema.json",
    "request_frame": "request_frame_v1.schema.json",
    "request_envelope": "request_envelope_v1.schema.json",
    "profile": "profile_v5.schema.json",
    "profile_lock": "profile_lock_v5.schema.json",
    "profile_artifact_lock": "profile_artifact_lock_v1.schema.json",
    "profile_intent": "profile_intent_v1.schema.json",
    "profile_release_provenance": "profile_release_provenance_v1.schema.json",
    "composition_catalog": "composition_catalog_v4.schema.json",
    "resolved_plan": "resolved_plan_v2.schema.json",
    "base": "base_definition_v4.schema.json",
    "shell": "shell_definition_v5.schema.json",
    "cli_io": "cli_io_v1.schema.json",
    "function_principal": "function_principal_v1.schema.json",
    "provenance": "provenance_v1.schema.json",
    "activation": "activation_record_v2.schema.json",
    "distribution": "distribution_v1.schema.json",
    "inventory": "inventory_v1.schema.json",
    "pack_artifact_index": "pack_artifact_index_v4.schema.json",
    "pack_contract_catalog": "pack_contract_catalog_v4.schema.json",
    "executable_catalog": "executable_catalog_v4.schema.json",
    "external_pack_catalog": "external_normal_pack_catalog_v4.schema.json",
    "defaults_setup": "defaults_setup_v4.schema.json",
}

_ID_FIELDS = {
    "id",
    "pack_id",
    "profile_id",
    "function_id",
    "operation_id",
    "provider_id",
    "provider_instance_id",
    "distribution_id",
    "caller_function_id",
    "target_provider_id",
}
_REFERENCE_FIELDS = {
    "authority_reference",
    "lease_reference",
    "resource_reference",
}
_FORBIDDEN_AUTHORITY_KEYS = {
    "approved",
    "approval",
    "approvals",
    "grant",
    "grants",
    "invocation_lease",
    "provider_authority",
    "provider_authority_record",
    "secret_material",
    "raw_secret",
    "host_execution",
    "rumi_allow_host_execution",
}


def schema_path(schema_name: str) -> Path:
    """Resolve a schema alias or filename inside the protocol package."""
    filename = SCHEMA_ALIASES.get(schema_name, schema_name)
    path = SCHEMA_DIR / filename
    if path.suffix != ".json" or not path.is_file():
        raise SchemaValidationError(f"unknown protocol schema: {schema_name}")
    return path


@lru_cache(maxsize=None)
def load_schema(schema_name: str) -> dict[str, Any]:
    """Load and syntax-check one protocol schema."""
    path = schema_path(schema_name)
    try:
        payload = strict_loads(path.read_bytes())
    except (OSError, ProtocolError) as exc:
        raise SchemaValidationError(f"cannot load schema {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SchemaValidationError(f"schema must be an object: {path}")
    if Draft202012Validator is None:
        raise SchemaValidationError("jsonschema is unavailable; refusing to validate")
    try:
        Draft202012Validator.check_schema(payload)
    except Exception as exc:  # jsonschema exposes several schema error classes.
        raise SchemaValidationError(f"invalid protocol schema {path}: {exc}") from exc
    return payload


@lru_cache(maxsize=1)
def _schema_store() -> dict[str, Any]:
    """Return absolute-ID schemas used by local JSON Schema references."""
    store: dict[str, Any] = {}
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        try:
            schema = load_schema(path.name)
        except SchemaValidationError:
            continue
        identifier = schema.get("$id")
        if isinstance(identifier, str):
            store[identifier] = schema
    return store


def validate_document(
    document: Mapping[str, Any] | str | bytes,
    schema_name: str,
    *,
    reject_authority_fields: bool = True,
) -> dict[str, Any]:
    """Validate and return a copy of a serialized v4 document.

    The function performs strict parsing, JSON Schema validation, canonical ID
    checks, duplicate semantic identity checks, and security invariants.  Any
    uncertainty raises ``SchemaValidationError``; there is no warning-to-allow
    path.
    """
    if isinstance(document, (str, bytes)):
        try:
            parsed = strict_loads(document)
        except ProtocolError as exc:
            raise SchemaValidationError(str(exc), diagnostics=(str(exc),)) from exc
    else:
        parsed = copy.deepcopy(dict(document)) if isinstance(document, Mapping) else document
        try:
            canonical_json(parsed)
        except ProtocolError as exc:
            raise SchemaValidationError(str(exc), diagnostics=(str(exc),)) from exc
    if not isinstance(parsed, dict):
        raise SchemaValidationError("serialized protocol document must be an object")

    schema_name = _versioned_schema_name(parsed, schema_name)
    schema = load_schema(schema_name)
    if Draft202012Validator is None or FormatChecker is None or RefResolver is None:
        raise SchemaValidationError("jsonschema is unavailable; refusing to validate")
    resolver = RefResolver.from_schema(schema, store=_schema_store())
    validator = Draft202012Validator(schema, format_checker=FormatChecker(), resolver=resolver)
    errors = sorted(validator.iter_errors(parsed), key=_error_sort_key)
    diagnostics = [f"{_error_path(error.absolute_path)}: {error.message}" for error in errors]
    diagnostics.extend(_semantic_diagnostics(parsed, schema_name, reject_authority_fields))
    if diagnostics:
        raise SchemaValidationError(
            f"{schema_name} validation failed", diagnostics=tuple(diagnostics)
        )
    return parsed


def _versioned_schema_name(document: Mapping[str, Any], schema_name: str) -> str:
    """Select record schemas by their serialized API version."""

    versions = {
        "profile": {
            "io.tobkiri.profile.v4": "profile_v4.schema.json",
            "io.tobkiri.profile.v5": "profile_v5.schema.json",
        },
        "profile_lock": {
            "io.tobkiri.profile-lock.v4": "profile_lock_v4.schema.json",
            "io.tobkiri.profile-lock.v5": "profile_lock_v5.schema.json",
        },
        "resolved_plan": {
            "io.tobkiri.resolved-plan.v1": "resolved_plan_v1.schema.json",
            "io.tobkiri.resolved-plan.v2": "resolved_plan_v2.schema.json",
        },
        "activation": {
            "io.tobkiri.activation-record.v1": "activation_record_v1.schema.json",
            "io.tobkiri.activation-record.v2": "activation_record_v2.schema.json",
        },
        "shell": {
            "io.tobkiri.shell.v4": "shell_definition_v4.schema.json",
            "io.tobkiri.shell.v5": "shell_definition_v5.schema.json",
        },
    }
    version_fields = {
        "profile": "profile_api_version",
        "profile_lock": "lock_api_version",
        "resolved_plan": "plan_api_version",
        "activation": "activation_api_version",
        "shell": "shell_api_version",
    }
    choices = versions.get(schema_name)
    if choices is None:
        return schema_name
    document_version = document.get(version_fields[schema_name])
    selected = (
        choices.get(document_version)
        if isinstance(document_version, str)
        else None
    )
    if selected is None:
        raise SchemaValidationError(f"unsupported {schema_name} API version")
    return selected


def validate_file(path: Path, schema_name: str) -> dict[str, Any]:
    """Read and validate a JSON document without executing its contents."""
    try:
        return validate_document(path.read_bytes(), schema_name)
    except OSError as exc:
        raise SchemaValidationError(f"cannot read {path}: {exc}") from exc


def _semantic_diagnostics(
    document: Mapping[str, Any],
    schema_name: str,
    reject_authority_fields: bool,
) -> list[str]:
    diagnostics: list[str] = []
    inventory_document = schema_name in {"inventory", "inventory_v1.schema.json"}
    for path, key, value in _walk(document):
        if key in _ID_FIELDS and isinstance(value, str) and not inventory_document:
            try:
                validate_canonical_id(value, field=key)
            except ProtocolError as exc:
                diagnostics.append(f"{path}: {exc}")
        elif key == "contract_id" and isinstance(value, str):
            try:
                validate_contract_id(value, field=key)
            except ProtocolError as exc:
                diagnostics.append(f"{path}: {exc}")
        elif key.endswith("_digest") and key != "tree_digest" and isinstance(value, str):
            try:
                validate_artifact_digest(value, field=key)
            except ProtocolError as exc:
                diagnostics.append(f"{path}: {exc}")
        elif key in _REFERENCE_FIELDS and isinstance(value, str):
            try:
                validate_opaque_reference(value, field=key)
            except ProtocolError as exc:
                diagnostics.append(f"{path}: {exc}")
        if (
            reject_authority_fields
            and schema_name
            in {
                "profile",
                "profile_v4.schema.json",
                "profile_v5.schema.json",
                "pack",
                "pack_manifest",
                "pack_manifest_v4.schema.json",
                "request",
                "request_frame",
                "request_frame_v1.schema.json",
            }
            and key in _FORBIDDEN_AUTHORITY_KEYS
        ):
            diagnostics.append(f"{path}: forbidden authority-bearing field: {key}")

    if not inventory_document:
        diagnostics.extend(_duplicate_identity_diagnostics(document))
    if schema_name in {"profile", "profile_v4.schema.json", "profile_v5.schema.json"}:
        diagnostics.extend(_profile_security_diagnostics(document))
    if schema_name in {
        "shell",
        "shell_definition_v4.schema.json",
        "shell_definition_v5.schema.json",
    }:
        diagnostics.extend(_shell_security_diagnostics(document))
    if schema_name in {"function_principal", "function_principal_v1.schema.json"}:
        diagnostics.extend(_principal_digest_diagnostics(document))
    if schema_name in {"distribution", "distribution_v1.schema.json"}:
        diagnostics.extend(_distribution_integrity_diagnostics(document))
    return diagnostics


def _distribution_integrity_diagnostics(document: Mapping[str, Any]) -> list[str]:
    payload = {
        key: value
        for key, value in document.items()
        if key not in {"integrity", "signature_envelope"}
    }
    expected = canonical_digest(payload)
    integrity = document.get("integrity")
    envelope = document.get("signature_envelope")
    if not isinstance(integrity, Mapping) or not isinstance(envelope, Mapping):
        return []
    diagnostics: list[str] = []
    if integrity.get("manifest_digest") != expected:
        diagnostics.append("$.integrity.manifest_digest: canonical digest does not match")
    if envelope.get("signed_digest") != expected:
        diagnostics.append("$.signature_envelope.signed_digest: signed digest does not match")
    return diagnostics


def _duplicate_identity_diagnostics(document: Mapping[str, Any]) -> list[str]:
    diagnostics: list[str] = []
    identity_keys = _ID_FIELDS | {"contract_id"}
    for path, value in _walk_containers(document):
        if not isinstance(value, list):
            continue
        if path == "$.requested_edges" or path.endswith(".bindings"):
            seen_edges: dict[tuple[str, ...], int] = {}
            for index, item in enumerate(value):
                if not isinstance(item, Mapping):
                    continue
                principal = item.get("function_principal")
                target_provider_id = item.get("target_provider_id")
                if isinstance(principal, Mapping):
                    target_provider_id = principal.get("function_id")
                binding_identity = (
                    str(item.get("caller_function_id") or ""),
                    str(target_provider_id or ""),
                    str(item.get("contract_id") or ""),
                    str(item.get("operation_id") or ""),
                )
                previous = seen_edges.get(binding_identity)
                if previous is not None:
                    diagnostics.append(
                        f"{path}[{index}]: duplicate operation binding; first at index {previous}"
                    )
                else:
                    seen_edges[binding_identity] = index
            continue
        if path == "$.variant_pins":
            seen_variants: dict[tuple[str, str], int] = {}
            for index, item in enumerate(value):
                if not isinstance(item, Mapping):
                    continue
                variant_identity = (
                    str(item.get("pack_id") or ""),
                    str(item.get("variant_id") or ""),
                )
                previous = seen_variants.get(variant_identity)
                if previous is not None:
                    diagnostics.append(
                        f"{path}[{index}]: duplicate executable variant pin; "
                        f"first at index {previous}"
                    )
                else:
                    seen_variants[variant_identity] = index
            continue
        if path == "$.operation_catalog" or path.endswith(".operations"):
            seen_operations: dict[str, int] = {}
            for index, item in enumerate(value):
                if not isinstance(item, Mapping):
                    continue
                operation_id = item.get("operation_id")
                if not isinstance(operation_id, str):
                    continue
                previous = seen_operations.get(operation_id)
                if previous is not None:
                    diagnostics.append(
                        f"{path}[{index}].operation_id: duplicate identity; "
                        f"first at index {previous}"
                    )
                else:
                    seen_operations[operation_id] = index
            continue
        seen: dict[tuple[str, str], int] = {}
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                continue
            for key in identity_keys:
                candidate = item.get(key)
                if not isinstance(candidate, str):
                    continue
                field_identity = (key, candidate)
                previous = seen.get(field_identity)
                if previous is not None:
                    diagnostics.append(
                        f"{path}[{index}].{key}: duplicate identity; first at index {previous}"
                    )
                else:
                    seen[field_identity] = index
    return diagnostics


def _profile_security_diagnostics(document: Mapping[str, Any]) -> list[str]:
    diagnostics: list[str] = []
    state = document.get("state")
    if state == "resolved":
        base = document.get("base")
        if not isinstance(base, Mapping) or not isinstance(base.get("artifact_digest"), str):
            diagnostics.append("$.base.artifact_digest: resolved profile requires an exact digest")
        if not isinstance(base, Mapping) or not isinstance(base.get("definition_revision"), str):
            diagnostics.append(
                "$.base.definition_revision: resolved profile requires an exact revision"
            )
        if not isinstance(document.get("catalog_revision"), str):
            diagnostics.append("$.catalog_revision: resolved profile requires a pinned catalog")
        if document.get("profile_authority_snapshot_digest") is None:
            diagnostics.append(
                "$.profile_authority_snapshot_digest: resolved profile requires a pinned snapshot"
            )
        for index, item in enumerate(document.get("packs", [])):
            if isinstance(item, Mapping) and not isinstance(item.get("artifact_digest"), str):
                diagnostics.append(
                    f"$.packs[{index}].artifact_digest: resolved profile requires an exact digest"
                )
    if state == "needs_resolution":
        if document.get("authority_references"):
            diagnostics.append(
                "$.authority_references: unresolved legacy profiles cannot carry authority references"
            )
        if document.get("profile_authority_snapshot_digest") is not None:
            diagnostics.append(
                "$.profile_authority_snapshot_digest: unresolved profile cannot carry authority"
            )
    references = set(document.get("authority_references", []))
    for path, key, value in _walk(document.get("requested_edges", [])):
        if key == "authority_reference" and value is not None and value not in references:
            diagnostics.append(f"{path}: edge reference is not listed in authority_references")
    return diagnostics


def _shell_security_diagnostics(document: Mapping[str, Any]) -> list[str]:
    """Reject any Shell field that attempts to carry execution authority."""
    diagnostics: list[str] = []
    forbidden_fragments = {
        "authority",
        "grant",
        "permission",
        "policy",
        "host_effect",
        "host_execution",
        "pack_execution",
    }
    for path, key, _value in _walk(document):
        normalized = key.lower().replace("-", "_")
        if any(fragment in normalized for fragment in forbidden_fragments):
            diagnostics.append(f"{path}: Shell definitions cannot confer authority: {key}")
    return diagnostics


def _principal_digest_diagnostics(document: Mapping[str, Any]) -> list[str]:
    supplied = document.get("principal_digest")
    if supplied is None:
        return []
    components = {
        key: document[key]
        for key in (
            "parent_artifact_digest",
            "function_implementation_digest",
            "function_id",
            "contract_revision_digest",
            "operation_id",
        )
        if key in document
    }
    from .canonical import canonical_digest

    expected = canonical_digest(components)
    return (
        []
        if supplied == expected
        else ["$.principal_digest: digest does not match principal components"]
    )


def _walk(value: Any, path: str = "$") -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, str(key), child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _walk_containers(value: Any, path: str = "$") -> Iterable[tuple[str, Any]]:
    yield path, value
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk_containers(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_containers(child, f"{path}[{index}]")


def _error_sort_key(error: Any) -> tuple[str, str]:
    return _error_path(error.absolute_path), error.message


def _error_path(path: Iterable[Any]) -> str:
    result = "$"
    for item in path:
        result += f"[{item!r}]" if isinstance(item, int) else f".{item}"
    return result
