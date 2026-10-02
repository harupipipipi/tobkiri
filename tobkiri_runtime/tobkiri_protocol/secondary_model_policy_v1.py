"""Public, provider-neutral secondary model policy and resolution receipts.

Consumers persist a snapshot receipt and pass it on subsequent resolutions.
Profiles come from the captured model owner, never from a consumer's metadata.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

VERSION = "tobkiri.secondary-model-policy.v1"
MODEL_RESOURCE = "tobkiri.resource.ai.model.profile.v1"
MODEL_OPERATION = "rumi_model_registry_pack.model-profile-resource"
THINKING_LEVELS = frozenset(
    {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    }
)


class ModelPolicyResolutionError(ValueError):
    """A fail-closed resolution error retaining the requested policy receipt."""

    def __init__(self, code: str, receipt: Mapping[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.receipt = deepcopy(dict(receipt))


def normalize_model_policy(value: Any) -> dict[str, Any]:
    """Validate a v1 policy without silently changing its mode or fallback."""
    policy = _object(value, "model_policy")
    allowed = {
        "mode",
        "profile_id",
        "snapshot_profile_id",
        "fallback_profile_id",
        "on_unavailable",
        "required_capabilities",
    }
    if set(policy) - allowed:
        raise ValueError("model policy contains unsupported fields")
    mode = policy.get("mode", "inherit_conversation")
    if mode not in {"inherit_conversation", "fixed", "snapshot"}:
        raise ValueError("model policy mode is invalid")
    unavailable = policy.get("on_unavailable", "fail")
    if unavailable not in {"fail", "fallback"}:
        raise ValueError("model policy unavailable behavior is invalid")
    capabilities = policy.get("required_capabilities", [])
    if not isinstance(capabilities, list) or any(
        not isinstance(item, str) or not item for item in capabilities
    ):
        raise ValueError("model capabilities must be a string array")
    result = {
        "mode": mode,
        "profile_id": _text(policy.get("profile_id")),
        "snapshot_profile_id": _text(policy.get("snapshot_profile_id")),
        "fallback_profile_id": _text(policy.get("fallback_profile_id")),
        "on_unavailable": unavailable,
        "required_capabilities": list(dict.fromkeys(capabilities)),
    }
    if mode == "fixed" and not result["profile_id"]:
        raise ValueError("fixed model policy requires profile_id")
    if unavailable == "fallback" and not result["fallback_profile_id"]:
        raise ValueError("fallback policy requires fallback_profile_id")
    return result


def normalize_thinking_policy(value: Any) -> dict[str, str]:
    """Validate explicit or inherited thinking, preserving unsupported requests."""
    policy = _object(value, "thinking_policy")
    if set(policy) - {"mode", "level"}:
        raise ValueError("thinking policy contains unsupported fields")
    mode = policy.get("mode", "inherit_conversation")
    if mode not in {"inherit_conversation", "fixed", "model_default"}:
        raise ValueError("thinking policy mode is invalid")
    level = _text(policy.get("level"))
    if mode == "fixed" and level not in THINKING_LEVELS:
        raise ValueError("thinking policy level is invalid")
    return {"mode": mode, "level": level}


def resolve_secondary_model_policy(
    payload: Mapping[str, Any],
    *,
    profiles: Sequence[Mapping[str, Any]],
    store_revision: int,
) -> dict[str, Any]:
    """Resolve against an authoritative owner projection and return a receipt."""
    model = normalize_model_policy(payload.get("model_policy"))
    thinking = normalize_thinking_policy(payload.get("thinking_policy"))
    context = _object(payload.get("context"), "context")
    snapshot = _object(payload.get("snapshot_receipt"), "snapshot_receipt")
    receipt: dict[str, Any] = {
        "contract_version": VERSION,
        "requested_model_policy": deepcopy(model),
        "requested_thinking_policy": deepcopy(thinking),
        "resolved_profile_id": "",
        "thinking_level": "",
        "store_revision": store_revision,
        "resolution_source": "",
        "fallback_reason": "",
        "thinking_translation": {},
    }
    if model["mode"] == "fixed":
        selected, source = model["profile_id"], "fixed_policy"
    elif model["mode"] == "snapshot":
        if snapshot and snapshot.get("contract_version") != VERSION:
            _fail("MODEL_SNAPSHOT_INVALID", receipt)
        selected = model["snapshot_profile_id"] or _text(snapshot.get("resolved_profile_id"))
        source = "snapshot_receipt" if selected else "snapshot_capture"
        if not selected:
            selected = _inherit(context, "model_profile_id")
    else:
        selected, source = _inherit(context, "model_profile_id"), "conversation"
    index = {_profile_id(item): item for item in profiles}
    profile = index.get(selected)
    code = _unavailable(profile, model["required_capabilities"])
    if code and model["on_unavailable"] == "fallback":
        receipt["fallback_reason"] = code
        selected = model["fallback_profile_id"]
        profile = index.get(selected)
        code = _unavailable(profile, model["required_capabilities"])
        source = "declared_fallback"
    receipt["resolved_profile_id"] = selected
    receipt["resolution_source"] = source
    if code:
        _fail(code, receipt)
    assert profile is not None
    metadata = _object(profile.get("metadata"), "metadata")
    if model["mode"] == "snapshot" and snapshot:
        level = _text(snapshot.get("thinking_level"))
        thinking_source = "snapshot_receipt"
    elif thinking["mode"] == "fixed":
        level, thinking_source = thinking["level"], "fixed_policy"
    elif thinking["mode"] == "model_default":
        parameters = _object(profile.get("parameters"), "parameters")
        level = _text(parameters.get("thinking_level")) or "none"
        thinking_source = "model_default"
    else:
        level = _inherit(context, "thinking_level") or "none"
        thinking_source = "conversation"
    receipt["thinking_level"] = level
    receipt["thinking_source"] = thinking_source
    receipt["thinking_translation"] = {
        "requested": thinking["level"] or thinking["mode"],
        "resolved": level,
        "translated": False,
    }
    if level not in THINKING_LEVELS:
        _fail("THINKING_LEVEL_INVALID", receipt)
    if level != "none":
        levels = metadata.get("thinking_levels", profile.get("thinking_levels"))
        supports = metadata.get("supports_thinking", profile.get("supports_thinking"))
        if supports is not True and not isinstance(levels, list):
            _fail("MODEL_THINKING_UNSUPPORTED", receipt)
        if isinstance(levels, list) and level not in levels:
            _fail("MODEL_THINKING_LEVEL_UNSUPPORTED", receipt)
    receipt["model_id"] = _text(profile.get("model_id"))
    receipt["profile_record_revision"] = int(profile.get("record_revision") or 0)
    if model["mode"] == "snapshot":
        receipt["snapshot_profile_id"] = selected
        receipt["snapshot_captured"] = source == "snapshot_capture"
    return receipt


def _unavailable(profile: Mapping[str, Any] | None, required: list[str]) -> str:
    if profile is None:
        return "MODEL_PROFILE_UNKNOWN"
    metadata = _object(profile.get("metadata"), "metadata")
    availability = _object(profile.get("availability"), "availability")
    if profile.get("enabled") is not True:
        return "MODEL_PROFILE_UNAVAILABLE"
    if availability.get("configured") is False:
        return "MODEL_API_KEY_MISSING"
    if availability.get("active") is False:
        return "MODEL_PROFILE_UNAVAILABLE"
    if metadata.get("requires_api_key") is True and not profile.get("credential_handle"):
        return "MODEL_API_KEY_MISSING"
    capabilities = metadata.get("capabilities", profile.get("capabilities", []))
    if isinstance(capabilities, Mapping):
        supported = {key for key, value in capabilities.items() if value is True}
    elif isinstance(capabilities, list):
        supported = set(capabilities)
    else:
        supported = set()
    for capability in required:
        if capability in {"text", "chat", "model.text"}:
            continue
        key = capability.removeprefix("model.")
        if capability not in supported and key not in supported:
            if metadata.get(f"supports_{key}") is not True:
                return "MODEL_CAPABILITY_UNSATISFIED"
    return ""


def _profile_id(value: Mapping[str, Any]) -> str:
    return _text(value.get("model_profile_id") or value.get("profile_id"))


def _inherit(context: Mapping[str, Any], field: str) -> str:
    for prefix in ("turn", "conversation", "global"):
        value = _text(context.get(f"{prefix}_{field}"))
        if value:
            return value
    return ""


def _object(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError("policy identifier must be a bounded string")
    return value.strip()


def _fail(code: str, receipt: dict[str, Any]) -> None:
    receipt["error"] = {"code": code}
    raise ModelPolicyResolutionError(code, receipt)
