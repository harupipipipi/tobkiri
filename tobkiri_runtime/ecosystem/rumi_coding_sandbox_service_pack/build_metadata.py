"""Scoped adoption input for finite tasks alongside unchanged legacy declarations."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.provenance import make_provenance, sha256_file
from tobkiri_protocol.validation import validate_document

PACK = Path(__file__).resolve().parent
ROOT = PACK.parents[1]


def build() -> dict[str, Any]:
    """Bind current host bytes, operations, schemas and legacy compatibility source."""
    source = json.loads((PACK / "pack-source.v1.json").read_text())
    compatibility = json.loads((PACK / source["compatibility_source"]).read_text())
    manifest = deepcopy(compatibility["pack"])
    catalog = deepcopy(compatibility["contracts"])
    executable = deepcopy(compatibility["executables"])
    identity = canonical_digest(source)
    pack_id = source["pack_id"]
    provenance = make_provenance(
        root=ROOT,
        source_path=f"ecosystem/{pack_id}/pack-source.v1.json",
        payload=source,
        source_kind="repository",
        normative=False,
    ).to_dict()
    implementation = sha256_file(PACK / source["implementation_path"])
    groups: dict[str, list[dict[str, Any]]] = {}
    for provider in source["providers"]:
        groups.setdefault(provider["contract_id"], []).append(provider)
    semantic, provided = [], []
    for contract_id, providers in groups.items():
        document = {
            "contract_api_version": "io.tobkiri.contract.v4",
            "contract_id": contract_id,
            "version": "1.0.0",
            "owner": pack_id,
            "status": "accepted",
            "operations": [
                {
                    "operation_id": item["operation_id"],
                    **{
                        f"{kind}_schema_digest": canonical_digest(schema)
                        for kind, schema in item["schemas"].items()
                    },
                    "effect_ceiling": item["effect_ceiling"],
                    "scope_semantics": "host_broker",
                    "idempotency": {"mode": "none"},
                }
                for item in providers
            ],
            "schema_catalog": {
                canonical_digest(schema): schema
                for item in providers
                for schema in item["schemas"].values()
            },
            "provider_semantics": {
                "provider_id": providers[-1]["function_id"],
                "cardinality": "one",
                "security": "restricted",
                "failure": "fail_closed",
                "isolation": "in_process",
                "required_capabilities": sorted(
                    {
                        effect.removeprefix("capability:")
                        for item in providers
                        for effect in item["effect_ceiling"]
                        if effect.startswith("capability:")
                    }
                ),
                "lifecycle": {"introduced": "1.0.0", "data_owner": pack_id},
            },
            "provenance": provenance,
        }
        revision = canonical_digest(document)
        document["revision_digest"] = revision
        catalog["contracts"].append(document)
        manifest["contracts"].append(
            {
                "contract_id": contract_id,
                "revision_digest": revision,
                "operations": [item["operation_id"] for item in providers],
            }
        )
        provided.append(
            {
                "contract_id": contract_id,
                "version": "1.0.0",
                "owner": pack_id,
                **document["provider_semantics"],
                "schemas": {
                    "input": {
                        "oneOf": [item["schemas"]["input"] for item in providers]
                    },
                    "output": {"type": "object"},
                    "error": {"type": "object"},
                },
                "operations": [
                    {
                        "id": item["operation_id"],
                        "entrypoint_id": item["operation_id"].split(".")[-1],
                        "implementation_digest": implementation,
                        "effect_ceiling": item["effect_ceiling"],
                    }
                    for item in providers
                ],
            }
        )
        for item in providers:
            function, operation = item["function_id"], item["operation_id"]
            manifest["functions"].append(
                {
                    "id": function,
                    "implementation_digest": implementation,
                    "contract_revision_digest": revision,
                    "operations": [operation],
                    "role": "host_capability_provider",
                }
            )
            manifest["operation_catalog"].append(
                {
                    "operation_id": operation,
                    "owner": pack_id,
                    "source_kind": "canonical_v4_contract",
                    "effect_ceiling": item["effect_ceiling"],
                    "contract_reference": contract_id,
                    "provider_id": function,
                }
            )
            manifest["provider_catalog"].append(
                {
                    "provider_id": function,
                    "owner": pack_id,
                    "contract_reference": contract_id,
                    "operations": [operation],
                }
            )
            executable["variants"].append(
                {
                    "variant_id": function + ".python",
                    "function_id": function,
                    "implementation_path": source["implementation_path"],
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
                            "operation_id": operation,
                            **{
                                f"{kind}_schema": schema
                                for kind, schema in item["schemas"].items()
                            },
                            "effect_class": item["effect_class"],
                            "timeout_default_ms": 30000,
                            "timeout_hard_max_ms": 180000,
                            "idempotency": "none",
                        }
                    ],
                }
            )
            semantic.append(
                {
                    "function_id": function,
                    "contract_id": contract_id,
                    "contract_version": "1.0.0",
                    "operation_ids": [operation],
                    "implementation_path": source["implementation_path"],
                    "schemas": item["schemas"],
                }
            )
    assets = [
        {
            "path": item["path"],
            "kind": item["kind"],
            "digest": sha256_file(PACK / item["path"]),
        }
        for item in source["runtime_artifacts"]
    ]
    manifest["artifacts"] = assets
    requirements = manifest["requirements"]
    requirements["capabilities"] = sorted(
        set(
            requirements["capabilities"]
            + source["public_capabilities"]
            + [item["capability"] for item in source["providers"]]
        )
    )
    dependencies = [
        {
            "contract_id": item["contract_id"],
            "version_range": item["version_range"],
            "cardinality": "one",
            "optional": item["optional"],
            "operations": [item["operation_id"]],
        }
        for item in source["public_dependencies"]
    ]
    requirements["contract_dependencies"].extend(dependencies)
    manifest["pack"].update(
        display_name=source["display_name"],
        artifact_digest=canonical_digest({"source": source, "assets": assets}),
    )
    manifest["provenance"] = provenance
    catalog["source_identity"] = executable["source_identity"] = identity
    executable.pop("catalog_digest", None)
    executable["catalog_digest"] = canonical_digest(executable)
    manifest["integrity"] = {
        "source_identity": identity,
        "artifact_set_digest": canonical_digest(assets),
        "contract_catalog_digest": canonical_digest(catalog),
    }
    integration = {
        "schema": "tobkiri.pack-integration-input.v1",
        "pack_catalog_record": {
            "functions": manifest["functions"],
            "pack_id": pack_id,
            "authority": "v4-authoritative",
            "display_name": source["display_name"],
            "description": "Captured local COW container tasks with ordinary Host approval.",
            "version": "1.0.0",
            "kind": "host_extension",
            "dependencies": requirements["pack_dependencies"],
            **{
                key: value
                for key, value in requirements.items()
                if key not in {"pack_dependencies", "contract_dependencies"}
            },
            "required_contracts": requirements["contract_dependencies"],
            "provided_contracts": [*compatibility["provided_contracts"], *provided],
            "runtime_artifacts": assets,
            "legacy_ids": manifest["migration"]["legacy_ids"],
            "legacy_operations": compatibility["legacy_operations"],
            "artifact_metadata": {
                "mode": "canonical-v4",
                "owner": pack_id,
                "source_format": "pack.v4.json",
            },
        },
        "semantic_source_entry": {"entries": semantic},
        "effect_reviews": source["providers"],
        "host_factory": {
            "module": source["runtime_module"],
            "symbol": source["factory_symbol"],
        },
        "declared_pack_data": source["declared_pack_data"],
        "protocol_sources": source["protocol_sources"],
        "ui_contributions": [],
        "requested_default_selection": "ROOT reviewed adoption required; no install evidence",
    }
    outputs = {
        "pack.v4.json": manifest,
        "contracts.v4.json": catalog,
        "executables.v4.json": executable,
        "integration-input.v1.json": integration,
    }
    for filename, schema in [
        ("pack.v4.json", "pack_manifest_v4.schema.json"),
        ("contracts.v4.json", "pack_contract_catalog_v4.schema.json"),
        ("executables.v4.json", "executable_catalog_v4.schema.json"),
    ]:
        validate_document(outputs[filename], schema)
    return outputs


if __name__ == "__main__":
    for filename, document in build().items():
        (PACK / filename).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n"
        )
