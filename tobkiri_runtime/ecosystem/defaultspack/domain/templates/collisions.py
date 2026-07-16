from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from .models import TemplateDiagnostic


@dataclass(frozen=True)
class CatalogCollision:
    bucket: str
    public_id: str
    projected_ids: tuple[str, ...]
    resolved: bool
    winner_projected_id: str | None
    diagnostics: tuple[TemplateDiagnostic, ...]


IdentityExtractor = Callable[[dict[str, Any]], str]


EXECUTABLE_BUCKETS = {
    "actions",
    "backend_services",
    "api_routes",
    "commands",
    "data_sources",
}
TRUST_RANK = {
    "untrusted": 0,
    "user": 1,
    "local": 2,
    "builtin": 3,
}


def resolve_catalog_collisions(
    catalog: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    resolved = deepcopy(catalog)
    diagnostics: list[dict[str, Any]] = []
    for bucket, extractor in _identity_extractors().items():
        values = resolved.get(bucket)
        if not isinstance(values, list):
            continue
        resolved[bucket], bucket_diagnostics = _resolve_bucket(bucket, values, extractor)
        diagnostics.extend(_diagnostic_to_dict(item) for item in bucket_diagnostics)
    return resolved, diagnostics


def _resolve_bucket(
    bucket: str,
    items: list[dict[str, Any]],
    extractor: IdentityExtractor,
) -> tuple[list[dict[str, Any]], list[TemplateDiagnostic]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    passthrough: list[dict[str, Any]] = []
    for item in items:
        public_id = extractor(item)
        if not public_id:
            passthrough.append(item)
            continue
        if public_id not in groups:
            order.append(public_id)
        groups.setdefault(public_id, []).append(item)

    output: list[dict[str, Any]] = list(passthrough)
    diagnostics: list[TemplateDiagnostic] = []
    for public_id in order:
        group = groups[public_id]
        projected_ids = _projected_ids(group)
        if len(projected_ids) <= 1:
            output.append(group[-1])
            continue
        winner, group_diagnostics = _resolve_override(bucket, public_id, group)
        diagnostics.extend(group_diagnostics)
        if winner is not None:
            output.append(winner)
            continue
        diagnostics.append(_collision_diagnostic(bucket, public_id, group))
    return output, diagnostics


def _resolve_override(
    bucket: str,
    public_id: str,
    group: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[TemplateDiagnostic]]:
    diagnostics: list[TemplateDiagnostic] = []
    by_projected_id = {
        str(item.get("projected_id") or "").strip(): item
        for item in group
        if str(item.get("projected_id") or "").strip()
    }
    override_items = [item for item in group if isinstance(item.get("override"), dict)]
    if not override_items:
        return None, diagnostics
    winner: dict[str, Any] | None = None
    for item in override_items:
        override = item["override"]
        mode = str(override.get("mode") or "").strip()
        target_id = str(override.get("target_projected_id") or "").strip()
        target = by_projected_id.get(target_id)
        invalid_reason = ""
        if mode not in {"replace", "merge"}:
            invalid_reason = "override.mode must be replace or merge"
        elif mode == "merge" and bucket not in {"settings_sections"}:
            invalid_reason = f"override.merge is not supported for bucket {bucket}"
        elif target is None:
            invalid_reason = "override target_projected_id does not exist"
        elif not _trust_allows(item, target, bucket):
            invalid_reason = "override trust level is lower than target trust level"
        if invalid_reason:
            diagnostics.append(
                _invalid_override_diagnostic(bucket, public_id, item, invalid_reason)
            )
            continue
        winner = item
    if winner is None:
        non_overrides = [item for item in group if not isinstance(item.get("override"), dict)]
        if len(non_overrides) == 1:
            return non_overrides[0], diagnostics
    return winner, diagnostics


def _trust_allows(replacer: dict[str, Any], target: dict[str, Any], bucket: str) -> bool:
    replacer_trust = _trust_rank(replacer)
    target_trust = _trust_rank(target)
    if replacer_trust < target_trust:
        return False
    if (
        bucket in EXECUTABLE_BUCKETS
        and _trust(target) in {"builtin", "local"}
        and _trust(replacer)
        in {
            "user",
            "untrusted",
        }
    ):
        return False
    return True


def _collision_diagnostic(
    bucket: str,
    public_id: str,
    group: list[dict[str, Any]],
) -> TemplateDiagnostic:
    projected_ids = _projected_ids(group)
    return TemplateDiagnostic(
        code="template.catalog.public_id_collision",
        message=f"template catalog public id collision in {bucket}: {public_id}",
        template_id=str(group[-1].get("template_id") or "") or None,
        piece_id=str(group[-1].get("piece_id") or "") or None,
        path=f"/{bucket}/{public_id}",
        source_path=str(group[-1].get("_source") or "") or None,
        details={
            "bucket": bucket,
            "public_id": public_id,
            "projected_ids": projected_ids,
            "source_paths": sorted(
                {
                    str(item.get("_source") or item.get("source_path") or "").strip()
                    for item in group
                    if str(item.get("_source") or item.get("source_path") or "").strip()
                }
            ),
        },
    )


def _invalid_override_diagnostic(
    bucket: str,
    public_id: str,
    item: dict[str, Any],
    reason: str,
) -> TemplateDiagnostic:
    return TemplateDiagnostic(
        code="template.catalog.invalid_override",
        message=f"invalid template catalog override for {bucket}:{public_id}: {reason}",
        template_id=str(item.get("template_id") or "") or None,
        piece_id=str(item.get("piece_id") or "") or None,
        path=f"/{bucket}/{public_id}/override",
        source_path=str(item.get("_source") or "") or None,
        details={
            "bucket": bucket,
            "public_id": public_id,
            "projected_id": str(item.get("projected_id") or ""),
            "reason": reason,
        },
    )


def _diagnostic_to_dict(diagnostic: TemplateDiagnostic) -> dict[str, Any]:
    result: dict[str, Any] = {
        "level": diagnostic.severity,
        "severity": diagnostic.severity,
        "code": diagnostic.code,
        "message": diagnostic.message,
        "template_id": diagnostic.template_id,
        "piece_id": diagnostic.piece_id,
        "path": diagnostic.path,
        "source_path": diagnostic.source_path,
        "source": diagnostic.source_path or "template_catalog",
    }
    if diagnostic.details:
        result["details"] = deepcopy(diagnostic.details)
    return result


def _projected_ids(items: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(item.get("projected_id") or "").strip()
                for item in items
                if str(item.get("projected_id") or "").strip()
            }
        )
    )


