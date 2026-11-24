from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_provider_tool_schema_infers_and_sanitizes_schema_shapes():
    from domain.tool.schema_adapter import sanitize_provider_tool_schema

    schema = sanitize_provider_tool_schema(
        {
            "properties": {
                "query": {"description": "search query"},
                "tags": {"type": "array"},
                "limit": {"minimum": 1},
                "mode": {"const": "fast"},
                "enabled": True,
            },
            "required": ["query"],
            "additionalProperties": False,
        }
    )

    assert schema == {
        "type": "object",
        "properties": {
            "query": {},
            "tags": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "number"},
            "mode": {"type": "string", "enum": ["fast"]},
            "enabled": {"type": "string"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }


def test_provider_tool_schema_coerces_malformed_child_schemas_to_permissive_shapes():
    from domain.tool.schema_adapter import sanitize_provider_tool_schema

    schema = sanitize_provider_tool_schema(
        {
            "type": "object",
            "properties": {
                "metadata": None,
                "tags": {"type": "array", "items": "string"},
                "choice": {"anyOf": ["string", {"type": "number"}]},
            },
        }
    )

    assert schema == {
        "type": "object",
        "properties": {
            "metadata": {},
            "tags": {"type": "array", "items": {"type": "string"}},
            "choice": {"anyOf": [{}, {"type": "number"}]},
        },
    }


def test_provider_tool_schema_preserves_reachable_local_refs_and_prunes_defs():
    from domain.tool.schema_adapter import sanitize_provider_tool_schema

    schema = sanitize_provider_tool_schema(
        {
            "type": "object",
            "properties": {
                "user": {"$ref": "#/$defs/User%20Name"},
                "remote": {"$ref": "https://example.com/schema.json"},
            },
            "$defs": {
                "User Name": {"type": "object", "properties": {"name": {"type": "string"}}},
                "Unused": {"type": "boolean"},
            },
        }
    )

    assert schema == {
        "type": "object",
        "properties": {
            "user": {"$ref": "#/$defs/User%20Name"},
            "remote": {"$ref": "https://example.com/schema.json"},
        },
        "$defs": {
            "User Name": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
    }


def test_provider_tool_schema_compacts_large_definition_payloads():
    from domain.tool.schema_adapter import sanitize_provider_tool_schema

    properties = {f"field_{index:03}": {"type": "string"} for index in range(300)}
    schema = sanitize_provider_tool_schema(
        {
            "type": "object",
            "description": "x" * 4500,
            "properties": {
                "event": {
                    "type": "object",
                    "description": "event",
                    "properties": {"name": {"type": "string", "description": "name"}},
                },
                "metadata": {"$ref": "#/$defs/Metadata"},
            },
            "$defs": {
                "Metadata": {
                    "type": "object",
                    "description": "metadata",
                    "properties": properties,
                }
            },
        }
    )

    assert schema == {
        "type": "object",
        "properties": {
            "event": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
            "metadata": {},
        },
    }


def test_provider_tool_schema_fails_closed_when_flat_payload_exceeds_budget():
    from domain.tool.schema_adapter import ToolSchemaError, sanitize_provider_tool_schema

    with pytest.raises(ToolSchemaError, match="exceeds the provider budget"):
        sanitize_provider_tool_schema(
            {
                "type": "object",
                "properties": {
                    f"field_{index:04}": {
                        "type": "string",
                        "description": "large generated field",
                    }
                    for index in range(500)
                },
            }
        )


def test_function_tool_adapter_sanitizes_existing_function_parameters():
    from domain.tool.schema_adapter import adapt_tool_definition

    adapted = adapt_tool_definition(
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Lookup",
                "parameters": {
                    "properties": {
                        "target": {"format": "uri"},
                    }
                },
            },
        }
    )

    assert adapted["function"]["parameters"] == {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
        },
    }


def test_provider_adapter_and_manifest_helper_use_safe_parameters():
    from domain.tool.provider_adapter import adapt_rumi_tools_to_provider_tools
    from domain.tool.tool_manifest_helpers import provider_tool_schema

    tool_def = {
        "tool_id": "legacy_tool",
        "summary": "Legacy",
        "schema": {
            "parameters": {
                "properties": {
                    "choice": {"type": "enum", "enum": ["one", "two"]},
                }
            }
        },
    }

    provider_tools, _mapping, _definitions = adapt_rumi_tools_to_provider_tools([tool_def])
    assert provider_tools[0]["function"]["parameters"]["properties"]["choice"] == {
        "type": "string",
        "enum": ["one", "two"],
    }
    assert provider_tool_schema(tool_def)["function"]["parameters"]["properties"]["choice"] == {
        "type": "string",
        "enum": ["one", "two"],
    }


def test_provider_tool_schema_rejects_singleton_null_at_every_adapter_boundary():
    from domain.tool.schema_adapter import ToolSchemaError, provider_tool_parameters, sanitize_provider_tool_schema

    with pytest.raises(ToolSchemaError):
        sanitize_provider_tool_schema({"type": "null"})

    with pytest.raises(ToolSchemaError):
        provider_tool_parameters({"type": "null"})
