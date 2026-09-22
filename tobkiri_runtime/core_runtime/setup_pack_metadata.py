"""Shared setup-pack metadata normalization and validation helpers."""

from __future__ import annotations

from typing import Any, Dict, List

MARKETPLACE_STATUSES = {"verified", "unverified", "blacklisted", "bundled", "local"}
SIGNING_MODES = {
    "none",
    "repository_reviewed",
    "repository_trusted",
    "marketplace",
    "sha256",
    "hmac",
    "ed25519",
}


def as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def normalize_dependency_specs(value: Any) -> List[Dict[str, str]]:
    raw_items = value if isinstance(value, list) else []
    if isinstance(value, dict):
        raw_items = [
            {"pack_id": key, **spec} if isinstance(spec, dict) else {"pack_id": key}
            for key, spec in value.items()
        ]
    result: List[Dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, str):
            spec = {"pack_id": item}
        elif isinstance(item, dict):
            pack_id = item.get("pack_id") or item.get("id") or item.get("name")
            if not pack_id:
                continue
            spec = {"pack_id": str(pack_id)}
            version = item.get("version") or item.get("constraint") or item.get("version_constraint")
            if version:
                spec["version"] = str(version)
        else:
            continue
        if spec["pack_id"] not in seen:
            seen.add(spec["pack_id"])
            result.append(spec)
    return result


def normalize_pack_ref_specs(value: Any) -> List[Dict[str, str]]:
    raw_items = value if isinstance(value, list) else []
    if isinstance(value, dict):
        raw_items = [
            {"pack_id": key, **spec} if isinstance(spec, dict) else {"pack_id": key}
            for key, spec in value.items()
        ]
    result: List[Dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, str):
            spec = {"pack_id": item}
        elif isinstance(item, dict):
            pack_id = item.get("pack_id") or item.get("id") or item.get("name")
            if not pack_id:
                continue
            spec = {"pack_id": str(pack_id)}
            for key in ("reason", "resolution", "scope"):
                if item.get(key):
                    spec[key] = str(item[key])
        else:
            continue
        if spec["pack_id"] not in seen:
            seen.add(spec["pack_id"])
            result.append(spec)
    return result


def has_signing_proof(signing: Dict[str, Any]) -> bool:
    if not isinstance(signing, dict):
        return False
    return (
        signing.get("verified") is True
        or bool(str(signing.get("signature") or "").strip())
        or bool(str(signing.get("sha256") or "").strip())
    )


def _issue(
    pack_id: str,
    target_pack_id: str,
    reason: str,
    error: str,
    *,
    severity: str = "error",
    **extra: Any,
) -> Dict[str, Any]:
    issue = {
        "setup_pack_id": pack_id,
        "target_pack_id": target_pack_id,
        "reason": reason,
        "error": error,
        "severity": severity,
    }
    issue.update(extra)
    return issue


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_string_field(
    issues: List[Dict[str, Any]],
    raw: Dict[str, Any],
    field_name: str,
    pack_id: str,
    target_pack_id: str,
    *,
    required: bool = False,
) -> None:
    if field_name not in raw:
        if required:
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "invalid_setup_pack_schema",
                    f"setup-pack field '{field_name}' is required",
                    field=field_name,
                )
            )
        return
    if not _is_non_empty_string(raw[field_name]):
        issues.append(
            _issue(
                pack_id,
                target_pack_id,
                "invalid_setup_pack_schema",
                f"setup-pack field '{field_name}' must be a non-empty string",
                field=field_name,
            )
        )


def _validate_bool_field(
    issues: List[Dict[str, Any]],
    raw: Dict[str, Any],
    field_name: str,
    pack_id: str,
    target_pack_id: str,
) -> None:
    if field_name in raw and not isinstance(raw[field_name], bool):
        issues.append(
            _issue(
                pack_id,
                target_pack_id,
                "invalid_setup_pack_schema",
                f"setup-pack field '{field_name}' must be a boolean",
                field=field_name,
            )
        )


def _validate_dict_field(
    issues: List[Dict[str, Any]],
    raw: Dict[str, Any],
    field_name: str,
    pack_id: str,
    target_pack_id: str,
) -> None:
    if field_name in raw and not isinstance(raw[field_name], dict):
        issues.append(
            _issue(
                pack_id,
                target_pack_id,
                "invalid_setup_pack_schema",
                f"setup-pack field '{field_name}' must be an object",
                field=field_name,
            )
        )


