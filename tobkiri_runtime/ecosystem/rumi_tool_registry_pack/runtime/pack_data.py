"""Project sealed Default Tools descriptors without loading their Python services."""

from __future__ import annotations

import json
from typing import Any

from core_runtime.host_provider_backend_v4 import CapturedHostPackDataV4
from ecosystem.rumi_tool_registry_pack.runtime.registry import _definition

DEFAULT_TOOLS_PACK = "rumi_default_tools_pack"
_LOCAL_PROVIDER = "rumi_default_tool_projection_pack.tool-adapter.defaultspack-compat"
_LOCAL_OPERATION = "rumi_default_tool_projection_pack.default-tool-local-operation"
_AUTHORITIES = frozenset(
    {
        "file.read",
        "file.write",
        "shell.inspect",
        "shell.execute",
        "git.read",
        "git.write",
        "git.publish",
        "browser.observe",
        "browser.control",
        "desktop.observe",
        "desktop.control",
        "clipboard.read",
        "clipboard.write",
    }
)


def definitions_from_pack_data(
    data: tuple[CapturedHostPackDataV4, ...],
) -> tuple[dict[str, Any], ...]:
    """Normalize selected packaged descriptors; execution remains Broker-owned.

    This intake covers sealed ``tools/*/manifest.json`` data only. Dynamic,
    component and memo definitions remain separate explicit owner contributions.
    """
    if len(data) > 1:
        raise ValueError("tool descriptor source is duplicated")
    definitions = []
    for source in data:
        if source.pack_id != DEFAULT_TOOLS_PACK or source.path_prefix != "tools/":
            raise ValueError("tool descriptor source is invalid")
        if not source.files:
            raise ValueError("selected tool descriptor source is empty")
        for item in source.files:
            parts = item.path.split("/")
            if len(parts) != 3 or parts[0] != "tools" or parts[2] != "manifest.json":
                raise ValueError("tool descriptor path is invalid")
            raw = json.loads(item.content)
            if not isinstance(raw, dict) or raw.get("category") != "tool":
                raise ValueError("tool descriptor is invalid")
            config = raw.get("config")
            if not isinstance(config, dict) or (
                raw.get("id") != parts[1] or config.get("name") != parts[1]
            ):
                raise ValueError("tool descriptor identity is invalid")
            enabled = raw.get("enabled", True)
            if type(enabled) is not bool:
                raise ValueError("tool descriptor enabled flag is invalid")
            if not enabled:
                continue
            schema = config.get("schema")
            if not isinstance(schema, dict) or not isinstance(schema.get("parameters"), dict):
                raise ValueError("tool descriptor schema is invalid")
            grants = config.get("capability_grants", [])
            if not isinstance(grants, list) or not all(isinstance(grant, str) for grant in grants):
                raise ValueError("tool descriptor capabilities are invalid")
            # These labels describe required authority; they never grant it.
            authority = next((grant for grant in grants if grant in _AUTHORITIES), None)
            if authority is None:
                authority = (
                    "service.mutate"
                    if config.get("requires_approval") or config.get("approval_policy") == "ask"
                    else "service.invoke"
                )
            definitions.append(
                _definition(
                    {
                        "tool_id": parts[1],
                        "display_name": raw.get("display_name", parts[1]),
                        "description": raw.get("description") or config.get("summary", ""),
                        "input_schema": schema["parameters"],
                        "execution": {
                            "kind": "local",
                            "contract_id": "tobkiri.service.tool.local.operation.v1",
                            "provider_instance_id": _LOCAL_PROVIDER,
                            "operation": _LOCAL_OPERATION,
                        },
                        "authority": authority,
                        "risk": config.get("risk", "unknown"),
                        "policy_tags": config.get("tags", []),
                        "aliases": config.get("aliases", []),
                        "widget": config.get("ui", {}),
                        "source_adapter_id": DEFAULT_TOOLS_PACK,
                    }
                )
            )
    return tuple(definitions)
