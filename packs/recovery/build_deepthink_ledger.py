"""Prepare an incremental DeepThink ledger Pack from frozen local archives.

No build, Core, DATA, Vault or network operation is performed here. Existing
provider/tool archives and the exact DeepThink configuration bytes are retained.
Prepared artifacts still need ordinary CLI verification and Core ABI admission.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
from typing import Any

HERE = Path(__file__).resolve().parent
PACKS = HERE.parent
REPO = PACKS.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PACKS))
import build
import build_deepthink
import build_deepthink_input as input_adapter
import build_generation_reader as reader

DEEPTHINK = "local.deepthink"
PROVIDER = "local.openrouter-mimo-provider"
REGENERATION = DEEPTHINK + ".regeneration-flow@1.0"
CONTROL = DEEPTHINK + ".control-flow@1.0"
INPUT_CONTEXT = DEEPTHINK + ".input-context@1.0"
ADDED_PORTS = {REGENERATION, reader.VIEW, CONTROL, INPUT_CONTEXT,
               *build_deepthink.NEW_VIEWS}
NEW_PUBLIC_VIEWS = {input_adapter.DIAGNOSTIC_VIEW, input_adapter.GOAL_REVIEW_VIEW}
MAX_JSON = 4 * 1024 * 1024
MAX_PACKS = 256
COMPONENT_HEADER = b"\0asm\r\0\1\0"


def _json(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("nonfinite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def _new_destination(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise FileExistsError("destination must not already exist")
    return path.resolve()


def _unchanged(inspection: dict[str, Any]) -> None:
    if reader.file_sha(inspection["path"]) != inspection["archive_sha256"]:
        raise ValueError("input archive changed during preparation")


def _member(inspection: dict[str, Any], name: str,
            maximum: int = MAX_JSON) -> bytes:
    _unchanged(inspection)
    with tarfile.open(inspection["path"], "r:") as archive:
        member = archive.getmember(name)
        if not member.isfile() or member.size > maximum:
            raise ValueError("archive member exceeds its bound")
        raw = archive.extractfile(member).read()
    _unchanged(inspection)
    if len(raw) != member.size:
        raise ValueError("archive member length changed")
    if name != "pack.json":
        entries = {entry["path"]: entry for entry in inspection["manifest"]["files"]}
        entry = entries.get(name)
        if entry is None or entry["size"] != len(raw) \
                or entry["sha256"] != build.sha256(raw):
            raise ValueError("archive member differs from frozen inventory")
    return raw


def _inspect(path: Path, identity: str,
             digest: str | None = None) -> dict[str, Any]:
    try:
        inspected = reader.inspect_archive(path.resolve(), identity, digest)
    except (KeyError, TypeError, json.JSONDecodeError, tarfile.TarError) as error:
        raise ValueError("malformed Pack archive inventory") from error
    # No extraction occurs. Reject noncanonical names even if a tar reader
    # would normalize them to a different inventory member.
    with tarfile.open(inspected["path"], "r:") as archive:
        for member in archive:
            if member.name != PurePosixPath(member.name).as_posix() \
                    or any(ord(char) < 32 for char in member.name):
                raise ValueError("archive has a noncanonical member path")
    if _json(_member(inspected, "pack.json")) != inspected["manifest"]:
        raise ValueError("manifest parsing is inconsistent")
    return inspected


def _baseline(path: Path) -> tuple[dict[str, Any], str, dict[str, Any]]:
    if not path.is_file() or path.stat().st_size > MAX_JSON:
        raise ValueError("baseline must be a bounded existing Profile JSON file")
    raw = path.read_bytes()
    digest = build.sha256(raw)
    profile = _json(raw)
    if not isinstance(profile, dict) or not isinstance(profile.get("packs"), list) \
            or not 1 <= len(profile["packs"]) <= MAX_PACKS:
        raise ValueError("invalid frozen Profile pack inventory")
    by_id = {}
    for pin in profile["packs"]:
        identity = pin["id"]
        if identity in by_id:
            raise ValueError("duplicate frozen Pack identity")
        source = pin["source"]
        if not isinstance(source, str) or not source.startswith("file:") \
                or not source[5:] or "\0" in source:
            raise ValueError("only existing file: TPK sources are supported")
        archive_path = Path(source[5:])
        if not archive_path.is_absolute():
            archive_path = path.parent / archive_path
        inspected = _inspect(archive_path, identity, pin["sha256"])
        if inspected["manifest"]["version"] != pin["version"]:
            raise ValueError("archive version differs from frozen pin")
        by_id[identity] = inspected
    if DEEPTHINK not in by_id or PROVIDER not in by_id:
        raise ValueError("frozen DeepThink and MiMo provider Packs are required")
    config = _json(_member(by_id[DEEPTHINK], "config.json"))
    if not isinstance(config, dict) or config.get("environment", {}).get(
        "provider_pack_hash"
    ) != by_id[PROVIDER]["manifest_sha256"]:
        raise ValueError("DeepThink captured provider pin differs from frozen Profile")
    for key in ("ceiling", "user_permissions", "pack_limits", "bindings"):
        if not isinstance(profile.get(key), dict):
            raise ValueError("explicit Profile policy and bindings are required")
    if DEEPTHINK not in profile["pack_limits"]:
        raise ValueError("frozen DeepThink Pack limit is required")
    if reader.file_sha(path) != digest:
        raise ValueError("baseline changed during inspection")
    return profile, digest, by_id


def _recheck_baseline(path: Path, digest: str, by_id: dict[str, Any]) -> None:
    if reader.file_sha(path) != digest:
        raise ValueError("baseline changed during preparation")
    for inspected in by_id.values():
        _unchanged(inspected)


def _ports(permissions: dict[str, Any]) -> set[str]:
    ports = permissions.get("ports")
    if not isinstance(ports, list) or not all(isinstance(item, str) for item in ports) \
            or len(ports) != len(set(ports)):
        raise ValueError("permission ports must be an explicit unique list")
    return set(ports)


def _ledger_permissions(old: dict[str, Any]) -> dict[str, Any]:
    permissions = deepcopy(old)
    permissions["ports"] = sorted(_ports(old) | ADDED_PORTS)
    return permissions


def _replace_bound_file(directory: Path, manifest: dict[str, Any],
                        relative: str) -> None:
    matches = [entry for entry in manifest["files"] if entry["path"] == relative]
    if len(matches) != 1:
        raise ValueError("staged file has no unique manifest binding")
    raw = (directory / relative).read_bytes()
    matches[0].update(size=len(raw), sha256=build.sha256(raw))


def stage_from_frozen(target_dir: Path, baseline_profile: Path,
                      destination: Path, build_profile: str = "release") -> dict:
    """Stage only new DeepThink bytes, preserving old config and numeric limits."""
    destination = _new_destination(destination)
    _, baseline_hash, originals = _baseline(baseline_profile)
    old = originals[DEEPTHINK]
    raw_config = _member(old, "config.json")
    config = _json(raw_config)
    if old["manifest"]["runtime"].get("config", {}).get("path") != "config.json":
        raise ValueError("frozen DeepThink config must be bound as config.json")
    artifact = build.component_artifact(
        target_dir, "tobkiri_pack_deepthink.wasm", build_profile,
    )
    with artifact.open("rb") as stream:
        if stream.read(8) != COMPONENT_HEADER:
            raise ValueError("artifact does not have a Wasm component header")
    artifact_hash = reader.file_sha(artifact)
    helper_hash = reader.file_sha(Path(__file__))
    # Reserve the new directory before calling a utility that permits reuse.
    destination.mkdir(parents=True, exist_ok=False)
    manifest = build_deepthink.stage(
        target_dir, config, destination=destination, build_profile=build_profile,
    )
    old_ports = _ports(old["manifest"]["permissions"])
    if _ports(manifest["permissions"]) != old_ports | ADDED_PORTS:
        raise ValueError("new DeepThink requires unexpected port authority")
    manifest["permissions"] = _ledger_permissions(old["manifest"]["permissions"])
    (destination / "config.json").write_bytes(raw_config)
    _replace_bound_file(destination, manifest, "config.json")
    provenance_path = destination / "component.build.json"
    provenance = _json(provenance_path.read_bytes())
    provenance["sources"]["packs/recovery/build_deepthink_ledger.py"] = helper_hash
    provenance["incremental_frozen_baseline"] = {
        "baseline_definition_sha256": baseline_hash,
        "deepthink_archive_sha256": old["archive_sha256"],
        "deepthink_manifest_sha256": old["manifest_sha256"],
        "exact_config_sha256": build.sha256(raw_config),
        "old_permissions_sha256": build.sha256(build.canonical(old["manifest"]["permissions"])),
        "old_numeric_limits_preserved": True,
        "other_packs_rebuilt": False,
    }
    build.write_json(provenance_path, provenance)
    _replace_bound_file(destination, manifest, "component.build.json")
    build.write_json(destination / "pack.json", manifest)
    if reader.file_sha(artifact) != artifact_hash \
            or reader.file_sha(destination / "component.wasm") != artifact_hash \
            or reader.file_sha(Path(__file__)) != helper_hash:
        raise ValueError("stage input changed during preparation")
    _recheck_baseline(baseline_profile, baseline_hash, originals)
    return manifest


def _validate_reader(inspected: dict[str, Any]) -> None:
    manifest = inspected["manifest"]
    if manifest["permissions"] != reader._normalized_reader_permissions():
        raise ValueError("reader authority differs from its read-only contract")
    if manifest["runtime"]["kind"] != "wasm" \
            or any(manifest[key] for key in ("stores", "flows", "overlays")) \
            or manifest["ports"]["consumes"] or manifest["ports"]["untyped"] \
            or manifest["ports"]["provides"] != [build.port(reader.VIEW.rsplit("@", 1)[0])]:
        raise ValueError("reader is not the standalone read-only typed port")


def _validate_ledger(old: dict[str, Any], ledger: dict[str, Any],
                     generation_reader: dict[str, Any]) -> None:
    manifest = ledger["manifest"]
    if _member(ledger, "config.json") != _member(old, "config.json"):
        raise ValueError("DeepThink configuration bytes changed")
    if manifest["permissions"] != _ledger_permissions(old["manifest"]["permissions"]):
        raise ValueError("DeepThink authority or numeric limits changed")
    if manifest["runtime"]["kind"] != "wasm" \
            or manifest["runtime"].get("config", {}).get("path") != "config.json":
        raise ValueError("ledger Pack must use the frozen DeepThink config path")
    consumed = {entry["shape"] + "@" + entry["version"]
                for entry in manifest["ports"]["consumes"]}
    if not ADDED_PORTS <= consumed \
            or manifest["ports"]["provides"] != [
                build.port(DEEPTHINK + ".workflow"),
                build.port(DEEPTHINK + ".input-context"),
                build.port(DEEPTHINK + ".tool-receipt-reader"),
                build.port(DEEPTHINK + ".goal-review-worker"),
            ]:
        raise ValueError("new recovery typed ports and Flow are not bound")
    owned = {(shape["name"], shape["version"], shape["kind"], shape["wit"])
             for shape in manifest["shapes"]}
    if not {
        (DEEPTHINK + "." + name, "1.0", kind, "wit/deps/deepthink.wit")
        for name, kind in (("workflow", "port"), ("records", "data"),
                           ("continuation-input", "port"),
                           ("continuation-flow", "flow"), ("regeneration-flow", "flow"),
                           ("control-flow", "flow"), ("input-context", "port"),
                           *((name, "port") for name in build_deepthink.NEW_PORT_SHAPES),
                           *((name, "flow") for name in build_deepthink.NEW_FLOW_SHAPES))
    } <= owned:
        raise ValueError("DeepThink input contracts lack their defining shape owner")
    for name in ("continuation", "regeneration", "control", "tool-diagnostics", "goal-review"):
        path = f"flows/{name}.flow.json"
        if path not in manifest["flows"]:
            raise ValueError("new input and recovery Flows are not bound")
        flow = _json(_member(ledger, path))
        if flow.get("implements") != DEEPTHINK + f".{name}-flow@1.0":
            raise ValueError("input or recovery Flow identity differs")
    if _member(ledger, "wit/deps/generation-reader.wit") != _member(
        generation_reader, "wit/deps/reader.wit"
    ):
        raise ValueError("reader dependency WIT differs from selected reader Pack")


def _validate_input(inspected: dict[str, Any], ledger: dict[str, Any]) -> None:
    """Require the narrow adapter and the selected worker's nominal contract."""
    manifest = inspected["manifest"]
    expected = build.base_manifest("deepthink-input", {"kind": "data"},
                                   input_adapter.permissions())
    build.normalize_manifest(expected)
    if manifest["permissions"] != expected["permissions"]:
        raise ValueError("input adapter authority differs from its Flow-only contract")
    if manifest["runtime"]["kind"] != "wasm" \
            or manifest["runtime"].get("config", {}).get("path") != "config.json" \
            or any(manifest[key] for key in ("shapes", "stores", "flows", "overlays")) \
            or manifest["ports"]["untyped"] \
            or manifest["ports"]["provides"] != [
                *(build.port(view.rsplit("@", 1)[0]) for view in input_adapter.PUBLIC_VIEWS),
            ] \
            or manifest["ports"]["consumes"] != input_adapter.consumed_ports():
        raise ValueError("input adapter is not the standalone typed Flow bridge")
    if _json(_member(inspected, "config.json")) != {}:
        raise ValueError("input adapter configuration changed")
    if _member(inspected, "wit/deps/deepthink.wit") != _member(
        ledger, "wit/deps/deepthink.wit",
    ):
        raise ValueError("input adapter dependency WIT differs from selected DeepThink")


