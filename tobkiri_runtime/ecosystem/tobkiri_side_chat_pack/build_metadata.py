"""Build scoped reviewed source inputs; no signing, install or release receipts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.provenance import make_provenance, sha256_file
from tobkiri_protocol.validation import validate_document

PACK = Path(__file__).resolve().parent
ROOT = PACK.parents[1]


def build() -> dict[str, Any]:
    """Derive exact declarations from this Pack's reviewed source and bytes."""
    source = json.loads((PACK / "pack-source.v1.json").read_text())
    pack_id = source["pack_id"]
    identity = canonical_digest(source)
    provenance = make_provenance(
        root=ROOT,
        source_path=f"ecosystem/{pack_id}/pack-source.v1.json",
        payload=source,
        source_kind="repository",
        normative=False,
    ).to_dict()
    implementation = sha256_file(PACK / source["implementation_path"])
    contracts, functions, variants, semantic, provided = [], [], [], [], []
    operations, providers = [], []
    for entry in source["providers"]:
        schemas = entry["schemas"]
        function, operation, contract = (
            entry[key] for key in ("function_id", "operation_id", "contract_id")
        )
        document = {
            "contract_api_version": "io.tobkiri.contract.v4",
            "contract_id": contract,
            "version": "1.0.0",
            "owner": pack_id,
            "status": "accepted",
            "operations": [
                {
                    "operation_id": operation,
                    **{
                        f"{kind}_schema_digest": canonical_digest(schema)
                        for kind, schema in schemas.items()
                    },
                    "effect_ceiling": entry["effect_ceiling"],
                    "scope_semantics": "host_broker",
                    "idempotency": {"mode": "none"},
                }
            ],
            "schema_catalog": {
                canonical_digest(value): value for value in schemas.values()
            },
            "provider_semantics": {
                "provider_id": function,
                "cardinality": "one",
                "security": "restricted",
                "failure": "fail_closed",
                "isolation": "in_process",
                "required_capabilities": [entry["capability"]],
                "lifecycle": {"introduced": "1.0.0", "data_owner": pack_id},
            },
            "provenance": provenance,
        }
        revision = canonical_digest(document)
        document["revision_digest"] = revision
        contracts.append(document)
        functions.append(
            {
                "id": function,
                "implementation_digest": implementation,
                "contract_revision_digest": revision,
                "operations": [operation],
                "role": "host_capability_provider",
            }
        )
        variants.append(
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
                        "contract_id": contract,
                        "contract_version": "1.0.0",
                        "revision_digest": revision,
                        "operation_id": operation,
                        **{f"{key}_schema": value for key, value in schemas.items()},
                        "effect_class": entry["effect_class"],
                        "timeout_default_ms": 30000,
                        "timeout_hard_max_ms": 300000,
                        "idempotency": "none",
                    }
                ],
            }
        )
        semantic.append(
            {
                "function_id": function,
                "contract_id": contract,
                "contract_version": "1.0.0",
                "operation_ids": [operation],
                "implementation_path": source["implementation_path"],
                "schemas": schemas,
            }
        )
        provided.append(
            {
                "contract_id": contract,
                "version": "1.0.0",
                "owner": pack_id,
                **document["provider_semantics"],
                "schemas": schemas,
                "operations": [
                    {
                        "id": operation,
                        "entrypoint_id": operation.split(".")[-1],
                        "implementation_digest": implementation,
                    }
                ],
            }
        )
        operations.append(
            {
                "operation_id": operation,
                "owner": pack_id,
                "source_kind": "canonical_v4_contract",
                "effect_ceiling": entry["effect_ceiling"],
                "contract_reference": contract,
                "provider_id": function,
            }
        )
        providers.append(
            {
                "provider_id": function,
                "owner": pack_id,
                "contract_reference": contract,
                "operations": [operation],
            }
        )
    runtime_artifacts = [
        {
            "path": path,
            "digest": sha256_file(PACK / path),
            "kind": kind,
            **({"index_role": index_role} if index_role is not None else {}),
        }
        for path, kind, index_role in [
            ("runtime/host.py", "executable", None),
            ("runtime/side_chat.py", "sidecar", None),
            ("pack-source.v1.json", "sidecar", None),
            (
                "frontend/contributions/side-chat.json",
                "sidecar",
                "sidecar",
            ),
        ]
    ]
    manifest_artifacts = [
        {
            key: value
            for key, value in artifact.items()
            if key != "index_role"
        }
        for artifact in runtime_artifacts
    ]
    requirements = {
        "pack_dependencies": {},
        "contract_dependencies": [
            {
                "contract_id": item["contract_id"],
                "version_range": item["version_range"],
                "cardinality": "one",
                "optional": False,
                "operations": [item["operation_id"]],
            }
            for item in source["public_dependencies"]
        ],
        "capabilities": [
            *source["public_capabilities"],
            *[entry["capability"] for entry in source["providers"]],
        ],
        "network": source["network"],
        "secrets": [],
        "execution_boundary": "host_brokered",
        "approval_policy": "capability_gated",
        "workspace_boundary": "host_brokered",
    }
    catalog = {
        "catalog_api_version": "io.tobkiri.pack-contract-catalog.v4",
        "pack_id": pack_id,
        "source_identity": identity,
        "contracts": contracts,
    }
    executable = {
        "catalog_api_version": "io.tobkiri.executable-catalog.v4",
        "pack_id": pack_id,
        "source_identity": identity,
        "variants": variants,
    }
    executable["catalog_digest"] = canonical_digest(executable)
    manifest = {
        "pack_api_version": "io.tobkiri.pack.v4",
        "pack": {
            "id": pack_id,
            "version": "1.0.0",
            "kind": "host_extension",
            "display_name": source["display_name"],
            "artifact_digest": canonical_digest(
                {"source": source, "assets": manifest_artifacts}
            ),
        },
        "functions": functions,
        "contracts": [
            {
                "contract_id": item["contract_id"],
                "revision_digest": item["revision_digest"],
                "operations": [item["operations"][0]["operation_id"]],
            }
            for item in contracts
        ],
        "artifacts": manifest_artifacts,
        "requirements": requirements,
        "operation_catalog": operations,
        "provider_catalog": providers,
        "integrity": {
            "source_identity": identity,
            "artifact_set_digest": canonical_digest(manifest_artifacts),
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
    integration = {
        "schema": "tobkiri.pack-integration-input.v1",
        "pack_catalog_record": {
            "pack_id": pack_id,
            "authority": "v4-authoritative",
            "display_name": source["display_name"],
            "description": "Optional independent child history with fresh parent context.",
            "version": "1.0.0",
            "kind": "host_extension",
            "dependencies": {},
            **{
                key: value
                for key, value in requirements.items()
                if key not in {"pack_dependencies", "contract_dependencies"}
            },
            "required_contracts": requirements["contract_dependencies"],
            "provided_contracts": provided,
            "runtime_artifacts": runtime_artifacts,
            "legacy_ids": [],
            "legacy_operations": [],
            "migration": {"removal_wave": 0, "sunset_at": "2027-12-31"},
            "source_evidence": [
                {
                    "path": f"ecosystem/{pack_id}/pack-source.v1.json",
                    "rule_id": "reviewed-side-chat-source",
                    "digest": sha256_file(PACK / "pack-source.v1.json"),
                }
            ],
            "source_provenance": {
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
        "ui_contributions": ["frontend/contributions/side-chat.json"],
        "guest_source_addition": "tobkiri_protocol/conversation_context.py",
        "requested_default_selection": "optional; no installed or release evidence",
        "ui_progress_dependency": {
            "contract_id": "tobkiri.resource.turn.progress.v1",
            "contract_version": "1.0.0",
            "operation_id": "rumi_turn_runtime_pack.turn-progress-resource",
            "optional": True,
            "owner": (
                "Application frontend resource capture; exact signed edge and "
                "readiness remain integration-owned"
            ),
            "required_host_effects": ["pure", "read"],
        },
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
            json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        )
