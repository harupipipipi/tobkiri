"""Project sealed tool descriptors without loading their Python services."""

from __future__ import annotations

import json
import re
from typing import Any

from core_runtime.host_provider_backend_v4 import CapturedHostPackDataV4
from ecosystem.rumi_tool_registry_pack.runtime.registry import _definition

DEFAULT_TOOLS_PACK = "rumi_default_tools_pack"
DEFAULTS_PACK = "defaultspack"
_DATA_SOURCES = frozenset(
    {
        (DEFAULT_TOOLS_PACK, "tools/"),
        (DEFAULTS_PACK, "tools/"),
        (DEFAULTS_PACK, "extensions/tools/"),
    }
)
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

    This intake covers sealed tool and tool-extension manifests, including the
    owned memo descriptors. Dynamic and component definitions remain separate
    explicit owner contributions.
    """
    sources = {(item.pack_id, item.path_prefix) for item in data}
    if len(data) > len(_DATA_SOURCES) or len(sources) != len(data):
        raise ValueError("tool descriptor source is duplicated")
    definitions = []
    pack_digests: dict[str, str] = {}
    for source in data:
        if (source.pack_id, source.path_prefix) not in _DATA_SOURCES:
            raise ValueError("tool descriptor source is invalid")
        digest = pack_digests.setdefault(source.pack_id, source.artifact_digest)
        if digest != source.artifact_digest:
            raise ValueError("tool descriptor sources disagree on Pack identity")
        if not source.files:
            raise ValueError("selected tool descriptor source is empty")
        prefix = source.path_prefix.rstrip("/").split("/")
        for item in source.files:
            parts = item.path.split("/")
            if (
                len(parts) != len(prefix) + 2
                or parts[:-2] != prefix
                or parts[-1] != "manifest.json"
            ):
                raise ValueError("tool descriptor path is invalid")
            tool_id = parts[-2]
            raw = json.loads(item.content)
            if not isinstance(raw, dict):
                raise ValueError("tool descriptor is invalid")
            config = raw.get("config")
            if not isinstance(config, dict) or raw.get("id") != tool_id:
                raise ValueError("tool descriptor identity is invalid")
            if source.pack_id == DEFAULT_TOOLS_PACK:
                if raw.get("category") != "tool" or config.get("name") != tool_id:
                    raise ValueError("tool descriptor identity is invalid")
            elif (
                raw.get("category") not in (None, "tool")
                or config.get("tool_id") != tool_id
                or (
                    source.path_prefix == "extensions/tools/"
                    and raw.get("category") != "tool"
                )
            ):
                # Defaults' existing format uses tool_id as identity and name
                # as display text. Do not rewrite source IDs from those labels.
                raise ValueError("Defaults tool descriptor identity is invalid")
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
                        "tool_id": tool_id,
                        "display_name": (
                            raw.get("display_name") or config.get("name") or tool_id
                        ),
                        "description": raw.get("description") or config.get("summary", ""),
                        "input_schema": schema["parameters"],
                        "execution": _execution(config),
                        "authority": authority,
                        "risk": config.get("risk", "unknown"),
                        "policy_tags": config.get("tags", []),
                        "aliases": config.get("aliases", []),
                        "widget": config.get("ui", {}),
                        "source_adapter_id": source.pack_id,
                    }
                )
            )
    return tuple(definitions)


def _execution(config: dict[str, Any]) -> dict[str, str]:
    """Retain exact declared local routes; legacy descriptors stay unavailable."""
    value = config.get("execution", {})
    if not isinstance(value, dict):
        raise ValueError("tool execution declaration is invalid")
    if value.get("type") != "global_contract":
        return {
            "kind": "local",
            "contract_id": "tobkiri.service.tool.local.operation.v1",
            "provider_instance_id": _LOCAL_PROVIDER,
            "operation": _LOCAL_OPERATION,
        }
    if (
        set(value) != {"type", "contract_id", "provider_instance_id", "operation"}
        or value["contract_id"] != "tobkiri.service.tool.local.operation.v1"
        or any(
            not isinstance(value[key], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", value[key]) is None
            for key in ("provider_instance_id", "operation")
        )
    ):
        raise ValueError("tool local operation declaration is invalid")
    return {"kind": "local", **{key: value[key] for key in value if key != "type"}}
