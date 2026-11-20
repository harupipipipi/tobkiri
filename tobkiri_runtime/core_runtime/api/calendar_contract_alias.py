"""Bind the bundled Calendar client's URLs to the current signed contract map."""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import unquote

from core_runtime.global_contracts.http_contract_dispatch import HTTPContractBinding


_SCHEDULE_PATH = "/api/agent/schedules/"
_CONCRETE_PATH = re.compile(re.escape(_SCHEDULE_PATH) + r"([^/]+)(/[^/]+)?")
_SCHEDULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_OPERATIONS = {
    ("PUT", ""): "update",
    ("DELETE", ""): "delete",
    ("GET", "/history"): "history",
    ("POST", "/pause"): "pause",
    ("POST", "/resume"): "resume",
    ("POST", "/trigger"): "trigger",
}


def bundled_calendar_contract_alias(
    server: Any, method: str, path: str, namespace: str,
) -> tuple[str, dict[str, str]] | None:
    """Accept the concrete URLs still emitted by the signed bundled client."""
    if namespace != "defaultspack":
        return None
    match = _CONCRETE_PATH.fullmatch(path)
    if match is None:
        return None
    encoded_id, suffix = match.groups()
    suffix = suffix or ""
    operation = _OPERATIONS.get((method, suffix))
    if operation is None:
        return None
    try:
        schedule_id = unquote(encoded_id, errors="strict")
    except UnicodeDecodeError:
        return None
    if _SCHEDULE_ID.fullmatch(schedule_id) is None:
        return None

    template = _SCHEDULE_PATH + "{schedule_id}" + suffix
    routes = getattr(server, "_contract_routes", None)
    if not isinstance(routes, Mapping):
        return None
    binding = routes.get((method, template))
    if (
        not isinstance(binding, HTTPContractBinding)
        or binding.method != method
        or binding.path != template
        or binding.application_id != "runtime.tauri.application.default"
        or binding.route_namespace != namespace
        or binding.presentation != "broker_result"
        or len(binding.targets) != 1
    ):
        return None
    target = binding.targets[0]
    read = operation == "history"
    function = "rumi_schedule_store_pack.calendar." + ("resource" if read else "manage")
    kind = "resource" if read else "action"
    verb = "read" if read else "manage"
    allowed_keys = {"schedule_id"}
    if read:
        allowed_keys.update(("limit", "offset"))
    elif operation == "update":
        allowed_keys.update(
            ("name", "description", "schedule_type", "schedule_config", "task")
        )
    if (
        target.contribution_id != "defaults.calendar.schedules." + operation
        or target.contract_id != f"tobkiri.{kind}.calendar.schedule.v1"
        or target.operation_id != f"rumi_schedule_store_pack.calendar-{verb}"
        or target.provider_id != function
        or target.function_id != function
        or target.allowed_payload_keys != allowed_keys
    ):
        return None
    return template, {"schedule_id": schedule_id}
