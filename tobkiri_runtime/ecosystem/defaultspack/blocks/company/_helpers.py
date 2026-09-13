from __future__ import annotations

from typing import Any

from blocks._common import error


def require_dict(input_data: Any) -> dict[str, Any] | None:
    return input_data if isinstance(input_data, dict) else None


def company_id_from(input_data: dict[str, Any], default: str | None = None) -> str | None:
    value = input_data.get("company_id") or input_data.get("id") or default
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def invalid(message: str):
    return error(message, "INVALID_INPUT")


def missing_company(company_id: str):
    return error("company not found: " + str(company_id), "NOT_FOUND")


def _int_param(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdecimal():
            return int(text)
    return default


def subagent_team_write_denied_for_company(company: Any):
    """Apply the subagent-team write policy to a resolved Company record."""

    metadata = company.get("metadata") if isinstance(company, dict) and isinstance(company.get("metadata"), dict) else {}
    settings = company.get("settings") if isinstance(company, dict) and isinstance(company.get("settings"), dict) else {}
    nested = settings.get("subagent_team") if isinstance(settings.get("subagent_team"), dict) else {}
    if (
        _metadata_marks_subagent_team(metadata)
        or _settings_marks_subagent_team(nested)
    ):
        return error("use /api/subagent-team for subagent team writes", "SUBAGENT_TEAM_POLICY_REQUIRED")
    return None


def company_runtime_route_sunset(route: str):
    """Return the stable Wave 10 sunset diagnostic for retired runtime routes."""

    return error(
        f"{route} is unavailable until its selected Company runtime contract ships",
        "COMPANY_RUNTIME_ROUTE_SUNSET",
    )


def _metadata_marks_subagent_team(metadata: dict[str, Any]) -> bool:
    return (
        bool(metadata.get("subagent_team"))
        or bool(metadata.get("subagent_team_workspace"))
        or metadata.get("surface") == "subagent_team_workspace"
        or metadata.get("workspace_kind") == "subagent_team"
        or metadata.get("frontend_surface") == "subagent_team_workspace"
    )


def _settings_marks_subagent_team(settings: dict[str, Any]) -> bool:
    return (
        settings.get("guard_owner") == "subagent_team_workspace"
        or settings.get("surface") == "subagent_team_workspace"
        or settings.get("workspace_kind") == "subagent_team"
        or settings.get("frontend_surface") == "subagent_team_workspace"
    )


def limit_offset(input_data: dict[str, Any]) -> tuple[int, int]:
    limit = _int_param(input_data.get("limit", 50), 50)
    offset = _int_param(input_data.get("offset", 0), 0)
    if limit < 1:
        limit = 50
    if offset < 0:
        offset = 0
    return limit, offset
