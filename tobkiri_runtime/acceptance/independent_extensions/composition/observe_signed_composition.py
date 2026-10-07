"""Real offline admission/selection observations with ephemeral test policy.

Never creates an ActivationRecord, starts a backend or changes live Host state.
The staged bundle is read-only. Opaque authority references supplied to the
data resolver are test inputs, not records in a live Authority Kernel.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core_runtime.bootstrap.profile_capture import runtime_user_data_scope
from core_runtime.external_pack_catalog_v4 import (
    admit_signed_external_pack,
    load_admitted_external_executable_catalog,
    load_external_pack_catalog,
)
from core_runtime.pack_artifact_integrity import write_host_install_record
from core_runtime.pack_authoring import PythonPackFunction, build_python_pack
from core_runtime.pack_signature import build_signed_manifest, sign_manifest
from ecosystem.defaultspack.domain.runtime_v4 import (
    dynamic_profile_edges,
    resolve_default_profile,
)
from tobkiri_host.artifact_compiler import compile_pack_root, routes_for_plan
from tobkiri_host.contracts import OperationCatalog
from tobkiri_protocol.bundle_catalog import BundledCatalog
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_file


def _files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _sign(root: Path, trust: Path, key: Ed25519PrivateKey) -> None:
    manifest = validate_file(root / "pack.v4.json", "pack")
    pack_id = manifest["pack"]["id"]
    versions = {
        contract["contract_id"]: contract["version"]
        for contract in validate_file(
            root / "contracts.v4.json",
            "pack_contract_catalog",
        )["contracts"]
    }
    signed = sign_manifest(
        build_signed_manifest(
            root,
            pack_id=pack_id,
            version=manifest["pack"]["version"],
            publisher_id="publisher.extension-test",
            core_compatibility=">=0",
            contract_versions=versions,
            requested_capabilities=[],
        ),
        key,
    )
    signed_path = root / ".tobkiri/signed-pack.json"
    signed_path.parent.mkdir(mode=0o700)
    signed_path.write_text(json.dumps(signed, sort_keys=True) + "\n")
    signed_path.chmod(0o600)
    write_host_install_record(
        trust,
        pack_id=pack_id,
        install_path=root,
        record={
            "signature_required": True,
            "publisher_id": "publisher.extension-test",
            "key_id": signed["signature"]["key_id"],
            "installed_version": manifest["pack"]["version"],
            "signed_manifest_path": ".tobkiri/signed-pack.json",
            "contract_versions": versions,
            "requested_capabilities": [],
        },
        publisher_record={
            "public_key_pem": key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode(),
            "allowed_pack_namespaces": [pack_id],
            "revoked_key_ids": [],
        },
    )


def _rename(root: Path, output: Path, pack_id: str) -> Path:
    """Re-author identity through the public producer, preserving real code."""
    manifest = validate_file(root / "pack.v4.json", "pack")
    catalog = validate_file(root / "contracts.v4.json", "pack_contract_catalog")
    executables = validate_file(root / "executables.v4.json", "executable_catalog")
    contracts = deepcopy(catalog["contracts"])
    for contract in contracts:
        contract["owner"] = pack_id
        contract["provider_semantics"]["provider_id"] = pack_id + ".provider"
        contract["revision_digest"] = canonical_digest(
            {
                field: value
                for field, value in contract.items()
                if field not in {"revision_digest", "provenance"}
            }
        )
    functions = []
    for number, variant in enumerate(executables["variants"]):
        contract_id = variant["operations"][0]["contract_id"]
        functions.append(
            PythonPackFunction(
                pack_id + f".function{number}",
                contract_id,
                tuple(op["operation_id"] for op in variant["operations"]),
                variant["implementation_path"],
                (root / variant["implementation_path"]).read_bytes(),
            )
        )
    return build_python_pack(
        output,
        pack_id=pack_id,
        version=manifest["pack"]["version"],
        display_name="Renamed independent component",
        contracts=contracts,
        functions=functions,
    )


def observe(roots: list[Path], bundle_root: Path, artifact_root: Path) -> dict[str, Any]:
    """Observe genuine offline boundaries and preserve all original Pack bytes."""
    if len(roots) != 2:
        raise ValueError("this independent composition matrix requires two Pack roots")
    before = {str(root): _files(root) for root in roots}
    observations: list[dict[str, Any]] = []
    expected_denials = {
        "selection.unapproved": ("ProfileResolutionDenied", "Pack artifact is not approved"),
        "selection.unknown-contract": ("ProfileResolutionDenied", "found 0"),
        "selection.duplicate-id": (
            "ProfileResolutionDenied",
            "additional Pack selection contains a duplicate",
        ),
        "selection.conflicting-function": ("ProfileResolutionDenied", "found 2"),
        "unsigned.admission": ("ExternalPackCatalogDenied", "signed-pack.json"),
        "signed.source-tamper": ("PackSignatureError", "Pack file manifest mismatch"),
        "signed.signature-tamper": ("PackSignatureError", "Pack signature mismatch"),
        "catalog.conflicting-contract": ("ResolutionError", "ambiguous operation route"),
    }

    def record(name: str, expected: str, operation: Callable[[], Any]) -> Any:
        try:
            result = operation()
            observations.append(
                {"case": name, "expected": expected, "actual": "allow", "pass": expected == "allow"}
            )
            return result
        except Exception as error:
            error_type, reason = expected_denials.get(name, ("", ""))
            observations.append(
                {
                    "case": name,
                    "expected": expected,
                    "actual": "deny",
                    "pass": expected == "deny"
                    and type(error).__name__ == error_type
                    and reason in str(error),
                    "error_type": type(error).__name__,
                    "reason": str(error),
                }
            )
            return None

    with tempfile.TemporaryDirectory(prefix="tobkiri-extension-test-") as value:
        # macOS exposes the system temp directory through /var -> /private/var.
        # The Host-owned policy writer requires its actual no-symlink root.
        temp = Path(value).resolve(strict=True)
        trust = temp / "publisher-trust.json"
        key = Ed25519PrivateKey.generate()
        test_roots: list[Path] = []
        with runtime_user_data_scope(temp / "user-data"):
            for number, original in enumerate(roots):
                copied = temp / f"author{number}"
                shutil.copytree(original, copied)
                compiled = record(
                    f"compile.author{number}",
                    "allow",
                    lambda copied=copied: compile_pack_root(copied),
                )
                assert compiled is not None
                _sign(copied, trust, key)
                record(
                    f"signed.admit.author{number}",
                    "allow",
                    lambda copied=copied: admit_signed_external_pack(
                        copied,
                        trust_store_path=trust,
                    ),
                )
                record(
                    f"signed.admit.idempotent.author{number}",
                    "allow",
                    lambda copied=copied: admit_signed_external_pack(
                        copied,
                        trust_store_path=trust,
                    ),
                )
                test_roots.append(copied)
            snapshot = load_external_pack_catalog()
            baseline = BundledCatalog.load(bundle_root, artifact_root=artifact_root)
            manifests = dict(baseline.packs)
            executables = dict(baseline.executable_catalogs)
            for pack_id, root in snapshot.roots.items():
                manifests[pack_id] = validate_file(root / "pack.v4.json", "pack")
                executables[pack_id] = load_admitted_external_executable_catalog(
                    pack_id,
                    manifests[pack_id],
                )
            catalog = replace(baseline, packs=manifests, executable_catalogs=executables)
            pack_ids = tuple(sorted(snapshot.roots))

            def select(
                selected: tuple[str, ...],
                candidate: BundledCatalog = catalog,
                unapproved: str | None = None,
                unknown: bool = False,
            ) -> Any:
                source = candidate.profiles["defaults"]
                edges = list(source["requested_edges"])
                if unknown:
                    source = deepcopy(source)
                    extra = deepcopy(source["requested_edges"][0])
                    extra["contract_id"] = "unknown.extension.v1"
                    extra["operation_id"] = "unknown.operation"
                    source["requested_edges"].append(extra)
                    candidate = replace(candidate, profiles={"defaults": source})
                    edges = list(source["requested_edges"])
                edges.extend(dynamic_profile_edges(candidate, "defaults", selected))
                authority_bindings = {
                    "|".join(
                        str(edge[field])
                        for field in (
                            "caller_function_id",
                            "target_provider_id",
                            "contract_id",
                            "operation_id",
                        )
                    ): "authority-ref:" + canonical_digest(edge).removeprefix("sha256:")
                    for edge in edges
                }
                approved = {
                    item["pack"]["artifact_digest"]
                    for identity, item in candidate.packs.items()
                    if identity != unapproved
                }
                return resolve_default_profile(
                    candidate,
                    "defaults",
                    approved_artifact_digests=approved,
                    authority_snapshot_digest=canonical_digest({"test": "offline-input-only"}),
                    authority_bindings=authority_bindings,
                    security_epoch=1,
                    additional_pack_ids=selected,
                )

            for selected in ((), (pack_ids[0],), (pack_ids[1],), pack_ids):
                resolved = record(
                    "selection." + "+".join(selected or ("unselected",)),
                    "allow",
                    lambda selected=selected: select(selected),
                )
                if resolved is not None:
                    actual = {
                        binding["pack_id"]
                        for binding in resolved.plan["bindings"]
                        if binding["pack_id"] in pack_ids
                    }
                    observations[-1]["selected_bindings"] = sorted(actual)
                    observations[-1]["pass"] &= actual == set(selected)
                    compiled = [
                        compile_pack_root(snapshot.roots[identity]) for identity in selected
                    ]
                    external_plan = {
                        "bindings": [
                            binding
                            for binding in resolved.plan["bindings"]
                            if binding["pack_id"] in pack_ids
                        ]
                    }
                    record(
                        "routes." + "+".join(selected or ("unselected",)),
                        "allow",
                        lambda: routes_for_plan(external_plan, compiled),
                    )
                    record(
                        "catalog." + "+".join(selected or ("unselected",)),
                        "allow",
                        lambda: OperationCatalog(
                            [item.artifact for item in compiled],
                            routes_for_plan(external_plan, compiled),
                        ),
                    )
            record("selection.unapproved", "deny", lambda: select(pack_ids, unapproved=pack_ids[0]))
            record("selection.unknown-contract", "deny", lambda: select(pack_ids, unknown=True))
            record("selection.duplicate-id", "deny", lambda: select((pack_ids[0], pack_ids[0])))
            renamed = _rename(roots[0], temp / "renamed", "renamed.extension.component")
            _sign(renamed, trust, key)
            record(
                "renamed.signed-admit",
                "allow",
                lambda: admit_signed_external_pack(renamed, trust_store_path=trust),
            )
            renamed_snapshot = load_external_pack_catalog()
            renamed_root = renamed_snapshot.roots["renamed.extension.component"]
            renamed_manifest = validate_file(renamed_root / "pack.v4.json", "pack")
            renamed_catalog = replace(
                catalog,
                packs={
                    **catalog.packs,
                    "renamed.extension.component": renamed_manifest,
                },
                executable_catalogs={
                    **catalog.executable_catalogs,
                    "renamed.extension.component": load_admitted_external_executable_catalog(
                        "renamed.extension.component",
                        renamed_manifest,
                    ),
                },
            )
            record(
                "renamed.selection",
                "allow",
                lambda: select(
                    ("renamed.extension.component", pack_ids[1]),
                    renamed_catalog,
                ),
            )
            conflicting_plan = select((*pack_ids, "renamed.extension.component"), renamed_catalog)
            conflicting_compiled = [
                compile_pack_root(renamed_snapshot.roots[identity])
                for identity in (*pack_ids, "renamed.extension.component")
            ]
            conflicting_external_plan = {
                "bindings": [
                    binding
                    for binding in conflicting_plan.plan["bindings"]
                    if binding["pack_id"] in renamed_snapshot.roots
                ]
            }
            record(
                "catalog.conflicting-contract",
                "deny",
                lambda: OperationCatalog(
                    [item.artifact for item in conflicting_compiled],
                    routes_for_plan(conflicting_external_plan, conflicting_compiled),
                ),
            )
            # Reusing the original Function ID for a second artifact cannot
            # create two selected immutable targets for the same edge.
            conflict_manifest = deepcopy(renamed_manifest)
            conflict_manifest["functions"][0]["id"] = catalog.packs[pack_ids[0]]["functions"][0][
                "id"
            ]
            conflict_catalog = replace(
                renamed_catalog,
                packs={
                    **renamed_catalog.packs,
                    "renamed.extension.component": conflict_manifest,
                },
            )
            record(
                "selection.conflicting-function",
                "deny",
                lambda: select(
                    (*pack_ids, "renamed.extension.component"),
                    conflict_catalog,
                ),
            )
            unsigned = temp / "unsigned"
            shutil.copytree(roots[0], unsigned)
            record(
                "unsigned.admission",
                "deny",
                lambda: admit_signed_external_pack(
                    unsigned,
                    trust_store_path=trust,
                ),
            )
            tampered = test_roots[0]
            variant = validate_file(tampered / "executables.v4.json", "executable_catalog")[
                "variants"
            ][0]
            implementation = tampered / variant["implementation_path"]
            implementation.write_bytes(
                implementation.read_bytes() + b"\n# tampered after signing\n"
            )
            record(
                "signed.source-tamper",
                "deny",
                lambda: admit_signed_external_pack(
                    tampered,
                    trust_store_path=trust,
                ),
            )
            signature_path = test_roots[1] / ".tobkiri/signed-pack.json"
            signed = json.loads(signature_path.read_text())
            value = signed["signature"]["value"]
            signed["signature"]["value"] = ("A" if value[0] != "A" else "B") + value[1:]
            signature_path.write_text(json.dumps(signed, sort_keys=True) + "\n")
            record(
                "signed.signature-tamper",
                "deny",
                lambda: admit_signed_external_pack(
                    test_roots[1],
                    trust_store_path=trust,
                ),
            )
    preserved = before == {str(root): _files(root) for root in roots}
    return {
        "schema": "tobkiri.independent-extension-offline-boundaries.v1",
        "original_inputs_preserved": preserved,
        "offline": True,
        "test_policy_only": True,
        "authority_references": "synthetic offline resolver inputs, never stored or activated",
        "VM_Broker_API_execution": False,
        "bundle_lock_sha256": hashlib.sha256(
            (bundle_root / "bundle.lock.json").read_bytes()
        ).hexdigest(),
        "cases": observations,
        "all_expected": preserved and all(item["pass"] for item in observations),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", action="append", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = observe(args.pack, args.bundle_root, args.artifact_root)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"all_expected": result["all_expected"], "cases": result["cases"]}))