def _validate_dependency_collection(
    issues: List[Dict[str, Any]],
    value: Any,
    field_name: str,
    pack_id: str,
    target_pack_id: str,
) -> None:
    if value is None:
        return
    if not isinstance(value, (list, dict)):
        issues.append(
            _issue(
                pack_id,
                target_pack_id,
                "invalid_setup_pack_schema",
                f"setup-pack field '{field_name}' must be a list or object",
                field=field_name,
            )
        )
        return
    if isinstance(value, dict):
        iterable = value.items()
        for key, spec in iterable:
            if not _is_non_empty_string(key):
                issues.append(
                    _issue(
                        pack_id,
                        target_pack_id,
                        "invalid_setup_pack_schema",
                        f"setup-pack field '{field_name}' has an empty dependency id",
                        field=field_name,
                    )
                )
            if not isinstance(spec, (dict, str, type(None))):
                issues.append(
                    _issue(
                        pack_id,
                        target_pack_id,
                        "invalid_setup_pack_schema",
                        f"setup-pack field '{field_name}.{key}' must be an object",
                        field=field_name,
                    )
                )
        return
    for index, item in enumerate(value):
        field = f"{field_name}[{index}]"
        if isinstance(item, str):
            if not item.strip():
                issues.append(
                    _issue(
                        pack_id,
                        target_pack_id,
                        "invalid_setup_pack_schema",
                        f"setup-pack field '{field}' must not be empty",
                        field=field,
                    )
                )
            continue
        if not isinstance(item, dict):
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "invalid_setup_pack_schema",
                    f"setup-pack field '{field}' must be a string or object",
                    field=field,
                )
            )
            continue
        dep_id = item.get("pack_id") or item.get("id") or item.get("name")
        if not _is_non_empty_string(dep_id):
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "invalid_setup_pack_schema",
                    f"setup-pack field '{field}.pack_id' is required",
                    field=field,
                )
            )
        for version_key in ("version", "constraint", "version_constraint"):
            if version_key in item and not isinstance(item[version_key], str):
                issues.append(
                    _issue(
                        pack_id,
                        target_pack_id,
                        "invalid_setup_pack_schema",
                        f"setup-pack field '{field}.{version_key}' must be a string",
                        field=f"{field}.{version_key}",
                    )
                )


def _validate_pack_ref_collection(
    issues: List[Dict[str, Any]],
    value: Any,
    field_name: str,
    pack_id: str,
    target_pack_id: str,
) -> None:
    if value is None:
        return
    if not isinstance(value, (list, dict)):
        issues.append(
            _issue(
                pack_id,
                target_pack_id,
                "invalid_setup_pack_schema",
                f"setup-pack field '{field_name}' must be a list or object",
                field=field_name,
            )
        )
        return
    if isinstance(value, dict):
        items = [
            {"pack_id": key, **spec} if isinstance(spec, dict) else {"pack_id": key}
            for key, spec in value.items()
        ]
    else:
        items = value
    for index, item in enumerate(items):
        field = f"{field_name}[{index}]"
        if isinstance(item, str):
            if not item.strip():
                issues.append(
                    _issue(
                        pack_id,
                        target_pack_id,
                        "invalid_setup_pack_schema",
                        f"setup-pack field '{field}' must not be empty",
                        field=field,
                    )
                )
            continue
        if not isinstance(item, dict):
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "invalid_setup_pack_schema",
                    f"setup-pack field '{field}' must be a string or object",
                    field=field,
                )
            )
            continue
        ref_id = item.get("pack_id") or item.get("id") or item.get("name")
        if not _is_non_empty_string(ref_id):
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "invalid_setup_pack_schema",
                    f"setup-pack field '{field}.pack_id' is required",
                    field=field,
                )
            )
        for metadata_key in ("reason", "resolution", "scope"):
            if metadata_key in item and not isinstance(item[metadata_key], str):
                issues.append(
                    _issue(
                        pack_id,
                        target_pack_id,
                        "invalid_setup_pack_schema",
                        f"setup-pack field '{field}.{metadata_key}' must be a string",
                        field=f"{field}.{metadata_key}",
                    )
                )


