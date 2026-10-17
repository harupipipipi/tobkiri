"""Stage an ordinary read-only reader without rebuilding old provider Packs.

This utility performs local artifact preparation only. It never starts Core,
reads DATA/Vault, acquires a credential or issues an HTTP/model request.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
from typing import Any

HERE = Path(__file__).resolve().parent
PACKS = HERE.parent
REPO = PACKS.parent
SOURCE = HERE / "local.openrouter-generation-reader"
sys.path.insert(0, str(PACKS))
import build

PACK_ID = "local.openrouter-generation-reader"
VIEW = PACK_ID + ".generation-reader@1.0"
ORIGIN = "https://openrouter.ai"
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_MEMBER = 128 * 1024 * 1024


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def reader_permissions() -> dict[str, Any]:
    permissions = build.permission_set(network=[ORIGIN])
    permissions["secrets"] = [
        {"name": "api-key", "reveal": False, "optional": False}
    ]
    # No host IO, storage, filesystem, process or credential-reveal privilege.
    return permissions


def stage(target_dir: Path, destination: Path,
          build_profile: str = "release") -> dict[str, Any]:
    """Bind the selected built component; never fall back or overwrite a stage."""
    artifact = build.component_artifact(
        target_dir, "tobkiri_pack_openrouter_generation_reader.wasm", build_profile,
    )
    with artifact.open("rb") as stream:
        if stream.read(8) != b"\0asm\r\0\1\0":
            raise ValueError("artifact does not have a Wasm component header")
    lock = SOURCE / "Cargo.lock"
    if not lock.is_file():
        raise FileNotFoundError("build the independent workspace and freeze its Cargo.lock first")
    core = REPO / "crates/tobkiri-wit/wit/core/core.wit"
    sources = [SOURCE / "Cargo.toml", lock, SOURCE / "build.rs",
               SOURCE / "wit/pack.wit", SOURCE / "wit/deps/reader.wit",
               *sorted((SOURCE / "src").glob("*.rs")), core,
               Path(__file__).resolve(), PACKS / "build.py"]
    # Validate every source before creating a partial stage directory.
    source_hashes = {str(path.relative_to(REPO)).replace("\\", "/"): file_sha(path)
                     for path in sources}
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "wit/deps").mkdir(parents=True)
    shutil.copyfile(artifact, destination / "component.wasm")
    for name in ["pack.wit", "deps/reader.wit"]:
        shutil.copyfile(SOURCE / "wit" / name, destination / "wit" / name)
    shutil.copyfile(core, destination / "wit/deps/core.wit")
    build.write_json(destination / "config.json", {"timeout_ms": 300000})
    build.write_json(destination / "wit/generation-reader.shape.json", {
        "cardinality": "one", "streams": {}, "callbacks": {}, "conformance": None,
    })
    build.write_json(destination / "component.build.json", {
        "format": "tobkiri.component-build/1", "language": "rust",
        "tools": {"rust": "1.94.0", "wit-bindgen": "0.62.0"},
        "build_profile": build_profile,
        "artifact_sha256": file_sha(artifact), "artifact_bytes": artifact.stat().st_size,
        "sources": source_hashes, "production_enabled": False,
        "scope": "independent generation reader workspace; existing provider artifacts not rebuilt",
    })
    manifest = build.base_manifest("openrouter-generation-reader", {
        "kind": "wasm", "component": "component.wasm",
        "toolchain": "rust/1.94.0;wit-bindgen/0.62.0", "config": {"path": "config.json"},
    }, reader_permissions())
    manifest["ports"]["provides"] = [build.port(VIEW.rsplit("@", 1)[0])]
    manifest["shapes"] = [{
        "name": VIEW.rsplit("@", 1)[0], "version": "1.0", "kind": "port",
        "wit": "wit/deps/reader.wit",
    }]
    return build.bind_files(destination, manifest, [
        "component.wasm", "component.build.json", "config.json", "wit/pack.wit",
        "wit/deps/reader.wit", "wit/deps/core.wit", "wit/generation-reader.shape.json",
    ])


def inspect_archive(path: Path, expected_id: str,
                    expected_hash: str | None = None) -> dict[str, Any]:
    """Check bounded local ustar inventories without extracting anything.

    These checks are not a substitute for CLI signature, WIT or ABI admission.
    """
    if not expected_id or any(character in expected_id for character in "/\\:\0") \
            or expected_id in (".", ".."):
        raise ValueError("invalid Pack identity")
    if path.suffix != ".tpk" or not path.is_file() or path.stat().st_size > MAX_ARCHIVE:
        raise ValueError("only bounded existing .tpk archives are accepted")
    archive_hash = file_sha(path)
    with tarfile.open(path, "r:") as archive:
        members = []
        for member in archive:
            if len(members) >= 16384:
                raise ValueError("archive inventory exceeds the bound")
            members.append(member)
        names = [member.name for member in members]
        if names != sorted(set(names)):
            raise ValueError("archive entries are duplicate or unsorted")
        for member in members:
            relative = PurePosixPath(member.name)
            if not member.isfile() or relative.is_absolute() or ".." in relative.parts \
                    or "\\" in member.name or ":" in member.name \
                    or member.size > MAX_MEMBER:
                raise ValueError("archive has an invalid member")
        manifest_entry = archive.getmember("pack.json")
        if manifest_entry.size > 4 * 1024 * 1024:
            raise ValueError("manifest exceeds the bound")
        raw = archive.extractfile(manifest_entry).read()
        manifest = json.loads(raw)
        manifest_hash = build.sha256(build.canonical(manifest))
        if manifest["id"] != expected_id or (expected_hash is not None and manifest_hash != expected_hash):
            raise ValueError("archive identity/hash differs from the frozen pin")
        bound = {entry["path"]: entry for entry in manifest["files"]}
        if len(bound) != len(manifest["files"]) or set(names) - {"pack.json", "signature.json"} != set(bound):
            raise ValueError("archive inventory differs from its manifest")
        config = None
        config_hash = None
        for name, entry in bound.items():
            member = archive.getmember(name)
            if member.size != entry["size"]:
                raise ValueError("archive member size differs from its manifest")
            digest = hashlib.sha256()
            with archive.extractfile(member) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if "sha256:" + digest.hexdigest() != entry["sha256"]:
                raise ValueError("archive member digest differs from its manifest")
        if "config.json" in bound:
            entry = archive.getmember("config.json")
            if entry.size > 4 * 1024 * 1024:
                raise ValueError("configuration exceeds the bound")
            raw_config = archive.extractfile(entry).read()
            config = json.loads(raw_config)
            config_hash = build.sha256(raw_config)
    if file_sha(path) != archive_hash:
        raise ValueError("archive changed during inspection")
    return {"path": path, "archive_sha256": archive_hash,
            "manifest_sha256": manifest_hash, "manifest": manifest,
            "config": config, "config_sha256": config_hash}


def extend_profile(baseline: Path, reader_archive: Path,
                   destination: Path) -> Path:
    """Copy immutable old TPKs and add only the reader and its User port.

    The emitted definition is for normal Core profile apply/create. This
    function never opens DATA, applies a Profile or approves any capability.
    """
    document = json.loads(baseline.read_text(encoding="utf-8"))
    baseline_hash = file_sha(baseline)
    profile = deepcopy(document)
    originals: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for pin in document["packs"]:
        if pin["id"] in by_id or pin["id"] == PACK_ID:
            raise ValueError("duplicate or already installed reader Pack")
        source = pin["source"]
        if not source.startswith("file:"):
            raise ValueError("baseline sources must be existing file: TPK archives")
        path = Path(source[5:])
        if not path.is_absolute():
            path = baseline.parent / path
        inspected = inspect_archive(path.resolve(), pin["id"], pin["sha256"])
        if inspected["manifest"]["version"] != pin["version"]:
            raise ValueError("archive version differs from the frozen pin")
        originals.append(inspected)
        by_id[pin["id"]] = inspected
    if "local.deepthink" not in by_id or "local.openrouter-mimo-provider" not in by_id:
        raise ValueError("the recovery definition requires its frozen DeepThink/MiMo Packs")
    frozen_config = by_id["local.deepthink"]["config"]
    if frozen_config["environment"]["provider_pack_hash"] != by_id["local.openrouter-mimo-provider"]["manifest_sha256"]:
        raise ValueError("DeepThink's captured provider hash differs from the baseline")
    reader = inspect_archive(reader_archive.resolve(), PACK_ID)
    # Existing provider authority must already exist. Add only a typed User port,
    # not a new network origin, credential, storage capability or resource limit.
    ceiling = profile["ceiling"]
    if ORIGIN not in ceiling["network"]["origins"] or not any(
        item["name"] == "api-key" for item in ceiling["secrets"]
    ):
        raise ValueError("baseline ceiling lacks the existing OpenRouter/opaque-key authority")
    if not isinstance(profile.get("user_permissions"), dict):
        raise ValueError("an explicit existing User policy is required")
    if reader["manifest"]["permissions"] != _normalized_reader_permissions():
        raise ValueError("reader archive authority differs from the narrow declared permission set")
    reader_manifest = reader["manifest"]
    if reader_manifest["runtime"]["kind"] != "wasm" or any(
        reader_manifest[key] for key in ("stores", "flows", "overlays")
    ) or reader_manifest["ports"]["consumes"] or reader_manifest["ports"]["provides"] != [
        build.port(VIEW.rsplit("@", 1)[0])
    ]:
        raise ValueError("reader archive is not the standalone read-only port")
    binding = VIEW.rsplit("@", 1)[0] + "@1"
    if binding in profile["bindings"] or PACK_ID in profile["pack_limits"]:
        raise ValueError("reader binding or policy already exists")
    for permissions in (ceiling, profile["user_permissions"]):
        permissions["ports"] = sorted(set(permissions["ports"]) | {VIEW})
    profile["bindings"][binding] = PACK_ID
    profile["pack_limits"][PACK_ID] = build.intersect_permissions(reader_permissions(), ceiling)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "packs").mkdir()
    copies = []
    for pin, inspected in zip(profile["packs"], originals):
        filename = pin["id"] + "-" + pin["sha256"].removeprefix("sha256:") + ".tpk"
        copied = destination / "packs" / filename
        shutil.copyfile(inspected["path"], copied)
        if file_sha(copied) != inspected["archive_sha256"] or file_sha(inspected["path"]) != inspected["archive_sha256"]:
            raise ValueError("baseline archive changed while copying")
        pin["source"] = "file:packs/" + filename
        copies.append({"id": pin["id"], "manifest_sha256": pin["sha256"],
                       "archive_sha256": inspected["archive_sha256"], "copied_without_rebuild": True})
    reader_name = PACK_ID + "-" + reader["manifest_sha256"].removeprefix("sha256:") + ".tpk"
    reader_copy = destination / "packs" / reader_name
    shutil.copyfile(reader["path"], reader_copy)
    if file_sha(reader_copy) != reader["archive_sha256"]:
        raise ValueError("reader archive changed while copying")
    profile["packs"].append({"id": PACK_ID, "version": reader["manifest"]["version"],
        "sha256": reader["manifest_sha256"], "source": "file:packs/" + reader_name})
    output = destination / "profile.json"
    if file_sha(baseline) != baseline_hash:
        raise ValueError("baseline definition changed during preparation")
    build.write_json(output, profile)
    build.write_json(destination / "PREPARATION.json", {
        "format": "tobkiri.generation-reader-profile-preparation/2",
        "baseline_definition_sha256": baseline_hash,
        "profile_definition_sha256": file_sha(output),
        "declared_profile_id": profile.get("id"),
        "identity_assigned_by_core_command": "id" not in profile,
        "reused_packs": copies,
        "deepthink_config_sha256": by_id["local.deepthink"]["config_sha256"],
        "reader_archive_sha256": reader["archive_sha256"],
        "core_admission_performed": False, "model_calls": 0, "lookup_calls": 0,
        "checkpoint_changes": False, "reconciliation_implemented": False,
    })
    return output


def _normalized_reader_permissions() -> dict[str, Any]:
    manifest = build.base_manifest("openrouter-generation-reader", {"kind": "data"}, reader_permissions())
    build.normalize_manifest(manifest)
    return manifest["permissions"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    staging = commands.add_parser("stage")
    staging.add_argument("--target-dir", type=Path, required=True)
    staging.add_argument("--destination", type=Path, required=True)
    staging.add_argument("--build-profile", choices=("debug", "release"), default="release")
    profile = commands.add_parser("profile")
    profile.add_argument("--baseline", type=Path, required=True)
    profile.add_argument("--reader-archive", type=Path, required=True)
    profile.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "stage":
        stage(args.target_dir, args.destination, args.build_profile)
    else:
        extend_profile(args.baseline, args.reader_archive, args.destination)


if __name__ == "__main__":
    main()