def _trust(item: dict[str, Any]) -> str:
    return str(item.get("trust_level") or "").strip().lower()


def _trust_rank(item: dict[str, Any]) -> int:
    return TRUST_RANK.get(_trust(item), -1)


def _field(*keys: str) -> IdentityExtractor:
    def extract(item: dict[str, Any]) -> str:
        for key in keys:
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""

    return extract


def _route_identity(item: dict[str, Any]) -> str:
    method = str(item.get("method") or "GET").strip().upper()
    path = str(item.get("path") or item.get("route_path") or "").strip()
    return f"{method} {path}" if path else ""


def _identity_extractors() -> dict[str, IdentityExtractor]:
    return {
        "field_renderers": _field("renderer", "id"),
        "data_sources": _field("data_source", "id"),
        "actions": _field("action_id", "id"),
        "backend_services": _field("service_id", "id"),
        "api_routes": _route_identity,
        "permissions": _field("permission_id", "id"),
        "component_bindings": _field("id"),
        "sidebar_items": _field("id"),
        "chat_renderers": _field("id"),
        "commands": _field("command_id", "id", "name"),
        "composer_inputs": _field("input_id", "id"),
        "composer_widgets": _field("widget_id", "id"),
        "status_surfaces": _field("surface_id", "id"),
        "ai_inputs": _field("ai_input_id", "input_id", "id"),
        "tool_policies": _field("policy_id", "tool_policy_id", "id"),
        "shell_regions": _field("region_id", "id"),
        "shell_renderers": _field("renderer_id", "id"),
        "context_policies": _field("policy_id", "id", "mode"),
        "external_io_templates": _field("id"),
        "test_contracts": _field("contract_id", "id"),
        "source_adapter_contributions": _field("public_id", "id"),
    }
