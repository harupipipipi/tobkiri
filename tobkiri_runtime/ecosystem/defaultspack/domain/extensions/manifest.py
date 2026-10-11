from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from jsonschema import Draft202012Validator

from .categories import DEFAULT_CATEGORY_SPECS

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-/]{1,256}$")
_STABLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*$")
_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_MARKETPLACE_STATUSES = {"verified", "unverified", "bundled", "local"}
_SIGNING_MODES = {
    "none",
    "repository_reviewed",
    "repository_trusted",
    "marketplace",
    "sha256",
    "hmac",
    "ed25519",
}
_VERSIONED_SCHEMA_FILES = {
    "tobkiri.activity/v1": "activity.v1.schema.json",
    "tobkiri.skill/v2": "skill.v2.schema.json",
    "tobkiri.tool/v3": "tool.v3.schema.json",
}


class ManifestValidationError(ValueError):
    pass


def _validate_json_contract(raw: Dict[str, Any], schema_version: str) -> None:
    schema_name = _VERSIONED_SCHEMA_FILES.get(schema_version)
    if not schema_name:
        return
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / schema_name
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestValidationError(
            f"versioned manifest schema is unavailable: {schema_name}"
        ) from exc
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ManifestValidationError(
            f"versioned manifest schema is invalid: {schema_name}"
        ) from exc
    encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 1_000_000:
        raise ManifestValidationError("versioned manifest exceeds 1 MB")
    _validate_manifest_complexity(raw)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(raw),
        key=lambda error: list(error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    location = ".".join(str(part) for part in error.absolute_path)
    prefix = f"manifest.{location}" if location else "manifest"
    raise ManifestValidationError(f"{prefix}: {error.message}")


def _validate_manifest_complexity(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 50_000:
            raise ManifestValidationError("versioned manifest is too complex")
        if depth > 64:
            raise ManifestValidationError("versioned manifest nesting is too deep")
        if isinstance(item, dict):
            if len(item) > 2_000:
                raise ManifestValidationError("versioned manifest object is too large")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > 10_000:
                raise ManifestValidationError("versioned manifest array is too large")
            stack.extend((child, depth + 1) for child in item)


def _as_dict(value: Any, key_name: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    raise ManifestValidationError(f"{key_name} must be an object")


def _normalize_env_field(value: Any, key_name: str) -> str | List[str]:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        normalized: List[str] = []
        for item in value:
            env_name = str(item or "").strip()
            if env_name and env_name not in normalized:
                normalized.append(env_name)
        return normalized
    raise ManifestValidationError(f"{key_name} must be a string or array of strings")


def _normalize_string_list(value: Any, key_name: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [item.strip() for item in value.replace(",", "\n").splitlines()]
    if not isinstance(value, (list, tuple)):
        raise ManifestValidationError(f"{key_name} must be a string or array")
    normalized: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _normalize_localized_text(value: Any, key_name: str, fallback: str) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{key_name} must be a string or locale object")
    normalized = {
        str(locale).strip(): str(text or "").strip()
        for locale, text in value.items()
        if str(locale).strip() and str(text or "").strip()
    }
    if not normalized:
        raise ManifestValidationError(f"{key_name} locale object must not be empty")
    return normalized


def _validate_versioned_contract(
    manifest: Dict[str, Any],
    *,
    category: str,
    extension_id: str,
    version: str,
) -> str:
    schema_version = str(manifest.get("schema_version") or "").strip()
    if not schema_version:
        return ""
    expected = {
        "activity": "tobkiri.activity/v1",
        "skill": "tobkiri.skill/v2",
        "tool": "tobkiri.tool/v3",
    }.get(category)
    if expected and schema_version != expected:
        raise ManifestValidationError(
            f"manifest.schema_version must be {expected} for {category}"
        )
    kind = str(manifest.get("kind") or category).strip()
    if kind != category:
        raise ManifestValidationError(
            f"manifest.kind mismatch: expected={category}, actual={kind}"
        )
    if not _STABLE_ID_PATTERN.fullmatch(extension_id):
        raise ManifestValidationError(
            "versioned manifest.id must be a stable lowercase identifier"
        )
    if not _SEMVER_PATTERN.fullmatch(version):
        raise ManifestValidationError("versioned manifest.version must be SemVer")
    _validate_json_contract(manifest, schema_version)
    return schema_version


def _normalize_marketplace(value: Any) -> Dict[str, Any]:
    marketplace = _as_dict(value, "manifest.marketplace")
    if not marketplace:
        return {}
    status = str(marketplace.get("status") or "unverified").strip().lower()
    if status == "blacklisted":
        raise ManifestValidationError("manifest.marketplace.status is blocked: blacklisted")
    if status not in _MARKETPLACE_STATUSES:
        raise ManifestValidationError(f"manifest.marketplace.status is unsupported: {status}")
    normalized = dict(marketplace)
    normalized["status"] = status
    if "publisher" in normalized:
        normalized["publisher"] = str(normalized.get("publisher") or "").strip()
    if "registry" in normalized:
        normalized["registry"] = str(normalized.get("registry") or "").strip()
    return normalized


def _normalize_signing(value: Any) -> Dict[str, Any]:
    signing = _as_dict(value, "manifest.signing")
    if not signing:
        return {}
    mode = str(signing.get("mode") or "none").strip().lower()
    if mode not in _SIGNING_MODES:
        raise ManifestValidationError(f"manifest.signing.mode is unsupported: {mode}")
    normalized = dict(signing)
    normalized["mode"] = mode
    normalized["verified"] = bool(signing.get("verified", False))
    if bool(signing.get("required")) and not (
        normalized["verified"]
        or str(signing.get("signature") or "").strip()
        or str(signing.get("sha256") or "").strip()
    ):
        raise ManifestValidationError(
            "manifest.signing requires signature, sha256, or verified=true"
        )
    return normalized


def validate_manifest(
    raw: Dict[str, Any],
    *,
    expected_category: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ManifestValidationError("manifest root must be an object")

    manifest = dict(raw)
    ext_id = str(manifest.get("id", "")).strip()
    if not ext_id:
        raise ManifestValidationError("manifest.id is required")
    if not _ID_PATTERN.match(ext_id):
        raise ManifestValidationError(
            "manifest.id must match ^[A-Za-z0-9_.\\-/]{1,256}$"
        )

    category = str(manifest.get("category") or manifest.get("kind") or "").strip()
    if not category:
        raise ManifestValidationError("manifest.category is required")
    if category not in DEFAULT_CATEGORY_SPECS:
        raise ManifestValidationError(f"unsupported manifest.category: {category}")
    if expected_category and category != expected_category:
        raise ManifestValidationError(
            f"manifest.category mismatch: expected={expected_category}, actual={category}"
        )

    version = str(manifest.get("version", "1")).strip() or "1"
    enabled = bool(manifest.get("enabled", True))
    schema_version = _validate_versioned_contract(
        manifest,
        category=category,
        extension_id=ext_id,
        version=version,
    )

    normalized: Dict[str, Any] = dict(manifest)
    normalized["id"] = ext_id
    normalized["category"] = category
    normalized["version"] = version
    normalized["enabled"] = enabled
    if schema_version:
        normalized["schema_version"] = schema_version
        normalized["kind"] = category
        normalized["display_name"] = _normalize_localized_text(
            manifest.get("display_name"), "manifest.display_name", ext_id
        )
        normalized["description"] = _normalize_localized_text(
            manifest.get("description"), "manifest.description", ""
        )
    else:
        normalized["display_name"] = str(manifest.get("display_name", ext_id))
        normalized["description"] = str(manifest.get("description", ""))
    normalized["metadata"] = _as_dict(manifest.get("metadata"), "manifest.metadata")
    normalized["config"] = _as_dict(manifest.get("config"), "manifest.config")
    normalized["capabilities"] = _as_dict(
        manifest.get("capabilities"), "manifest.capabilities"
    )
    normalized["marketplace"] = _normalize_marketplace(manifest.get("marketplace"))
    normalized["signing"] = _normalize_signing(manifest.get("signing"))

    if category == "llm_provider":
        adapter = str(manifest.get("adapter", "")).strip()
        entrypoint = str(manifest.get("entrypoint", "")).strip()
        if not adapter and not entrypoint:
            raise ManifestValidationError(
                "llm_provider manifest requires either adapter or entrypoint"
            )
        if adapter:
            normalized["adapter"] = adapter
        if entrypoint:
            normalized["entrypoint"] = entrypoint
        api_key_env = _normalize_env_field(
            manifest.get("api_key_env"), "manifest.api_key_env"
        )
        if api_key_env:
            normalized["api_key_env"] = api_key_env
        base_url_env = str(manifest.get("base_url_env", "")).strip()
        if base_url_env:
            normalized["base_url_env"] = base_url_env
        default_base_url = str(manifest.get("default_base_url", "")).strip()
        if default_base_url:
            normalized["default_base_url"] = default_base_url
        default_model = str(manifest.get("default_model", "")).strip()
        if default_model:
            normalized["default_model"] = default_model
        default_model_for = _as_dict(
            manifest.get("default_model_for"), "manifest.default_model_for"
        )
        if default_model_for:
            normalized["default_model_for"] = {
                str(key): str(value) for key, value in default_model_for.items() if value
            }
        headers = _as_dict(manifest.get("headers"), "manifest.headers")
        if headers:
            normalized["headers"] = {
                str(key): str(value) for key, value in headers.items()
            }
        known_models = manifest.get("models")
        if known_models is not None:
            if not isinstance(known_models, list):
                raise ManifestValidationError("manifest.models must be an array")
            normalized["models"] = list(known_models)
        normalized["credential_required"] = bool(
            manifest.get("credential_required", bool(api_key_env))
        )
        normalized["priority"] = int(manifest.get("priority", 100))

    if category == "llm_model":
        provider_id = str(manifest.get("provider_id", "")).strip()
        model_id = str(manifest.get("model_id", "")).strip()
        if not provider_id or not model_id:
            if "/" in ext_id and not provider_id and not model_id:
                provider_id, model_id = ext_id.split("/", 1)
            else:
                raise ManifestValidationError(
                    "llm_model manifest requires provider_id and model_id"
                )
        normalized["provider_id"] = provider_id
        normalized["model_id"] = model_id
        normalized["display_name"] = str(
            manifest.get("display_name", model_id or ext_id)
        )
        normalized["defaults"] = _as_dict(manifest.get("defaults"), "manifest.defaults")
        normalized["priority"] = int(manifest.get("priority", 100))
        normalized["type"] = str(manifest.get("type", "chat"))
        for numeric_key in ("context_window", "max_context", "max_context_tokens"):
            if numeric_key in manifest:
                normalized[numeric_key] = int(manifest.get(numeric_key, 0))
        if "supports_thinking" in manifest:
            normalized["supports_thinking"] = bool(manifest.get("supports_thinking"))
        if "thinking_levels" in manifest:
            levels = manifest.get("thinking_levels")
            if not isinstance(levels, list):
                raise ManifestValidationError("manifest.thinking_levels must be an array")
            normalized["thinking_levels"] = [str(level) for level in levels]
        if "default_thinking_level" in manifest:
            normalized["default_thinking_level"] = str(manifest.get("default_thinking_level") or "")

    if category == "skill":
        if schema_version == "tobkiri.skill/v2":
            instructions = _as_dict(
                manifest.get("instructions"), "manifest.instructions"
            )
            path = str(instructions.get("path") or "").strip()
            if path != "SKILL.md":
                raise ManifestValidationError(
                    "skill v2 manifest.instructions.path must be SKILL.md"
                )
            max_tokens = int(instructions.get("max_tokens", 0))
            if max_tokens <= 0:
                raise ManifestValidationError(
                    "skill v2 manifest.instructions.max_tokens must be positive"
                )
            activation = _as_dict(
                manifest.get("activation"), "manifest.activation"
            )
            scope = _as_dict(manifest.get("scope"), "manifest.scope")
            composition = _as_dict(
                manifest.get("composition"), "manifest.composition"
            )
            tool_policy = _as_dict(
                manifest.get("tool_policy"), "manifest.tool_policy"
            )
            security = _as_dict(manifest.get("security"), "manifest.security")
            if security.get("may_grant_permissions") is not False:
                raise ManifestValidationError(
                    "skill v2 security.may_grant_permissions must be false"
                )
            normalized["instructions"] = {
                **instructions,
                "path": path,
                "max_tokens": max_tokens,
            }
            normalized["activation"] = activation
            normalized["scope"] = {
                **scope,
                "activity_ids": _normalize_string_list(
                    scope.get("activity_ids"), "manifest.scope.activity_ids"
                ),
                "tool_ids": _normalize_string_list(
                    scope.get("tool_ids"), "manifest.scope.tool_ids"
                ),
            }
            normalized["composition"] = composition
            normalized["tool_policy"] = {
                **tool_policy,
                "allowed_tool_ids": _normalize_string_list(
                    tool_policy.get("allowed_tool_ids"),
                    "manifest.tool_policy.allowed_tool_ids",
                ),
                "denied_tool_ids": _normalize_string_list(
                    tool_policy.get("denied_tool_ids"),
                    "manifest.tool_policy.denied_tool_ids",
                ),
            }
            normalized["security"] = security
            normalized["triggers"] = _normalize_string_list(
                activation.get("positive_examples"),
                "manifest.activation.positive_examples",
            )
            normalized["applies_to_tools"] = list(
                normalized["scope"]["tool_ids"]
            )
            return normalized
        triggers = manifest.get("triggers", manifest.get("keywords", []))
        if isinstance(triggers, str):
            triggers = [item.strip() for item in triggers.split(",") if item.strip()]
        if triggers is None:
            triggers = []
        if not isinstance(triggers, list):
            raise ManifestValidationError("manifest.triggers must be a string or array")
        normalized["triggers"] = [str(item).strip() for item in triggers if str(item).strip()]
        applies_to_tools = manifest.get("applies_to_tools", manifest.get("tool_ids", []))
        if isinstance(applies_to_tools, str):
            applies_to_tools = [item.strip() for item in applies_to_tools.split(",") if item.strip()]
        if applies_to_tools is None:
            applies_to_tools = []
        if not isinstance(applies_to_tools, list):
            raise ManifestValidationError("manifest.applies_to_tools must be a string or array")
        normalized["applies_to_tools"] = [
            str(item).strip() for item in applies_to_tools if str(item).strip()
        ]

    if category == "activity":
        members = _as_dict(manifest.get("members"), "manifest.members")
        skills = _as_dict(members.get("skills"), "manifest.members.skills")
        selection = _as_dict(manifest.get("selection"), "manifest.selection")
        permissions = _as_dict(
            manifest.get("permissions"), "manifest.permissions"
        )
        ui = _as_dict(manifest.get("ui"), "manifest.ui")
        max_candidate_tools = int(selection.get("max_candidate_tools", 20))
        max_attached_tools = int(selection.get("max_attached_tools", 8))
        if max_candidate_tools <= 0 or max_attached_tools <= 0:
            raise ManifestValidationError(
                "activity selection limits must be positive"
            )
        if max_attached_tools > 8:
            raise ManifestValidationError(
                "activity selection.max_attached_tools must not exceed 8"
            )
        normalized["aliases"] = _normalize_string_list(
            manifest.get("aliases"), "manifest.aliases"
        )
        normalized["members"] = {
            **members,
            "tool_ids": _normalize_string_list(
                members.get("tool_ids"), "manifest.members.tool_ids"
            ),
            "tool_tags": _normalize_string_list(
                members.get("tool_tags"), "manifest.members.tool_tags"
            ),
            "service_ids": _normalize_string_list(
                members.get("service_ids"), "manifest.members.service_ids"
            ),
            "skills": {
                **skills,
                "required": _normalize_string_list(
                    skills.get("required"), "manifest.members.skills.required"
                ),
                "optional": _normalize_string_list(
                    skills.get("optional"), "manifest.members.skills.optional"
                ),
                "safety": _normalize_string_list(
                    skills.get("safety"), "manifest.members.skills.safety"
                ),
            },
        }
        normalized["selection"] = {
            **selection,
            "max_candidate_tools": max_candidate_tools,
            "max_attached_tools": max_attached_tools,
        }
        normalized["permissions"] = permissions
        normalized["ui"] = ui

    if category == "tool" and schema_version == "tobkiri.tool/v3":
        contract = _as_dict(manifest.get("contract"), "manifest.contract")
        input_schema = contract.get("input_schema")
        output_schema = contract.get("output_schema")
        if not isinstance(input_schema, dict):
            raise ManifestValidationError(
                "tool v3 contract.input_schema must be an object"
            )
        if output_schema is not None and not isinstance(output_schema, dict):
            raise ManifestValidationError(
                "tool v3 contract.output_schema must be an object"
            )
        execution = _as_dict(manifest.get("execution"), "manifest.execution")
        execution_type = str(execution.get("type") or "").strip()
        if execution_type not in {
            "rumi_function",
            "capability",
            "mcp",
            "sandbox_function",
            "declarative_http",
        }:
            raise ManifestValidationError(
                f"tool v3 execution.type is unsupported: {execution_type or '<missing>'}"
            )
        security = _as_dict(manifest.get("security"), "manifest.security")
        if "trusted" in manifest or "trusted" in security:
            raise ManifestValidationError(
                "tool trust is loader-owned and may not be asserted by a manifest"
            )
        normalized["contract"] = {
            **contract,
            "input_schema": dict(input_schema),
            "output_schema": dict(output_schema or {}),
        }
        normalized["execution"] = execution
        normalized["effects"] = list(manifest.get("effects") or [])
        normalized["risk"] = _as_dict(manifest.get("risk"), "manifest.risk")
        normalized["approval"] = _as_dict(
            manifest.get("approval"), "manifest.approval"
        )
        normalized["requirements"] = _as_dict(
            manifest.get("requirements"), "manifest.requirements"
        )
        normalized["security"] = security
        normalized["ui"] = _as_dict(manifest.get("ui"), "manifest.ui")

    return normalized
