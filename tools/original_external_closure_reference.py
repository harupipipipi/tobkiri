"""Observe the pinned original closure using two independently admitted signed Packs.

Run prepare in the pinned original reference CI environment. Each output
owner is populated by the original preview/commit path. The summary carries
the observed catalog and closure read order plus the exact Pack digests.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from original_authority_reference import ORIGINAL_HEAD, write_json
from original_external_pack_catalog_reference import inventory
from original_setup_review_reference import check_original


FORMAT = "io.tobkiri.original-external-closure-reference.v1"
ROOT = "qa.closure.z-root"
DEPENDENCY = "qa.closure.a-dependency"
IDS = (DEPENDENCY, ROOT)
BUNDLE = "ecosystem/defaultspack/v4"


def publish(source_dir: Path, pack_id: str, dependencies: dict[str, str]) -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from core_runtime.native_pack_onboarding import commit_signed_pack, preview_signed_pack
    from core_runtime.pack_sdk import scaffold_pack
    from core_runtime.pack_signature import SIGNED_MANIFEST_RELATIVE, build_signed_manifest, sign_manifest
    from tobkiri_protocol.canonical import canonical_digest

    source = source_dir / pack_id
    manifest_path = scaffold_pack(source, pack_id=pack_id, display_name=pack_id, profile="minimal")
    if dependencies:
        # The scaffold gives us coherent inert metadata. Change the authored
        # dependency and refresh the index entry that authenticates its bytes.
        manifest = json.loads(manifest_path.read_bytes())
        manifest["requirements"]["pack_dependencies"] = dependencies
        write_json(manifest_path, manifest)
        index_path = source / "artifact-index.v4.json"
        index = json.loads(index_path.read_bytes())
        canonical_manifest = next(row for row in index["artifacts"] if row["path"] == manifest_path.name)
        canonical_manifest["digest"] = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        index["integrity_seal"]["signed_digest"] = canonical_digest(
            {key: value for key, value in index.items() if key != "integrity_seal"}
        )
        write_json(index_path, index)

    signer = Ed25519PrivateKey.generate()
    signed = sign_manifest(build_signed_manifest(
        source,
        pack_id=pack_id,
        version="0.1.0",
        publisher_id="publisher." + pack_id,
        core_compatibility=">=0",
        created_at="2025-01-01T00:00:00+00:00",
    ), signer)
    signature = source / SIGNED_MANIFEST_RELATIVE
    signature.parent.mkdir(mode=0o700)
    write_json(signature, signed)
    public_key = source_dir / f"{pack_id}.public.pem"
    public_key.write_bytes(signer.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    preview = preview_signed_pack(source, public_key)
    entry = commit_signed_pack(
        source, public_key,
        expected_preview_digest=preview["preview_digest"],
        trust_store_path=source_dir / "publisher-trust.json",
    )
    if (entry["state"] != "committed" or entry["pack_id"] != pack_id
            or entry["artifact_digest"] != preview["artifact_digest"]
            or entry["catalog_record"]["dependencies"] != dependencies
            or entry["catalog_record"]["authority"] != "host-signed-external-normal-v4"):
        raise AssertionError("original signed Normal Pack admission differs")
    return entry


def observe(original: Path, owner: Path) -> dict:
    runtime = original / "tobkiri_runtime"
    sys.path.insert(0, str(runtime))
    os.environ.update(TOBKIRI_USER_DATA=str(owner), RUMI_USER_DATA=str(owner))
    from core_runtime import external_pack_catalog_v4 as external, pack_control_v4 as control
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import install_defaultspack_profile_runtime

    port = install_defaultspack_profile_runtime()
    bundle_root = runtime / BUNDLE
    catalog = port.load_catalog(bundle_root)
    if set(IDS) & catalog.packs.keys():
        raise AssertionError("closure fixture identity collides with a real bundled Pack")
    owner.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(dir=owner.parent, prefix="closure-publisher-") as temp:
        source_dir = Path(temp)
        entries = {
            DEPENDENCY: publish(source_dir, DEPENDENCY, {}),
            ROOT: publish(source_dir, ROOT, {DEPENDENCY: ">=0.1.0"}),
        }

    pins = {pack_id: entries[pack_id]["artifact_digest"] for pack_id in IDS}
    if len(set(pins.values())) != len(IDS):
        raise AssertionError("independent Normal Packs must have different artifact digests")
    before = inventory(owner)
    sequence = []
    original_catalog = external._load_external_pack_catalog
    original_verify = external._verify_entry_root
    original_root = control.resolve_pack_root
    original_executable = external.load_verified_pack_executable_catalog

    def read_catalog(artifact_pins=None, **options):
        sequence.append(["catalog", dict(artifact_pins) if artifact_pins is not None else None])
        return original_catalog(artifact_pins, **options)

    def verify(pack_id, entry, root, **options):
        original_verify(pack_id, entry, root, **options)
        expected_root = external._artifact_store(owner) / entry["store_token"]
        if entry != entries[pack_id] or root != expected_root:
            raise AssertionError("original CAS verification selected another entry")
        sequence.append(["verify", pack_id])

    def resolve_root(pack_id, *, artifact_digest=None, origin=None):
        sequence.append(["root", pack_id, artifact_digest])
        return original_root(pack_id, artifact_digest=artifact_digest, origin=origin)

    def executable(pack_id, manifest, **options):
        sequence.append(["executable", pack_id, manifest["pack"]["artifact_digest"]])
        return original_executable(pack_id, manifest, **options)

    scans = [["verify", pack_id] for pack_id in IDS]

    def observe_closure(requested, artifact_pins):
        sequence.clear()
        merged, closure = control.catalog_with_admitted_pack_closure(
            catalog, requested, artifact_pins=artifact_pins,
        )
        expected = []
        for pack_id in (IDS if artifact_pins is None else (ROOT, DEPENDENCY)):
            pin = artifact_pins.get(pack_id) if artifact_pins is not None else None
            expected.extend([
                ["catalog", artifact_pins], *scans, ["root", pack_id, pin],
                ["catalog", {pack_id: pin} if pin is not None else None], *scans,
            ])
        for pack_id in IDS:
            expected.extend([
                ["executable", pack_id, pins[pack_id]],
                ["catalog", {pack_id: pins[pack_id]}], *scans, ["verify", pack_id],
            ])
        project = {
            pack_id: {
                "pack": merged.packs[pack_id],
                "executable_catalog": merged.executable_catalogs[pack_id],
            }
            for pack_id in IDS
        }
        if (closure != set(IDS) or sequence != expected
                or set(merged.packs) - catalog.packs.keys() != closure
                or set(merged.executable_catalogs) - catalog.executable_catalogs.keys() != closure
                or project[ROOT]["pack"]["requirements"]["pack_dependencies"] != {DEPENDENCY: ">=0.1.0"}
                or any(project[pack_id]["pack"]["pack"]["artifact_digest"] != pins[pack_id] for pack_id in IDS)):
            raise AssertionError(f"original admitted closure or read order differs: {sequence!r}")
        return {
            "requested": requested,
            "artifact_pins": artifact_pins,
            "closure": sorted(closure),
            "sequence": sequence.copy(),
            "project": project,
        }

    with ExitStack() as observations:
        observations.enter_context(patch.object(external, "_load_external_pack_catalog", read_catalog))
        observations.enter_context(patch.object(external, "_verify_entry_root", verify))
        observations.enter_context(patch.object(control, "resolve_pack_root", resolve_root))
        observations.enter_context(patch.object(external, "load_verified_pack_executable_catalog", executable))
        snapshot = external._load_external_pack_catalog()
        if (sequence != [["catalog", None], *scans]
                or snapshot.entries != entries
                or snapshot.records != {pack_id: entries[pack_id]["catalog_record"] for pack_id in IDS}
                or snapshot.versions != {pack_id: {pins[pack_id]: entries[pack_id]} for pack_id in IDS}):
            raise AssertionError("original ordinary external catalog verification differs")
        ordered = sequence.copy()
        pinned = observe_closure([ROOT], pins)
        bootstrap = observe_closure(list(IDS), None)

    if inventory(owner) != before or pinned["project"] != bootstrap["project"]:
        raise AssertionError("original closure reread changed the projection or owner inventory")
    return {
        "bundle_lock_sha256": hashlib.sha256((bundle_root / "bundle.lock.json").read_bytes()).hexdigest(),
        "pins": pins,
        "catalog": {"sequence": ordered, "selected": pins},
        "pinned": pinned,
        "bootstrap": bootstrap,
    }


def prepare(original: Path, output: Path) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    results = {}
    for role in ("reference", "native"):
        owner = output / f"{role}-owner"
        observation = output / f"{role}-observation.json"
        subprocess.run([
            sys.executable, "-B", str(Path(__file__).resolve()), "worker",
            "--original-root", str(original), "--owner", str(owner), "--output", str(observation),
        ], check=True, timeout=180)
        results[role] = json.loads(observation.read_bytes())
        observation.unlink()
    if results["reference"] != results["native"]:
        raise AssertionError("independently admitted closure observations differ")
    check_original(original)
    write_json(output / "summary.json", {
        "format": FORMAT,
        "original_head": ORIGINAL_HEAD,
        "runtime_dir": str(original / "tobkiri_runtime"),
        "bundle_root": str(original / "tobkiri_runtime" / BUNDLE),
        "native_user_data": str(output / "native-owner"),
        **results["native"],
        "native_comparison": False,
        "qualification": False,
        "production_enabled": False,
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "worker"))
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner", type=Path)
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    check_original(original)
    if args.command == "prepare":
        prepare(original, args.output.resolve())
    elif args.owner is None:
        parser.error("worker requires --owner")
    else:
        write_json(args.output, observe(original, args.owner.resolve()))


if __name__ == "__main__":
    main()
