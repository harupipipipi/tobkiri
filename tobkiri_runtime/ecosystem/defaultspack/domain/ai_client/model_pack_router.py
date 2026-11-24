from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from domain.ai_client.model_pack import ModelPack, ModelPackMember
from domain.ai_client.model_pack_store import ModelPackStore
from domain.ai_client.model_search import get_model_capabilities


@dataclass
class ModelPackRoutingRequest:
    user_text: str = ""
    has_images: bool = False
    requires_tool_calling: bool = False
    requires_fast: bool = False
    requested_thinking_level: str = ""
    task_hints: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelPackSelection:
    pack_id: str
    selected_model: str
    ordered_members: list[dict[str, Any]] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "selected_model": self.selected_model,
            "ordered_members": [dict(item) for item in self.ordered_members],
            "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
        }


def select_model_pack(
    pack_or_ref: ModelPack | str,
    request: ModelPackRoutingRequest | dict[str, Any] | None = None,
    *,
    settings: dict[str, Any] | None = None,
    profiles: list[dict[str, Any]] | None = None,
) -> ModelPackSelection | None:
    if isinstance(pack_or_ref, ModelPack):
        pack = pack_or_ref
    else:
        pack = ModelPackStore(settings).get(pack_or_ref)
    if pack is None:
        return None
    routing_request = _coerce_request(request)
    matching_members = [
        member
        for member in pack.members
        if _member_matches(
            member,
            routing_request,
            profiles=profiles,
            settings=settings,
        )
    ]
    has_hard_requirements = _has_hard_requirements(routing_request)
    selected_members = matching_members or ([] if has_hard_requirements else list(pack.members))
    ordered_members = [_member_payload(member) for member in selected_members if member.model]
    for fallback_model in pack.fallback:
        if fallback_model and fallback_model not in {item.get("model") for item in ordered_members}:
            if has_hard_requirements and not _model_matches(
                fallback_model,
                routing_request,
                profiles=profiles,
                settings=settings,
            ):
                continue
            ordered_members.append({"model": fallback_model, "conditions": {}, "fallback_on": ["any"], "metadata": {"source": "pack_fallback"}})
    reason_codes = ["model_pack", pack.mode]
    if routing_request.has_images:
        reason_codes.append("requires_vision")
    if routing_request.requires_tool_calling:
        reason_codes.append("requires_tool_calling")
    if routing_request.requires_fast:
        reason_codes.append("requires_fast")
    if str(routing_request.requested_thinking_level or "").strip() not in {"", "none"}:
        reason_codes.append("requires_thinking")
    warnings = []
    if has_hard_requirements and not ordered_members:
        warnings.append("no_model_pack_member_satisfied_capabilities")
    if not ordered_members:
        return ModelPackSelection(
            pack_id=pack.id,
            selected_model="",
            ordered_members=[],
            reason_codes=_dedupe(reason_codes),
            warnings=warnings,
        )
    return ModelPackSelection(
        pack_id=pack.id,
        selected_model=str(ordered_members[0].get("model") or ""),
        ordered_members=ordered_members,
        reason_codes=_dedupe(reason_codes),
        warnings=warnings,
    )


def _coerce_request(value: ModelPackRoutingRequest | dict[str, Any] | None) -> ModelPackRoutingRequest:
    if isinstance(value, ModelPackRoutingRequest):
        return value
    raw = value if isinstance(value, dict) else {}
    return ModelPackRoutingRequest(
        user_text=str(raw.get("user_text") or ""),
        has_images=bool(raw.get("has_images")),
        requires_tool_calling=bool(raw.get("requires_tool_calling")),
        requires_fast=bool(raw.get("requires_fast")),
        requested_thinking_level=str(raw.get("requested_thinking_level") or ""),
        task_hints=dict(raw.get("task_hints") if isinstance(raw.get("task_hints"), dict) else {}),
    )


def _member_matches(
    member: ModelPackMember,
    request: ModelPackRoutingRequest,
    *,
    profiles: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
) -> bool:
    raw_capabilities = get_model_capabilities(
        member.model,
        profiles=profiles,
        settings=settings,
    )
    capabilities = raw_capabilities if isinstance(raw_capabilities, dict) else {}
    capabilities_known = _capabilities_known(capabilities)
    conditions = member.conditions if isinstance(member.conditions, dict) else {}
    if request.has_images and not bool(capabilities.get("supports_image_input") or capabilities.get("supports_vision")):
        if capabilities_known or not _condition_declares(conditions, "has_images", "requires_vision"):
            return False
    if request.requires_tool_calling and not bool(capabilities.get("supports_tool_calling")):
        if capabilities_known or not _condition_declares(conditions, "has_tools", "requires_tools"):
            return False
    if request.requires_fast and not bool(capabilities.get("supports_fast")):
        return False
    if str(request.requested_thinking_level or "").strip() not in {"", "none"} and not bool(capabilities.get("supports_thinking")):
        return False
    if "has_images" in conditions and bool(conditions.get("has_images")) != bool(request.has_images):
        return False
    if "requires_vision" in conditions and bool(conditions.get("requires_vision")) != bool(request.has_images):
        return False
    if "has_tools" in conditions and bool(conditions.get("has_tools")) != bool(request.requires_tool_calling):
        return False
    if "requires_tools" in conditions and bool(conditions.get("requires_tools")) != bool(request.requires_tool_calling):
        return False
    task_types = conditions.get("task_types") or conditions.get("task_type")
    if task_types:
        actual_type = str(
            request.task_hints.get("task_type")
            or request.task_hints.get("type")
            or ""
        ).strip().casefold()
        if isinstance(task_types, str):
            options = {task_types.strip().casefold()}
        elif isinstance(task_types, list):
            options = {str(item).strip().casefold() for item in task_types if str(item or "").strip()}
        else:
            options = set()
        if options and actual_type not in options:
            return False
    return True


def _capabilities_known(capabilities: dict[str, Any]) -> bool:
    if not isinstance(capabilities, dict) or not capabilities:
        return False
    return any(
        key in capabilities
        for key in {
            "supports_image_input",
            "supports_vision",
            "supports_tool_calling",
            "supports_fast",
            "supports_thinking",
        }
    )


def _condition_declares(conditions: dict[str, Any], *keys: str) -> bool:
    return any(key in conditions and bool(conditions.get(key)) for key in keys)


def _model_matches(
    model: str,
    request: ModelPackRoutingRequest,
    *,
    profiles: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
) -> bool:
    return _member_matches(
        ModelPackMember(model=model),
        request,
        profiles=profiles,
        settings=settings,
    )


def _has_hard_requirements(request: ModelPackRoutingRequest) -> bool:
    return bool(
        request.has_images
        or request.requires_tool_calling
        or request.requires_fast
        or str(request.requested_thinking_level or "").strip() not in {"", "none"}
    )


def _member_payload(member: ModelPackMember) -> dict[str, Any]:
    payload = member.as_dict()
    payload.setdefault("fallback_on", ["rate_limit", "quota", "provider_error", "timeout"])
    return payload


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output
