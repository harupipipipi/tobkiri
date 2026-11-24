"""Present the selected Registry's definitions to the Defaults tool picker."""

from __future__ import annotations

from typing import Any, Mapping
from copy import deepcopy
import json
import re

from core_runtime.pack_api_server import DispatchSession
from ..domain.tool.definition_projection import project_tool_definition
from ..domain.tool.service_catalog import ToolServiceCatalog


TOOL_CATALOG_TARGET = (
    "defaults.tools.catalog",
    "tobkiri.resource.tool.definition.v1",
    "rumi_tool_registry_pack.tool-definition-resource",
    "rumi_tool_registry_pack.tool-registry.definition",
    "rumi_tool_registry_pack.tool-registry.definition",
)


def present_tool_catalog(
    result: Mapping[str, object],
    *,
    session: DispatchSession | None,
) -> dict[str, object]:
    """Show real descriptors; a listed definition never grants execution authority."""
    if session is None:
        raise ValueError("tool catalog requires a captured session")
    session.assert_current()
    if result.get("state") == "error":
        return dict(result)
    definitions = result.get("definitions")
    revision = result.get("revision")
    if (
        not isinstance(definitions, list)
        or type(revision) is not int
        or revision < 0
        or result.get("profile_id") != session.profile_id
    ):
        raise ValueError("tool registry returned an invalid catalog")
    tools = []
    identifiers: set[str] = set()
    ready: dict[tuple[str, str, str], bool] = {}
    for definition in definitions:
        if not isinstance(definition, Mapping):
            raise ValueError("tool registry returned an invalid definition")
        identifier = definition.get("tool_id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("tool registry returned an invalid identity")
        identifiers.add(identifier)
        tool = project_tool_definition(definition)
        tool["availability"] = {
            "status": "ready" if _local_route_ready(tool, session, ready) else "unavailable"
        }
        tools.append(tool)
    catalog = ToolServiceCatalog(tools)
    records = catalog.compact_records()
    definitions_by_id = {definition["tool_id"]: definition for definition in definitions}
    for record in records:
        definition = definitions_by_id[record["tool_id"]]
        schema = definition.get("input_schema")
        digest = definition.get("definition_hash")
        # Display metadata is not an execution grant. The broker independently
        # resolves this identity and compares its current definition hash.
        if (isinstance(schema, Mapping) and isinstance(digest, str)
                and re.fullmatch(r"[0-9a-f]{64}", digest)
                and len(json.dumps(schema, ensure_ascii=False, allow_nan=False).encode()) <= 32768):
            record["input_schema"] = deepcopy(dict(schema))
            record["definition_hash"] = digest
            result_schema = definition.get("result_schema", {})
            if (definition.get("result_schema_format") == "normalized-result.v1"
                    and isinstance(result_schema, Mapping)
                    and len(json.dumps(result_schema, ensure_ascii=False, allow_nan=False).encode()) <= 32768):
                record["result_schema"] = deepcopy(dict(result_schema))
                record["result_schema_format"] = "normalized-result.v1"
    session.assert_current()
    return {
        "services": catalog.services(),
        "tools": records,
        "count": len(tools),
        "registry_revision": revision,
        "profile_id": session.profile_id,
        "plan_digest": getattr(session, "plan_digest", None),
    }


def _local_route_ready(
    tool: Mapping[str, Any],
    session: DispatchSession,
    ready: dict[tuple[str, str, str], bool],
) -> bool:
    execution = tool["execution"]
    # A remote connection needs its own owner's health evidence. A ready
    # generic transport does not prove an individual MCP tool is connected.
    if execution.get("kind") != "local":
        return False
    identity = tuple(
        execution.get(key)
        for key in (
            "contract_id",
            "operation",
            "provider_instance_id",
        )
    )
    if not all(isinstance(value, str) and value for value in identity):
        return False
    contract, operation, provider = identity
    key = (contract, operation, provider)
    if key not in ready:
        matches = [
            item
            for item in session.provider_metadata(contract)
            if provider in (item.get("provider_instance_id"), item.get("function_id"))
            and item.get("operation_id") == operation
        ]
        ready[key] = False
        if len(matches) == 1:
            try:
                session.assert_operation_ready(contract, operation)
            except Exception:
                # Preserve the descriptor, with unavailable execution. Stale
                # Profile/session failures are still fenced before publication.
                pass
            else:
                ready[key] = True
    return ready[key]
