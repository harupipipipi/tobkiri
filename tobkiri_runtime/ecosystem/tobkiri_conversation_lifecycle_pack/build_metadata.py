"""Build scoped reviewed Pack declarations; never release or admission evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.provenance import make_provenance, sha256_file
from tobkiri_protocol.validation import validate_document

PACK = Path(__file__).resolve().parent
ROOT = PACK.parents[1]


def build() -> dict[str, dict[str, Any]]:
    """Derive exact v4 bindings from source declarations and current Pack bytes."""
    source = json.loads((PACK / "pack-source.v1.json").read_text())
    pack_id = source["pack_id"]
    provenance = make_provenance(
        root=ROOT,
        source_path=f"ecosystem/{pack_id}/pack-source.v1.json",
        payload=source,
        source_kind="repository",
        normative=False,
    ).to_dict()
    source_identity = canonical_digest(source)
    implementation = sha256_file(PACK / "runtime/host.py")
    contracts, functions, variants, operations, providers = [], [], [], [], []
    for declaration in source["providers"]:
        function_id = declaration["function_id"]
        operation_id = declaration["operation_id"]
        contract_id = declaration["contract_id"]
        properties: dict[str, Any] = {
            "profile_id": {"type": "string", "minLength": 1},
            "operation": {"type": "string"},
            "_session_id": {"type": "string"},
        }
        required = ["profile_id", "operation"]
        if function_id.endswith(".status"):
            properties.update(
                {
                    "operation": {"const": "get"},
                    "conversation_id": {"type": "string", "minLength": 1},
                }
            )
            required.append("conversation_id")
        elif function_id.endswith(".manage"):
            properties.update(
                {
                    "operation": {"const": "configure"},
                    "conversation_id": {"type": "string", "minLength": 1},
                    "mode": {"enum": ["manual", "archive_after_completion"]},
                }
            )
            required.extend(["conversation_id", "mode"])
        else:
            properties["operation"] = {
                "enum": ["describe", "dispatch", "cancel", "status"]
            }
            for key in ("action_id", "idempotency_key", "schedule_id", "lease_id"):
                properties[key] = {"type": "string", "minLength": 1}
            properties["payload"] = {"type": "object", "maxProperties": 0}
        input_schema = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        output_schema = {"type": "object"}
        error_schema = {"type": "object"}
        schema_catalog = {
            canonical_digest(schema): schema
            for schema in (input_schema, output_schema, error_schema)
        }
        effect = [f"capability:{declaration['capability']}", "host:brokered-execution"]
        contract = {
            "contract_api_version": "io.tobkiri.contract.v4",
            "contract_id": contract_id,
            "version": "1.0.0",
            "owner": pack_id,
            "status": "accepted",
            "operations": [
                {
                    "operation_id": operation_id,
                    "input_schema_digest": canonical_digest(input_schema),
                    "output_schema_digest": canonical_digest(output_schema),
                    "error_schema_digest": canonical_digest(error_schema),
                    "effect_ceiling": effect,
                    "scope_semantics": "host_broker",
                    "idempotency": {"mode": "none"},
                }
            ],
            "schema_catalog": schema_catalog,
            "provider_semantics": {
                "provider_id": function_id,
                "cardinality": "many" if declaration.get("action_ids") else "one",
                "security": "restricted",
                "failure": "fail_closed",
                "isolation": "in_process",
                "required_capabilities": [declaration["capability"]],
                "lifecycle": {"introduced": "1.0.0", "data_owner": pack_id},
            },
            "provenance": provenance,
        }
        if declaration.get("action_ids"):
            contract["provider_semantics"]["instance_key"] = declaration["action_ids"][
                0
            ]
        revision = canonical_digest(contract)
        contract["revision_digest"] = revision
        contracts.append(contract)
        functions.append(
            {
                "id": function_id,
                "implementation_digest": implementation,
                "contract_revision_digest": revision,
                "operations": [operation_id],
                "role": "host_capability_provider",
            }
        )
        operations.append(
            {
                "operation_id": operation_id,
                "owner": pack_id,
                "source_kind": "canonical_v4_contract",
                "effect_ceiling": effect,
                "contract_reference": contract_id,
                "provider_id": function_id,
            }
        )
        providers.append(
            {
                "provider_id": function_id,
                "owner": pack_id,
                "contract_reference": contract_id,
                "operations": [operation_id],
            }
        )
        variants.append(
            {
                "variant_id": f"{function_id}.python",
                "function_id": function_id,
                "implementation_path": "runtime/host.py",
                "implementation_digest": implementation,
                "execution_kind": "host_extension",
                "platform": "any",
                "architecture": "any",
                "runtime_abi": "python3.13",
                "backend": "tobkiri.python-host-v4",
                "materialization_mode": "on_demand",
                "execution_domain_profile": "host.extension.default.v1",
                "operations": [
                    {
                        "contract_id": contract_id,
                        "contract_version": "1.0.0",
                        "revision_digest": revision,
                        "operation_id": operation_id,
                        "input_schema": input_schema,
                        "output_schema": output_schema,
                        "error_schema": error_schema,
                        "effect_class": declaration["effect_class"],
                        "timeout_default_ms": 30000,
                        "timeout_hard_max_ms": 300000,
                        "idempotency": "none",
                    }
                ],
            }
        )
    catalog = {
        "catalog_api_version": "io.tobkiri.pack-contract-catalog.v4",
        "pack_id": pack_id,
        "source_identity": source_identity,
        "contracts": contracts,
    }
    executable = {
        "catalog_api_version": "io.tobkiri.executable-catalog.v4",
        "pack_id": pack_id,
        "source_identity": source_identity,
        "variants": variants,
    }
    executable["catalog_digest"] = canonical_digest(executable)
    assets = [
        {"path": path, "digest": sha256_file(PACK / path), "kind": kind}
        for path, kind in (
            ("runtime/host.py", "executable"),
            ("runtime/lifecycle.py", "sidecar"),
            ("frontend/contributions/lifecycle.json", "ui.contribution"),
            ("pack-source.v1.json", "sidecar"),
        )
    ]
    manifest = {
        "pack_api_version": "io.tobkiri.pack.v4",
        "pack": {
            "id": pack_id,
            "version": "1.0.0",
            "kind": "host_extension",
            "display_name": source["display_name"],
            "artifact_digest": canonical_digest(
                {"source": source, "artifacts": assets}
            ),
        },
        "functions": functions,
        "contracts": [
            {
                "contract_id": contract["contract_id"],
                "revision_digest": contract["revision_digest"],
                "operations": [contract["operations"][0]["operation_id"]],
            }
            for contract in contracts
        ],
        "artifacts": assets,
        "requirements": {
            "pack_dependencies": {},
            "contract_dependencies": [
                {
                    "contract_id": dependency["contract_id"],
                    "version_range": dependency["version_range"],
                    "cardinality": "one",
                    "optional": False,
                    "operations": [dependency["operation_id"]],
                }
                for dependency in source["public_dependencies"]
            ],
            "capabilities": [
                "conversation.read",
                "conversation.manage",
                "schedule.read",
                "schedule.manage",
                *[item["capability"] for item in source["providers"]],
            ],
            "network": source["network"],
            "secrets": [],
            "execution_boundary": "host_brokered",
            "approval_policy": "capability_gated",
            "workspace_boundary": "none",
        },
        "operation_catalog": operations,
        "provider_catalog": providers,
        "integrity": {
            "source_identity": source_identity,
            "artifact_set_digest": canonical_digest(assets),
            "contract_catalog_digest": canonical_digest(catalog),
        },
        "provenance": provenance,
        "migration": {
            "compatibility": "none",
            "legacy_ids": [],
            "removal_wave": 0,
            "sunset_at": "2027-12-31",
        },
    }
    outputs = {
        "pack.v4.json": manifest,
        "contracts.v4.json": catalog,
        "executables.v4.json": executable,
    }
    for filename, schema in (
        ("pack.v4.json", "pack_manifest_v4.schema.json"),
        ("contracts.v4.json", "pack_contract_catalog_v4.schema.json"),
        ("executables.v4.json", "executable_catalog_v4.schema.json"),
    ):
        validate_document(outputs[filename], schema)
    return outputs


if __name__ == "__main__":
    for filename, document in build().items():
        (PACK / filename).write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
