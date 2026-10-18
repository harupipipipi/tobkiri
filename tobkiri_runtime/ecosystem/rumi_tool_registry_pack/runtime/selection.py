"""Select owned schemas against captured routes, without granting invocation."""

from __future__ import annotations

from typing import Any, Mapping

from tobkiri_protocol.saved_tools import validate_tool_selection
from tobkiri_protocol.tool_groups import infer_tool_service

EXECUTE = "tobkiri.service.tool.execute.v1"
LOCAL = "tobkiri.service.tool.local.operation.v1"
MCP = "tobkiri.service.mcp.tool.call.v1"
ROUTE_CONTRACTS = frozenset({EXECUTE, LOCAL, MCP})


def select_tools(
    catalog: Mapping[str, Any], selection: Any, client: Any,
) -> dict[str, Any]:
    """Return only executable selected schemas and their owner definition hashes."""
    selection = validate_tool_selection(selection)
    definitions = {item["tool_id"]: item for item in catalog["definitions"]}
    aliases = catalog.get("aliases", {})

    def expand(items: list[Any]) -> set[str]:
        names: set[str] = set()
        for item in items:
            kind, identifier = ("tool", item) if isinstance(item, str) else (item["kind"], item["id"])
            if kind == "service":
                matches = {
                    name for name, value in definitions.items()
                    if infer_tool_service({**value, "name": name, "ui": value.get("widget", {})}) == identifier
                }
            else:
                name = aliases.get(identifier, identifier)
                matches = {name} if name in definitions else set()
            if not matches:
                raise ValueError("selected tool or service is not registered")
            names.update(matches)
        return names

    included = expand(selection.get("include", []))
    excluded = expand(selection.get("exclude", []))
    candidates = (
        set(definitions) if selection["mode"] == "auto"
        else included if selection["mode"] == "manual" else set()
    ) - excluded
    routes = {contract: tuple(client.providers(contract)) for contract in ROUTE_CONTRACTS}
    available = {name for name in candidates if _available(definitions[name], routes)}
    if (included - excluded) - available or (selection.get("must_use") and not available):
        raise PermissionError("selected tool execution is unavailable")
    selected = [definitions[name] for name in sorted(available)]
    return {
        "tools": [{"type": "function", "function": {
            "name": item["tool_id"], "description": item["description"],
            "parameters": item["input_schema"],
        }} for item in selected],
        "definitions": {item["tool_id"]: item["definition_hash"] for item in selected},
    }


def _available(definition: Mapping[str, Any], routes: Mapping[str, tuple]) -> bool:
    execution = definition["execution"]
    kind = execution.get("kind")
    if kind not in {"local", "mcp"}:
        return False
    contract = LOCAL if kind == "local" else MCP
    if execution.get("contract_id") != contract:
        return False
    pack = f"rumi_tool_{kind}_executor_pack"
    executors = [
        item for item in routes[EXECUTE]
        if item.get("function_id") == f"{pack}.tool-executor.{kind}"
        and item.get("operation_id") == f"{pack}.tool-{kind}-execute"
        and item.get("backend_id") and not item.get("backend_unavailable_reason")
    ]
    operations = [
        item for item in routes[contract]
        if execution.get("provider_instance_id") in (item.get("provider_instance_id"), item.get("function_id"))
        and item.get("operation_id") == (
            execution.get("operation") if kind == "local" else "rumi_mcp_gateway_pack.mcp-tool-call"
        )
        and item.get("backend_id") and not item.get("backend_unavailable_reason")
    ]
    return len(executors) == len(operations) == 1
