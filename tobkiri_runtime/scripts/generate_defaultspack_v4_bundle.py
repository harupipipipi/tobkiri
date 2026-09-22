#!/usr/bin/env python3
"""Normalize the finite default Profile bundle to canonical Pack v4 records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Mapping

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

# Keep the documented ``python scripts/generate_*.py`` entry point independent
# of the caller's working directory.  The runtime package is the generator's
# canonical import root; relying on the script directory or ambient PYTHONPATH
# makes a fresh checkout fail before it can validate the bundle.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tobkiri_protocol.canonical import canonical_digest  # noqa: E402
from tobkiri_protocol.defaultspack_bundle_order import (  # noqa: E402
    canonical_defaultspack_bundle_entries,
)
from tobkiri_protocol.profile_scope import (  # noqa: E402
    normalize_requested_scope_template,
)
from tobkiri_protocol.provenance import (  # noqa: E402
    informational_source_commit,
    normative_generated_provenance,
)
from tobkiri_protocol.validation import validate_document  # noqa: E402
from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog  # noqa: E402


BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"
PACKS = BUNDLE / "packs"
PACK_SOURCE_CATALOG = ROOT / "schemas" / "pack_v4_catalog.v1.json"
CANONICAL_PACK_FILES = {
    "defaultspack.pack.v4.json": ROOT / "ecosystem" / "defaultspack" / "pack.v4.json",
    "rumi-file-inspect.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_file_inspect_pack" / "pack.v4.json"
    ),
    "rumi-host-authority-bridge.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_host_authority_bridge_pack" / "pack.v4.json"
    ),
    "rumi-workspace-mount.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_workspace_mount_pack" / "pack.v4.json"
    ),
    "tobkiri-host-pack-control.pack.v4.json": (
        ROOT / "ecosystem" / "tobkiri_host_pack_control" / "pack.v4.json"
    ),
    "rumi_ai_gateway_pack.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_ai_gateway_pack" / "pack.v4.json"
    ),
    "rumi_model_catalog_pack.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_model_catalog_pack" / "pack.v4.json"
    ),
    "rumi_model_registry_pack.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_model_registry_pack" / "pack.v4.json"
    ),
    "rumi_ai_pipeline_pack.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_ai_pipeline_pack" / "pack.v4.json"
    ),
    "rumi_provider_adapters_pack.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_provider_adapters_pack" / "pack.v4.json"
    ),
    "rumi_ai_routing_pack.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_ai_routing_pack" / "pack.v4.json"
    ),
    "rumi_ai_stream_pack.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_ai_stream_pack" / "pack.v4.json"
    ),
    "rumi_ai_tool_bridge_pack.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_ai_tool_bridge_pack" / "pack.v4.json"
    ),
    "rumi_ai_usage_pack.pack.v4.json": (ROOT / "ecosystem" / "rumi_ai_usage_pack" / "pack.v4.json"),
    "rumi_provider_registry_pack.pack.v4.json": (
        ROOT / "ecosystem" / "rumi_provider_registry_pack" / "pack.v4.json"
    ),
}
TAURI_ROLE_PACKS = {
    "runtime.tauri.application.default.pack.v4.json": {
        "pack_id": "runtime.tauri.application.default",
        "display_name": "Tobkiri Tauri Application Runtime",
        "kind": "application",
        "contract_id": "runtime.tauri.application.v1",
        "operation_id": "launch",
        "role": "brokered",
        "isolation": "dedicated_process",
    },
    "dev.tauri.toolchain.default.pack.v4.json": {
        "pack_id": "dev.tauri.toolchain.default",
        "display_name": "Tobkiri Tauri Development Toolchain",
        "kind": "host_extension",
        "contract_id": "dev.tauri.toolchain.v1",
        "operation_id": "build",
        "role": "host_capability_provider",
        "isolation": "dedicated_process",
    },
}
DEFAULTSPACK_DESKTOP_ENTRYPOINT = "defaultspack/desktop_app.py"
DEFAULTSPACK_FRONTEND_CONTRACT_MAP = "defaultspack/frontend_contract_map.v4.json"


def _canonical_optional_host_extension_ids() -> tuple[str, ...]:
    """Return the deterministic Host Extension catalog selection and closure."""

    payload = json.loads(PACK_SOURCE_CATALOG.read_text(encoding="utf-8"))
    records = payload.get("packs")
    if not isinstance(records, list):
        raise ValueError("canonical Pack source catalog has no packs")
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("canonical Pack source catalog contains a malformed record")
        pack_id = record.get("pack_id")
        if not isinstance(pack_id, str) or not pack_id or pack_id in by_id:
            raise ValueError(f"canonical Pack source catalog has an invalid Pack ID: {pack_id!r}")
        by_id[pack_id] = record

    selected = {
        pack_id
        for pack_id, record in by_id.items()
        if record.get("kind") == "host_extension"
    }
    pending = sorted(selected)
    while pending:
        pack_id = pending.pop(0)
        record = by_id[pack_id]
        dependencies = record.get("dependencies")
        if not isinstance(dependencies, dict):
            raise ValueError(f"Pack dependencies are malformed: {pack_id}")
        for dependency_id in sorted(dependencies):
            if dependency_id not in by_id:
                raise ValueError(
                    f"canonical Pack source catalog has an unknown dependency: "
                    f"{pack_id} -> {dependency_id}"
                )
            if dependency_id not in selected:
                selected.add(dependency_id)
                pending.append(dependency_id)
        pending.sort()
    return tuple(sorted(selected))


def _canonical_pack_sources() -> dict[Path, Path]:
    """Select Profile and catalog-referenceable Pack sources without discovery drift."""

    sources = {path: path for path in PACKS.glob("*.pack.v4.json")}
    sources.update({PACKS / name: source for name, source in CANONICAL_PACK_FILES.items()})
    canonical_names = {
        source.parent.name: output.name
        for output, source in sources.items()
        if source.parent.name
    }
    for pack_id in _canonical_optional_host_extension_ids():
        output_name = canonical_names.get(pack_id, f"{pack_id}.pack.v4.json")
        sources[PACKS / output_name] = ROOT / "ecosystem" / pack_id / "pack.v4.json"
    return sources


def _executable_catalog_source(
    source: Path,
    document: Mapping[str, Any],
) -> Path | None:
    """Resolve the one canonical executable sidecar for a runnable Pack.

    Canonical source Packs keep the sidecar beside ``pack.v4.json`` while the
    already-rendered v4 bundle names it ``<pack_id>.executables.v4.json``.
    Supporting both explicit layouts keeps the output deterministic and avoids
    silently dropping a required executable catalog during bundle rendering.
    """

    kind = str(document["pack"]["kind"])
    if kind in {"base", "shell"}:
        return None
    candidates = (
        source.parent / "executables.v4.json",
        source.with_name(f"{document['pack']['id']}.executables.v4.json"),
    )
    existing = tuple(candidate for candidate in candidates if candidate.is_file())
    if len(existing) != 1:
        raise ValueError(
            "canonical runnable Pack must have exactly one executable catalog: "
            f"{document['pack']['id']}"
        )
    return existing[0]


def _pretty(document: dict[str, Any]) -> bytes:
    return json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"


def _requirements(kind: str, existing: Any) -> dict[str, Any]:
    """Preserve Pack-owned requirements without branching on a Pack identity."""

    if isinstance(existing, dict):
        return dict(existing)
    capabilities: list[str] = []
    return {
        "pack_dependencies": {},
        "contract_dependencies": [],
        "capabilities": capabilities,
        "network": {"allowed_domains": [], "allowed_ports": []},
        "secrets": [],
        "execution_boundary": (
            "declarative_only"
            if kind == "base"
            else "host_brokered"
            if kind == "shell"
            else "sandbox"
        ),
        "approval_policy": "capability_gated" if capabilities else "none",
        "workspace_boundary": "host_brokered" if capabilities else "pack_local",
    }


def _normalize_pack(document: dict[str, Any]) -> dict[str, Any]:
    pack_id = str(document["pack"]["id"])
    owner = pack_id
    contracts = {item["revision_digest"]: item for item in document["contracts"]}
    operations: list[dict[str, Any]] = []
    providers: list[dict[str, Any]] = []
    for function in document["functions"]:
        contract = contracts[function["contract_revision_digest"]]
        providers.append(
            {
                "provider_id": function["id"],
                "owner": owner,
                "contract_reference": contract["contract_id"],
                "operations": list(function["operations"]),
            }
        )
        for operation_id in function["operations"]:
            operations.append(
                {
                    "operation_id": operation_id,
                    "owner": owner,
                    "contract_reference": contract["contract_id"],
                    "provider_id": function["id"],
                    "source_kind": "canonical_v4_contract",
                    "effect_ceiling": [],
                }
            )
    document["requirements"] = _requirements(document["pack"]["kind"], document.get("requirements"))
    document["operation_catalog"] = operations
    document["provider_catalog"] = providers
    identity_source = {
        key: document[key]
        for key in (
            "pack_api_version",
            "pack",
            "functions",
            "contracts",
            "artifacts",
            "requirements",
            "operation_catalog",
            "provider_catalog",
            "provenance",
            "migration",
        )
    }
    document["integrity"] = {
        "source_identity": canonical_digest(identity_source),
        "artifact_set_digest": canonical_digest(document["artifacts"]),
        "contract_catalog_digest": canonical_digest(document["contracts"]),
    }
    return validate_document(document, "pack")


def _generated_provenance(
    document: dict[str, Any],
    source_path: str,
    source_commit: str,
    *,
    generator_path: Path | None = None,
) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in document.items()
        if key not in {"provenance", "integrity", "definition_revision"}
    }
    return normative_generated_provenance(
        source_path=source_path,
        payload=payload,
        repository_commit_value=source_commit,
        generator="defaultspack-v4-core",
        generator_version="2.0.0",
        generator_path=(generator_path or Path(__file__)).relative_to(ROOT.parent).as_posix(),
        generator_payload=(generator_path or Path(__file__)).read_bytes(),
    )


def _tauri_role_pack(spec: dict[str, str], source_commit: str) -> dict[str, Any]:
    """Generate one canonical Tauri role without projecting shell authority."""
    pack_id = spec["pack_id"]
    contract_id = spec["contract_id"]
    operation_id = spec["operation_id"]
    contract_digest = canonical_digest({"contract_id": contract_id, "operations": [operation_id]})
    if pack_id == "runtime.tauri.application.default":
        implementation_digest = canonical_digest(
            {"pack_id": pack_id, "availability": "build_required"}
        )
        contract_map = ROOT / "ecosystem" / "defaultspack" / DEFAULTSPACK_FRONTEND_CONTRACT_MAP
        artifacts = [
            {
                "path": DEFAULTSPACK_FRONTEND_CONTRACT_MAP,
                "digest": "sha256:" + hashlib.sha256(contract_map.read_bytes()).hexdigest(),
                "kind": "asset",
                "platform": "all",
            }
        ]
        artifact_digest = canonical_digest({"pack_id": pack_id, "availability": "build_required"})
    else:
        implementation_digest = canonical_digest(
            {"pack_id": pack_id, "contract": contract_digest, "operation": operation_id}
        )
        artifacts = [
            {
                "path": f"artifacts/{pack_id}",
                "digest": implementation_digest,
                "kind": "executable",
                "platform": "host",
            }
        ]
        artifact_digest = canonical_digest(
            {
                "pack_id": pack_id,
                "implementation": implementation_digest,
                "prebuilt": True,
            }
        )
    source_path = f"ecosystem/defaultspack/v4/packs/{pack_id}.pack.v4.json"
    document = {
        "pack_api_version": "io.tobkiri.pack.v4",
        "pack": {
            "id": pack_id,
            "version": "1.0.0",
            "kind": spec["kind"],
            "artifact_digest": artifact_digest,
            "display_name": spec["display_name"],
        },
        "functions": [
            {
                "id": pack_id,
                "implementation_digest": implementation_digest,
                "contract_revision_digest": contract_digest,
                "operations": [operation_id],
                "role": spec["role"],
                "isolation": spec["isolation"],
            }
        ],
        "contracts": [
            {
                "contract_id": contract_id,
                "revision_digest": contract_digest,
                "operations": [operation_id],
            }
        ],
        "artifacts": artifacts,
        "provenance": {},
        "migration": {
            "compatibility": "none",
            "legacy_ids": [],
            "removal_wave": 0,
            "sunset_at": "2026-08-05",
        },
    }
    document["provenance"] = _generated_provenance(document, source_path, source_commit)
    normalized = _normalize_pack(document)
    if pack_id.startswith("dev.tauri."):
        normalized["requirements"].update(
            {
                "execution_boundary": "host_brokered",
                "approval_policy": "always",
                "workspace_boundary": "host_brokered",
            }
        )
        normalized["integrity"] = {
            "source_identity": canonical_digest(
                {key: value for key, value in normalized.items() if key != "integrity"}
            ),
            "artifact_set_digest": canonical_digest(normalized["artifacts"]),
            "contract_catalog_digest": canonical_digest(normalized["contracts"]),
        }
        normalized = validate_document(normalized, "pack")
    return normalized


def _unavailable_shell_pack(document: dict[str, Any], source_commit: str) -> dict[str, Any]:
    """Remove fabricated launch bytes from a source-only Shell Pack."""

    pack_id = str(document["pack"]["id"])
    unavailable_digest = canonical_digest({"pack_id": pack_id, "availability": "build_required"})
    document["pack"]["artifact_digest"] = unavailable_digest
    document["artifacts"] = []
    for function in document["functions"]:
        function["implementation_digest"] = canonical_digest(
            {
                "pack_id": pack_id,
                "function_id": function["id"],
                "availability": "build_required",
            }
        )
    source_path = f"ecosystem/defaultspack/v4/packs/{pack_id}.pack.v4.json"
    document["provenance"] = _generated_provenance(document, source_path, source_commit)
    return _normalize_pack(document)


def _declarative_base_pack(document: dict[str, Any], source_commit: str) -> dict[str, Any]:
    pack_id = str(document["pack"]["id"])
    document["pack"]["artifact_digest"] = canonical_digest(
        {"pack_id": pack_id, "declarative_definition": True}
    )
    document["artifacts"] = []
    document["provenance"] = _generated_provenance(
        document,
        f"ecosystem/defaultspack/v4/packs/{pack_id}.pack.v4.json",
        source_commit,
    )
    return _normalize_pack(document)


def _normalize_base(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("base_api_version") == "io.tobkiri.base.v4":
        document["definition_revision"] = canonical_digest(
            {key: value for key, value in document.items() if key != "definition_revision"}
        )
        return validate_document(document, "base")
    normalized = {
        "base_api_version": "io.tobkiri.base.v4",
        "pack_id": document["pack_id"],
        "artifact_digest": document["artifact_digest"],
        "definition_revision": "sha256:" + "0" * 64,
        "capability_foundation": {
            "provided_contracts": list(document.get("backend_contracts", [])),
            "required_contracts": [],
        },
        "policy_foundation": {
            "policy_digest": canonical_digest(
                {"network_default": "deny", "host_effect_default": "lease_only"}
            ),
            "network_default": "deny",
            "host_effect_default": "lease_only",
        },
        "dependencies": [],
        "shell_requirements": {
            "mode": "interactive",
            "presentation_families": list(document["presentation_families"]),
            "required_capabilities": list(document["required_shell_capabilities"]),
        },
        "state_owners": list(document["state_owners"]),
        "provenance": document["provenance"],
    }
    normalized["definition_revision"] = canonical_digest(
        {key: value for key, value in normalized.items() if key != "definition_revision"}
    )
    return validate_document(normalized, "base")


def _normalize_shell(document: dict[str, Any]) -> dict[str, Any]:
    presentation = document.get("presentation")
    if isinstance(presentation, dict):
        family = presentation["family"]
        kind = presentation["kind"]
        technology = presentation["technology"]
        capabilities = list(presentation["capabilities"])
        contribution_contracts = list(presentation["consumes_contribution_contracts"])
    else:
        family = "terminal"
        kind = "terminal_stdio"
        technology = "cli"
        capabilities = list(document["required_capabilities"])
        contribution_contracts = list(document["consumes_contribution_contracts"])
    target_specs: tuple[tuple[str, str, str, str], ...]
    if technology == "tauri":
        target_specs = (
            ("macos", "arm64", "Tobkiri.app", "Tobkiri.app/Contents/MacOS/tobkiri-shell"),
            ("macos", "x86_64", "Tobkiri.app", "Tobkiri.app/Contents/MacOS/tobkiri-shell"),
            ("windows", "x86_64", "tobkiri-shell.exe", "tobkiri-shell.exe"),
            ("linux", "x86_64", "Tobkiri.AppImage", "Tobkiri.AppImage"),
        )
        bundle_identity = "io.tobkiri.shell.tauri"
    else:
        target_specs = (("macos", "arm64", "bin/tobkiri-shell", "tobkiri-shell"),)
        bundle_identity = "io.tobkiri.shell.cli.default"
    build_targets = [
        {
            "artifact_id": f"{document['provider_id']}.{platform}-{architecture}",
            "platform": platform,
            "architecture": architecture,
            "artifact_ref": artifact_ref,
            "entrypoint": entrypoint,
            "bundle_identity": bundle_identity,
        }
        for platform, architecture, artifact_ref, entrypoint in target_specs
    ]
    normalized = {
        "shell_api_version": "io.tobkiri.shell.v5",
        "provider_id": document["provider_id"],
        "pack_id": document["pack_id"],
        "availability": "build_required",
        "artifact_digest": None,
        "definition_revision": "sha256:" + "0" * 64,
        "contract_id": "app.shell.v1",
        "presentation": {
            "family": family,
            "kind": kind,
            "technology": technology,
            "capabilities": capabilities,
            "consumes_contribution_contracts": contribution_contracts,
        },
        "launch": {
            "prebuilt_only": True,
            "build_targets": build_targets,
            "variants": [],
        },
        "local_auth": {
            "protocol": "io.tobkiri.local-auth.v1",
            "audience": "runtime-profile",
        },
        "provenance": document["provenance"],
    }
    normalized["definition_revision"] = canonical_digest(
        {key: value for key, value in normalized.items() if key != "definition_revision"}
    )
    return validate_document(normalized, "shell")


def _normalize_profile(document: dict[str, Any]) -> dict[str, Any]:
    document["profile_api_version"] = "io.tobkiri.profile.v5"
    document.setdefault("mode", "interactive")
    document.setdefault("catalog_revision", None)
    document["base"].setdefault("definition_revision", None)
    if document["shell"] is not None:
        document["shell"].setdefault("definition_revision", None)
        document["shell"].setdefault("executable_artifact_digest", None)
        document["shell"].setdefault("platform", "macos")
        document["shell"].setdefault("architecture", "arm64")
    return document


def _render(source_commit: str | None = None) -> dict[Path, bytes]:
    source_commit = informational_source_commit(ROOT.parent, source_commit)
    rendered: dict[Path, bytes] = {}
    pack_sources = _canonical_pack_sources()
    pack_sources.update(
        {
            PACKS / name: PACKS / name
            for name in TAURI_ROLE_PACKS
        }
    )
    for path in sorted(pack_sources):
        source = pack_sources[path]
        canonical = CANONICAL_PACK_FILES.get(path.name)
        role_spec = TAURI_ROLE_PACKS.get(path.name)
        canonical_source = canonical is not None or source.parent.parent == ROOT / "ecosystem"
        document = (
            _tauri_role_pack(role_spec, source_commit)
            if role_spec is not None
            else json.loads(source.read_text(encoding="utf-8"))
        )
        if str(document["pack"]["id"]).startswith("shell."):
            document = _unavailable_shell_pack(document, source_commit)
        elif document["pack"]["id"] == "defaults-basepack":
            document = _declarative_base_pack(document, source_commit)
        rendered[path] = _pretty(
            validate_document(document, "pack")
            if canonical_source or role_spec is not None
            else _normalize_pack(document)
        )
        if role_spec is None:
            source_catalog = _executable_catalog_source(source, document)
            if source_catalog is not None:
                catalog_path = path.with_name(
                    f"{document['pack']['id']}.executables.v4.json"
                )
                catalog = validate_document(
                    json.loads(source_catalog.read_text(encoding="utf-8")),
                    "executable_catalog",
                )
                if (
                    catalog["pack_id"] != document["pack"]["id"]
                    or catalog["source_identity"]
                    != document["integrity"]["source_identity"]
                ):
                    raise ValueError(
                        "canonical executable catalog identity is stale: "
                        f"{document['pack']['id']}"
                    )
                rendered[catalog_path] = source_catalog.read_bytes()

    base_path = BUNDLE / "defaults-basepack.base.v1.json"
    shell_paths = sorted(BUNDLE.glob("*.shell.v1.json"))
    profile_paths = sorted(BUNDLE.glob("*.profile.v4.json"))
    if not profile_paths:
        raise ValueError("defaultspack v4 bundle has no Profile definitions")
    base_source = json.loads(base_path.read_text(encoding="utf-8"))
    base_pack = json.loads(rendered[PACKS / "defaults-basepack.pack.v4.json"])
    base_source["artifact_digest"] = base_pack["pack"]["artifact_digest"]
    base_source["provenance"] = _generated_provenance(
        base_source,
        base_path.relative_to(ROOT).as_posix(),
        source_commit,
    )
    base = _normalize_base(base_source)
    shells: list[tuple[Path, dict[str, Any]]] = []
    for path in shell_paths:
        shell = _normalize_shell(json.loads(path.read_text(encoding="utf-8")))
        shell["provenance"] = _generated_provenance(
            shell, path.relative_to(ROOT).as_posix(), source_commit
        )
        shell["definition_revision"] = canonical_digest(
            {key: value for key, value in shell.items() if key != "definition_revision"}
        )
        shells.append((path, validate_document(shell, "shell")))
    for profile_path in profile_paths:
        profile = _normalize_profile(json.loads(profile_path.read_text(encoding="utf-8")))
        profile["provenance"] = _generated_provenance(
            profile,
            profile_path.relative_to(ROOT).as_posix(),
            source_commit,
        )
        for pack in profile["packs"]:
            if pack["pack_id"] == "rumi-file-inspect":
                pack["pack_id"] = "rumi_file_inspect_pack"
        if not any(
            pack["pack_id"] == "runtime.tauri.application.default" for pack in profile["packs"]
        ):
            profile["packs"].append(
                {
                    "pack_id": "runtime.tauri.application.default",
                    "artifact_digest": None,
                    "role": "application",
                }
            )
        if any(pack["pack_id"].startswith("dev.tauri.") for pack in profile["packs"]):
            raise ValueError("Development Realm Tauri toolchain cannot enter production Profile")
        for edge in profile["requested_edges"]:
            if edge["target_provider_id"] == "defaultspack.file.inspect":
                edge.update(
                    {
                        "target_provider_id": ("rumi_file_inspect_pack.file-inspect.service"),
                        "contract_id": "tobkiri.service.file.inspect.v1",
                        "operation_id": "rumi_file_inspect_pack.file-inspect",
                    }
                )
            template = edge["requested_scope_template"]
            if template and "dimensions" not in template:
                template = {
                    "dimensions": {str(key): [str(value)] for key, value in template.items()}
                }
            elif template:
                template = {
                    key: value for key, value in template.items() if key != "semantics_digest"
                }
            contract_digests = [
                str(contract["revision_digest"])
                for raw in rendered.values()
                for pack in [json.loads(raw)]
                if isinstance(pack, dict)
                for function in pack.get("functions", [])
                if function.get("id") == edge["target_provider_id"]
                for contract in pack.get("contracts", [])
                if contract.get("contract_id") == edge["contract_id"]
                and edge["operation_id"] in contract.get("operations", [])
            ]
            if len(contract_digests) != 1:
                raise ValueError(
                    "Profile edge must resolve to exactly one canonical Provider: "
                    f"{edge['caller_function_id']} -> {edge['target_provider_id']} / "
                    f"{edge['contract_id']} / {edge['operation_id']}"
                )
            edge["requested_scope_template"] = normalize_requested_scope_template(
                template,
                contract_id=edge["contract_id"],
                operation_id=edge["operation_id"],
                semantics_digest=contract_digests[0],
            )
        rendered[profile_path] = _pretty(validate_document(profile, "profile"))
    rendered[base_path] = _pretty(base)
    for shell_path, shell in shells:
        rendered[shell_path] = _pretty(shell)

    entries: list[dict[str, str]] = []
    kinds = {"packs": "pack", "base.v1": "base", "shell.v1": "shell", "profile.v4": "profile"}
    paths = [
        *sorted(path for path in rendered if path.parent == PACKS),
        base_path,
        *shell_paths,
        *profile_paths,
    ]
    for path in paths:
        raw = rendered[path] if path in rendered else path.read_bytes()
        relative = path.relative_to(BUNDLE).as_posix()
        kind = (
            "executable_catalog"
            if relative.endswith(".executables.v4.json")
            else next(value for marker, value in kinds.items() if marker in relative)
        )
        entries.append(
            {
                "path": relative,
                "kind": kind,
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            }
        )
    entries = canonical_defaultspack_bundle_entries(entries)
    lock = {"schema": "io.tobkiri.defaultspack-bundle-lock.v1", "entries": entries}
    rendered[BUNDLE / "bundle.lock.json"] = (
        json.dumps(lock, indent=2, ensure_ascii=False).encode() + b"\n"
    )
    return rendered


def _validate_catalog(catalog: BundledCatalog) -> None:
    """Reject incompatible or ambiguous generated composition inputs."""

    for pack_id, manifest in catalog.packs.items():
        for dependency_id, version_range in manifest["requirements"]["pack_dependencies"].items():
            dependency = catalog.packs.get(dependency_id)
            if dependency is None:
                raise ValueError(f"Pack dependency is missing: {pack_id} -> {dependency_id}")
            try:
                compatible = Version(dependency["pack"]["version"]) in SpecifierSet(
                    version_range.replace(" ", ",")
                )
            except (InvalidSpecifier, InvalidVersion) as error:
                raise ValueError(
                    f"Pack dependency constraint is invalid: {pack_id} -> {dependency_id}"
                ) from error
            if not compatible:
                raise ValueError(f"Pack dependency is incompatible: {pack_id} -> {dependency_id}")

    for profile_id, profile in catalog.profiles.items():
        base_id = str(profile["base"]["pack_id"])
        base = catalog.bases.get(base_id)
        base_manifest = catalog.packs.get(base_id)
        if base is None or base_manifest is None or base_manifest["pack"]["kind"] != "base":
            raise ValueError(f"Profile Base is missing or invalid: {profile_id} -> {base_id}")
        if base["artifact_digest"] != base_manifest["pack"]["artifact_digest"]:
            raise ValueError(f"Profile Base artifact is stale: {profile_id} -> {base_id}")
        for dependency in base["dependencies"]:
            dependency_manifest = catalog.packs.get(str(dependency["pack_id"]))
            if (
                dependency_manifest is None
                or dependency["artifact_digest"] != (dependency_manifest["pack"]["artifact_digest"])
            ):
                raise ValueError(
                    f"Profile Base dependency is stale: {profile_id} -> {dependency['pack_id']}"
                )

        shell_request = profile.get("shell")
        if not isinstance(shell_request, dict):
            raise ValueError(f"Defaults Profile requires an exact Shell: {profile_id}")
        provider_id = str(shell_request["provider_id"])
        shell = catalog.shells.get(provider_id)
        if (
            shell is None
            or shell_request["pack_id"] != shell["pack_id"]
            or shell_request["contract_id"] != shell["contract_id"]
        ):
            raise ValueError(f"Profile Shell binding is stale: {profile_id} -> {provider_id}")
        shell_manifest = catalog.packs.get(str(shell["pack_id"]))
        if shell_manifest is None or shell_manifest["pack"]["kind"] != "shell":
            raise ValueError(f"Profile Shell Pack is invalid: {profile_id} -> {provider_id}")
        targets = [
            target
            for target in shell["launch"]["build_targets"]
            if target["platform"] == shell_request["platform"]
            and target["architecture"] == shell_request["architecture"]
        ]
        if len(targets) != 1:
            raise ValueError(f"Profile Shell target is unavailable: {profile_id}")
        requirements = base["shell_requirements"]
        if (
            profile["mode"] != requirements["mode"]
            or shell["presentation"]["family"] not in requirements["presentation_families"]
            or not set(requirements["required_capabilities"]).issubset(
                set(shell["presentation"]["capabilities"])
            )
        ):
            raise ValueError(f"Profile Shell is incompatible with Base: {profile_id}")

        pack_rows = list(profile["packs"])
        pack_ids = [str(row["pack_id"]) for row in pack_rows]
        if len(pack_ids) != len(set(pack_ids)):
            raise ValueError(f"Profile contains duplicate Packs: {profile_id}")
        applications = [row for row in pack_rows if row.get("role") == "application"]
        if len(applications) != 1:
            raise ValueError(f"Profile Application binding is ambiguous: {profile_id}")
        for row in pack_rows:
            row_manifest = catalog.packs.get(str(row["pack_id"]))
            if row_manifest is None:
                raise ValueError(f"Profile Pack is missing: {profile_id} -> {row['pack_id']}")
            if row["artifact_digest"] not in {
                None,
                row_manifest["pack"]["artifact_digest"],
            }:
                raise ValueError(
                    f"Profile Pack artifact is stale: {profile_id} -> {row['pack_id']}"
                )
        application_id = str(applications[0]["pack_id"])
        if catalog.packs[application_id]["pack"]["kind"] != "application":
            raise ValueError(f"Profile Application Pack is invalid: {profile_id}")
        if any(pack_id.startswith("dev.tauri.") for pack_id in pack_ids):
            raise ValueError("Development Realm Tauri toolchain cannot enter production Profile")

        edge_keys = [
            (
                str(edge["caller_function_id"]),
                str(edge["target_provider_id"]),
                str(edge["contract_id"]),
                str(edge["operation_id"]),
            )
            for edge in profile["requested_edges"]
        ]
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError(f"Profile contains duplicate requested edges: {profile_id}")


def _publish(
    rendered: dict[Path, bytes],
    *,
    fault: Any | None = None,
) -> None:
    """Validate and publish the complete bundle as one rollback-safe transaction."""

    if BUNDLE.is_symlink() or not BUNDLE.is_dir():
        raise ValueError("defaultspack v4 bundle root must be a real directory")
    expected_paths = {path.relative_to(BUNDLE) for path in rendered}
    if Path("bundle.lock.json") not in expected_paths:
        raise ValueError("rendered defaultspack bundle has no lock")
    for relative in expected_paths:
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"rendered path escapes the bundle: {relative}")
        current = BUNDLE
        for part in relative.parts:
            current /= part
            if current.exists() and current.is_symlink():
                raise ValueError(f"rendered path contains a symlink: {relative}")

    stage = Path(tempfile.mkdtemp(prefix=".defaultspack-v4-stage-", dir=BUNDLE.parent))
    backup = Path(tempfile.mkdtemp(prefix=".defaultspack-v4-backup-", dir=BUNDLE.parent))
    backup.rmdir()
    moved_original = False
    try:
        for path, raw in sorted(rendered.items(), key=lambda item: item[0].as_posix()):
            target = stage / path.relative_to(BUNDLE)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        _validate_catalog(BundledCatalog.load(stage))
        if fault is not None:
            fault("before_publish")
        os.replace(BUNDLE, backup)
        moved_original = True
        if fault is not None:
            fault("after_backup")
        os.replace(stage, BUNDLE)
        if fault is not None:
            fault("after_publish")
        shutil.rmtree(backup)
        moved_original = False
    except BaseException:
        if moved_original and backup.exists():
            if BUNDLE.exists():
                failed = BUNDLE.parent / f".{BUNDLE.name}.failed-{os.getpid()}"
                if failed.exists():
                    shutil.rmtree(failed)
                os.replace(BUNDLE, failed)
                os.replace(backup, BUNDLE)
                shutil.rmtree(failed)
            elif backup.exists():
                os.replace(backup, BUNDLE)
        moved_original = False
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if backup.exists() and not moved_original:
            shutil.rmtree(backup)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    rendered = _render(args.source_commit)
    stale = [
        path for path, raw in rendered.items() if not path.exists() or path.read_bytes() != raw
    ]
    if args.check:
        if stale:
            for path in stale:
                print(path.relative_to(ROOT))
            return 1
        return 0
    _publish(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