def validate_setup_pack_schema(
    raw: Any,
    *,
    fallback_pack_id: str = "",
    fallback_target_pack_id: str = "",
) -> List[Dict[str, Any]]:
    pack_id = fallback_pack_id or ""
    target_pack_id = fallback_target_pack_id or pack_id
    if not isinstance(raw, dict):
        return [
            _issue(
                pack_id,
                target_pack_id,
                "invalid_setup_pack_schema",
                "setup-pack manifest root must be an object",
                field="pack.json",
            )
        ]

    pack_id = str(raw.get("pack_id") or fallback_pack_id or "")
    target_pack_id = str(raw.get("target_pack_id") or fallback_target_pack_id or pack_id)
    issues: List[Dict[str, Any]] = []

    _validate_string_field(issues, raw, "pack_id", pack_id, target_pack_id, required=True)
    _validate_string_field(issues, raw, "target_pack_id", pack_id, target_pack_id)
    for field_name in ("display_name", "description", "version", "risk_level"):
        _validate_string_field(issues, raw, field_name, pack_id, target_pack_id)
    for field_name in ("recommended", "supports_all_ok", "all_ok_eligible"):
        _validate_bool_field(issues, raw, field_name, pack_id, target_pack_id)
    for field_name in (
        "compatibility",
        "marketplace",
        "signing",
        "overlap_policy",
        "base_pack_promotion",
    ):
        _validate_dict_field(issues, raw, field_name, pack_id, target_pack_id)
    if "depends_on" in raw:
        _validate_dependency_collection(
            issues, raw.get("depends_on"), "depends_on", pack_id, target_pack_id
        )
    if "dependencies" in raw:
        _validate_dependency_collection(
            issues, raw.get("dependencies"), "dependencies", pack_id, target_pack_id
        )
    if "conflicts_with" in raw:
        _validate_pack_ref_collection(
            issues, raw.get("conflicts_with"), "conflicts_with", pack_id, target_pack_id
        )

    marketplace = raw.get("marketplace")
    if isinstance(marketplace, dict) and "status" in marketplace:
        status = marketplace["status"]
        if not isinstance(status, str) or not status.strip():
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "invalid_setup_pack_schema",
                    "setup-pack field 'marketplace.status' must be a non-empty string",
                    field="marketplace.status",
                )
            )

    signing = raw.get("signing")
    if isinstance(signing, dict):
        for field_name in ("mode", "algorithm", "signature", "sha256"):
            if field_name in signing and not isinstance(signing[field_name], str):
                issues.append(
                    _issue(
                        pack_id,
                        target_pack_id,
                        "invalid_setup_pack_schema",
                        f"setup-pack field 'signing.{field_name}' must be a string",
                        field=f"signing.{field_name}",
                    )
                )
        if "required" in signing and not isinstance(signing["required"], bool):
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "invalid_setup_pack_schema",
                    "setup-pack field 'signing.required' must be a boolean",
                    field="signing.required",
                )
            )
        if "verified" in signing and not isinstance(signing["verified"], bool):
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "invalid_setup_pack_schema",
                    "setup-pack field 'signing.verified' must be a boolean",
                    field="signing.verified",
                )
            )

    return issues


def validate_setup_pack_metadata(
    *,
    pack_id: str,
    target_pack_id: str,
    marketplace: Dict[str, Any] | None = None,
    signing: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    marketplace = marketplace if isinstance(marketplace, dict) else {}
    if marketplace:
        if not any(marketplace.get(key) for key in ("registry", "id", "url", "source")):
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "invalid_marketplace_metadata",
                    "Marketplace metadata must identify a registry, id, URL, or source",
                    severity="warning",
                )
            )
        status = str(marketplace.get("status") or "unverified").strip().lower()
        if status not in MARKETPLACE_STATUSES:
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "invalid_marketplace_status",
                    "Unsupported marketplace status: " + status,
                )
            )
        elif status == "blacklisted":
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "marketplace_blacklisted",
                    "Setup pack is blacklisted by marketplace metadata",
                )
            )

    signing = signing if isinstance(signing, dict) else {}
    if signing:
        mode = str(signing.get("mode") or "none").strip().lower()
        if mode not in SIGNING_MODES:
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "invalid_signing_mode",
                    "Unsupported signing mode: " + mode,
                )
            )
        if bool(signing.get("required")) and not has_signing_proof(signing):
            issues.append(
                _issue(
                    pack_id,
                    target_pack_id,
                    "missing_required_signature",
                    "Setup pack requires signing metadata but no signature/hash is present",
                )
            )
    return issues