def _copy(inspected: dict[str, Any], directory: Path) -> str:
    filename = (inspected["manifest"]["id"] + "-"
                + inspected["manifest_sha256"].removeprefix("sha256:") + ".tpk")
    target = directory / filename
    _unchanged(inspected)
    with inspected["path"].open("rb") as source, target.open("xb") as output:
        shutil.copyfileobj(source, output, 1024 * 1024)
    if reader.file_sha(target) != inspected["archive_sha256"]:
        raise ValueError("copied archive differs from frozen input")
    _unchanged(inspected)
    return "file:packs/" + filename


def extend_profile(baseline_profile: Path, ledger_archive: Path,
                   reader_archive: Path, destination: Path, *,
                   input_archive: Path) -> Path:
    """Retain frozen data/model Packs and add the ordinary Flow input bridge."""
    destination = _new_destination(destination)
    original, baseline_hash, originals = _baseline(baseline_profile)
    ledger = _inspect(ledger_archive, DEEPTHINK)
    generation_reader = _inspect(reader_archive, reader.PACK_ID)
    _validate_reader(generation_reader)
    _validate_ledger(originals[DEEPTHINK], ledger, generation_reader)
    adapter = _inspect(input_archive, input_adapter.PACK_ID)
    _validate_input(adapter, ledger)
    if reader.PACK_ID in originals:
        _validate_reader(originals[reader.PACK_ID])
        if generation_reader["archive_sha256"] != originals[reader.PACK_ID]["archive_sha256"]:
            raise ValueError("existing reader archive bytes must remain unchanged")
    profile = deepcopy(original)
    ceiling = profile["ceiling"]
    if reader.ORIGIN not in ceiling["network"]["origins"] or not any(
        item["name"] == "api-key" and not item["reveal"]
        for item in ceiling["secrets"]
    ):
        raise ValueError("frozen ceiling lacks existing OpenRouter opaque-key authority")
    ceiling["ports"] = sorted(_ports(ceiling) | ADDED_PORTS)
    # User invokes regenerate-phase on its already authorized continuation-input
    # port. The Pack owns the new Flow; User receives only the read-only reader.
    user = profile["user_permissions"]
    user["ports"] = sorted(_ports(user) | {reader.VIEW} | NEW_PUBLIC_VIEWS)
    binding = reader.VIEW.rsplit("@", 1)[0] + "@1"
    if profile["bindings"].get(binding, reader.PACK_ID) != reader.PACK_ID:
        raise ValueError("reader binding belongs to a different Pack")
    profile["bindings"][binding] = reader.PACK_ID
    input_binding = input_adapter.INPUT_VIEW.rsplit("@", 1)[0] + "@1"
    if profile["bindings"].get(input_binding) not in (DEEPTHINK, input_adapter.PACK_ID):
        raise ValueError("continuation input binding belongs to a different Pack")
    profile["bindings"][input_binding] = input_adapter.PACK_ID
    context_binding = INPUT_CONTEXT.rsplit("@", 1)[0] + "@1"
    if profile["bindings"].get(context_binding, DEEPTHINK) != DEEPTHINK:
        raise ValueError("input context binding belongs to a different Pack")
    profile["bindings"][context_binding] = DEEPTHINK
    for view, provider in [
        ("local.deepthink.tool-receipt-reader@1.0", DEEPTHINK),
        ("local.deepthink.goal-review-worker@1.0", DEEPTHINK),
        *((view, input_adapter.PACK_ID) for view in sorted(NEW_PUBLIC_VIEWS)),
    ]:
        binding = view.rsplit("@", 1)[0] + "@1"
        if profile["bindings"].get(binding, provider) != provider:
            raise ValueError("new ordinary operation binding belongs to a different Pack")
        profile["bindings"][binding] = provider
    deepthink_limit = deepcopy(profile["pack_limits"][DEEPTHINK])
    deepthink_limit["ports"] = sorted(_ports(deepthink_limit) | ADDED_PORTS)
    profile["pack_limits"][DEEPTHINK] = build.intersect_permissions(
        build.intersect_permissions(ledger["manifest"]["permissions"], deepthink_limit),
        ceiling,
    )
    reader_limit = generation_reader["manifest"]["permissions"]
    if reader.PACK_ID in profile["pack_limits"]:
        reader_limit = build.intersect_permissions(
            reader_limit, profile["pack_limits"][reader.PACK_ID],
        )
    profile["pack_limits"][reader.PACK_ID] = build.intersect_permissions(
        reader_limit, ceiling,
    )
    adapter_limit = adapter["manifest"]["permissions"]
    if input_adapter.PACK_ID in profile["pack_limits"]:
        # Keep all old non-port restrictions; admit only new public/Flow views
        # before applying the normal manifest/ceiling intersection.
        previous_adapter_limit = deepcopy(profile["pack_limits"][input_adapter.PACK_ID])
        previous_adapter_limit["ports"] = sorted(
            _ports(previous_adapter_limit) | set(input_adapter.PUBLIC_VIEWS)
            | set(input_adapter.FLOW_VIEWS))
        adapter_limit = build.intersect_permissions(
            adapter_limit, previous_adapter_limit,
        )
    profile["pack_limits"][input_adapter.PACK_ID] = build.intersect_permissions(
        adapter_limit, ceiling,
    )
    replacements = {DEEPTHINK: ledger, reader.PACK_ID: generation_reader,
                    input_adapter.PACK_ID: adapter}
    if len(set(originals) | set(replacements)) > MAX_PACKS:
        raise ValueError("new Profile pack inventory exceeds its bound")
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "packs").mkdir()
    copies = []
    for pin in profile["packs"]:
        identity = pin["id"]
        inspected = replacements.get(identity, originals[identity])
        pin.update(version=inspected["manifest"]["version"],
                   sha256=inspected["manifest_sha256"],
                   source=_copy(inspected, destination / "packs"))
        copies.append({"id": identity, "manifest_sha256": pin["sha256"],
                       "archive_sha256": inspected["archive_sha256"],
                       "unchanged_from_baseline": inspected["archive_sha256"]
                       == originals[identity]["archive_sha256"]})
    for identity in (reader.PACK_ID, input_adapter.PACK_ID):
        if identity in originals:
            continue
        inspected = replacements[identity]
        profile["packs"].append({
            "id": identity, "version": inspected["manifest"]["version"],
            "sha256": inspected["manifest_sha256"],
            "source": _copy(inspected, destination / "packs"),
        })
        copies.append({"id": identity, "manifest_sha256": inspected["manifest_sha256"],
                       "archive_sha256": inspected["archive_sha256"],
                       "unchanged_from_baseline": False})
    _recheck_baseline(baseline_profile, baseline_hash, originals)
    _unchanged(ledger)
    _unchanged(generation_reader)
    _unchanged(adapter)
    output = destination / "profile.json"
    build.write_json(output, profile)
    build.write_json(destination / "PREPARATION.json", {
        "format": "tobkiri.deepthink-ledger-profile-preparation/1",
        "baseline_definition_sha256": baseline_hash,
        "profile_definition_sha256": reader.file_sha(output),
        "declared_profile_id": profile.get("id"),
        "identity_assigned_by_core_command": "id" not in profile,
        "exact_deepthink_config_sha256": ledger["config_sha256"],
        "deepthink_manifest_sha256": ledger["manifest_sha256"],
        "deepthink_archive_sha256": ledger["archive_sha256"],
        "reader_manifest_sha256": generation_reader["manifest_sha256"],
        "reader_archive_sha256": generation_reader["archive_sha256"],
        "input_manifest_sha256": adapter["manifest_sha256"],
        "input_archive_sha256": adapter["archive_sha256"],
        "input_binding": input_adapter.PACK_ID,
        "selected_pack_count": len(profile["packs"]),
        "old_reader_replaced": False,
        "existing_reader_bytes_retained": reader.PACK_ID in originals,
        "copied_packs": copies,
        "nonport_ceiling_and_user_policy_unchanged": True,
        "other_pack_limits_unchanged": True,
        "other_packs_rebuilt": False,
        "core_admission_performed": False,
        "runtime_acceptance_performed": False,
        "profile_applied": False,
        "model_calls": 0, "lookup_calls": 0, "checkpoint_changes": False,
    })
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    staging = commands.add_parser("stage")
    staging.add_argument("--target-dir", type=Path, required=True)
    staging.add_argument("--baseline", type=Path, required=True)
    staging.add_argument("--destination", type=Path, required=True)
    staging.add_argument("--build-profile", choices=("debug", "release"), default="release")
    profile = commands.add_parser("profile")
    profile.add_argument("--baseline", type=Path, required=True)
    profile.add_argument("--ledger-archive", type=Path, required=True)
    profile.add_argument("--reader-archive", type=Path, required=True)
    profile.add_argument("--input-archive", type=Path, required=True)
    profile.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "stage":
        stage_from_frozen(args.target_dir, args.baseline, args.destination,
                          args.build_profile)
    else:
        extend_profile(args.baseline, args.ledger_archive, args.reader_archive,
                       args.destination, input_archive=args.input_archive)


if __name__ == "__main__":
    main()
