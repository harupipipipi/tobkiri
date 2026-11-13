"""Record Shell IO and Pack resolution in a pinned original Setup review."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import plistlib
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any
from unittest.mock import patch

from original_authority_reference import ORIGINAL_HEAD, error_graph, write_json
from original_catalog_prefix_reference import bind_resources
from original_setup_candidate_selection_reference import DENIED_CLASS

FORMAT = "io.tobkiri.original-setup-shell-reference.v1"
CASES = (
    "selected-shell", "missing-artifact-root", "symlink-artifact-root",
    "symlink-selected-artifact", "missing-selected-artifact",
    "symlink-at-tree-entry", "missing-at-tree-entry",
    "entrypoint-missing-after-tree", "entrypoint-symlink-after-tree",
    "entrypoint-bytes-after-tree",
)
ARTIFACT_ROOT = "ecosystem/defaultspack/platform-artifacts"
BUNDLE = Path("ecosystem/defaultspack/v4")
PROFILE = "defaults.profile.v5.json"
APPLICATION = "runtime.tauri.application.default"
PACK_STALE_PIN = "stale-application-pack-pin"
COMPOSITION_CASES = ("packaged-profile-resolved", PACK_STALE_PIN)
PREDECESSOR_CASES = ("fresh-pack-pin-predecessor", "corrupt-pack-pin-predecessor")
CORRUPT_POINTER = b"{\n"
STALE_PIN = "sha256:" + "0" * 64


class _ShellVerified(BaseException):
    """Stop after the original verifier returns, before Setup publishes anything."""


class _PackResolved(BaseException):
    """Stop at the successful original resolve Port return, before state creation."""


class _BeforeInactivePredecessor(BaseException):
    """Stop before the next helper executes its first instruction."""


def _package(original: Path, runtime: Path, output: Path) -> tuple[dict, list[str]]:
    # Reuse the pinned conformance helper's sealed source provenance. The
    # packaged generator and product modules are imported from the staged tree.
    source = original / "tobkiri_runtime"
    sys.path.insert(1, str(source))
    try:
        from tests.conformance_support.packaged_profile import (
            create_test_source_provenance,
        )
    finally:
        sys.path.remove(str(source))
    from scripts.generate_packaged_defaultspack_v4_bundle import stage_packaged_bundle
    from tobkiri_protocol.bundle_catalog import BundledCatalog

    tree = subprocess.check_output(
        ["git", "-C", str(original), "rev-parse", "--verify", "HEAD^{tree}"],
        text=True,
    ).strip()
    provenance = create_test_source_provenance(
        source, output / "packaging",
        provenance_record={
            "source_commit": ORIGINAL_HEAD, "source_tree": tree, "source_clean": True,
        },
    )
    release = output / "shell-release"
    release.mkdir(mode=0o700)
    bundle_identity = "io.tobkiri.shell.tauri"
    if sys.platform == "darwin":
        platform, architecture = "macos", "arm64"
        artifact = "Tobkiri.app"
        entrypoint = f"{artifact}/Contents/MacOS/tobkiri-shell"
        info = f"{artifact}/Contents/Info.plist"
        resource = f"{artifact}/Contents/Resources/fixture.txt"
        files = {
            entrypoint: b"\xcf\xfa\xed\xfe" + (0x0100000C).to_bytes(4, "little"),
            info: plistlib.dumps({"CFBundleIdentifier": bundle_identity}),
            resource: b"Shell resource fixture\n",
        }
        tree_files = [info, entrypoint, resource]
    elif sys.platform == "linux":
        platform, architecture = "linux", "x86_64"
        artifact = entrypoint = "Tobkiri.AppImage"
        files = {entrypoint: b"\x7fELF\x02\x01" + bytes(12) + (62).to_bytes(2, "little")}
        tree_files = [entrypoint]
    else:
        raise ValueError("unsupported original Shell reference host")
    for name, data in files.items():
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (release / entrypoint).chmod(0o755)
    bundle = runtime / BUNDLE
    artifacts = runtime / ARTIFACT_ROOT
    stage_packaged_bundle(
        source_artifact=release / artifact,
        bundle_root=bundle,
        artifact_root=artifacts,
        relative_path=artifact,
        entrypoint=entrypoint,
        platform=platform,
        architecture=architecture,
        bundle_identity=bundle_identity,
        source_provenance_file=provenance,
    )
    bind_resources(runtime, packaged_artifacts=True)
    catalog = BundledCatalog.load(bundle)
    [variant] = catalog.shells["shell.tauri.default"]["launch"]["variants"]
    if catalog.artifact_root != artifacts:
        raise AssertionError("packaged fixture did not load its selected Shell root")
    return variant, tree_files


@contextmanager
def _composition_input(case: str, runtime: Path) -> Iterator[None]:
    if case != PACK_STALE_PIN:
        yield
        return

    from scripts.profile_compatibility_provenance import (
        compatibility_profile_payload,
        validate_compatibility_profile,
    )
    from tobkiri_protocol.canonical import canonical_digest

    bundle = runtime / BUNDLE
    profile_path = bundle / PROFILE
    lock_path = bundle / "bundle.lock.json"
    manifest_path = runtime / "runtime-resource-manifest.v1.json"
    paths = (profile_path, lock_path, manifest_path)
    original_profile, original_lock, original_manifest = [
        path.read_bytes() for path in paths
    ]
    profile = json.loads(original_profile)
    [application] = [pack for pack in profile["packs"] if pack["pack_id"] == APPLICATION]
    if application["artifact_digest"] is not None:
        raise AssertionError("packaged Profile no longer requests an unresolved Application")
    application["artifact_digest"] = STALE_PIN
    profile["provenance"]["source_digest"] = canonical_digest(
        compatibility_profile_payload(profile)
    )
    validate_compatibility_profile(profile)
    lock = json.loads(original_lock)
    [entry] = [entry for entry in lock["entries"] if entry["path"] == PROFILE]
    try:
        write_json(profile_path, profile)
        entry["digest"] = "sha256:" + hashlib.sha256(profile_path.read_bytes()).hexdigest()
        write_json(lock_path, lock)
        bind_resources(runtime, packaged_artifacts=True)
        yield
    finally:
        for path, raw in zip(paths, (original_profile, original_lock, original_manifest)):
            path.write_bytes(raw)


def _resolved_facts(result: Any, local: dict, variant: dict) -> dict:
    from ecosystem.defaultspack.domain.profile_resolution import ResolvedDefaultProfile
    from tobkiri_protocol.canonical import canonical_digest

    names = ("profile", "lock", "plan")
    documents = {name: getattr(result, name) for name in names}
    if (
        type(result) is not ResolvedDefaultProfile
        or set(vars(result)) != set(names)
        or any(type(value) is not dict for value in documents.values())
        or any(value is not local[name] for name, value in documents.items())
        or len({id(value) for value in documents.values()}) != len(names)
    ):
        raise AssertionError("original candidate did not retain its three resolved records")
    profile, lock, plan = result.profile, result.lock, result.plan
    digests = {name: canonical_digest(value) for name, value in documents.items()}
    if (
        profile["profile_id"] != local["profile_id"]
        or profile["state"] != "resolved"
        or any(profile["profile_id"] != record["profile_id"] for record in (plan, lock))
        or plan["profile_revision"] != digests["profile"]
        or any(plan[key] != lock[key] for key in (
            "profile_revision", "profile_definition_digest", "catalog_revision",
            "bundle_digest", "security_epoch", "profile_authority_snapshot_digest",
            "requested_edges_digest", "constraints_digest", "closure_digest",
            "provenance_digest", "plan_digest",
        ))
        or plan["profile_definition_digest"] != canonical_digest(local["source"])
        or plan["catalog_revision"] != profile["catalog_revision"]
        or plan["profile_authority_snapshot_digest"] != local["authority_snapshot_digest"]
        or plan["security_epoch"] != local["security_epoch"]
        or plan["shell"]["executable_artifact_digest"] != variant["entrypoint_digest"]
        or plan["launch_contribution"]["artifact_digest"] != variant["artifact_digest"]
        or plan["launch_contribution"]["entrypoint"] != variant["entrypoint"]
    ):
        raise AssertionError("original candidate digest or identity bindings changed")
    for name, record in (("lock", lock), ("plan", plan)):
        key = f"{name}_digest"
        if record[key] != canonical_digest({k: v for k, v in record.items() if k != key}):
            raise AssertionError(f"original {name} digest does not bind its document")
    return {
        "wrapper": {
            "type": f"{type(result).__module__}.{type(result).__qualname__}",
            "fields": list(vars(result)),
            "same_pack_records": True,
            "distinct_record_references": True,
        },
        "canonical_document_digests": digests,
        "api_versions": {
            "profile": profile["profile_api_version"],
            "lock": lock["lock_api_version"],
            "plan": plan["plan_api_version"],
        },
        "identity": {key: plan[key] for key in (
            "profile_id", "profile_revision", "profile_definition_digest",
            "catalog_revision", "bundle_digest", "security_epoch",
            "profile_authority_snapshot_digest", "requested_edges_digest",
            "constraints_digest", "closure_digest", "provenance_digest", "plan_digest",
        )} | {"lock_digest": lock["lock_digest"]},
        "base": plan["base"],
        "shell": plan["shell"],
        "application": plan["application"],
        "launch_contribution": plan["launch_contribution"],
        "effective_set": plan["effective_set"],
        "record_counts": {
            "bindings": len(plan["bindings"]),
            "variant_pins": len(lock["variant_pins"]),
            "requested_edges": len(profile["requested_edges"]),
            "authority_references": len(profile["authority_references"]),
            "content_projections": len(plan["content_projections"]),
        },
    }


class _PredecessorObservation:
    """Trace the actual Host reads after successful Pack resolution."""

    def __init__(self, runtime: Path, user_data: Path, *, corrupt: bool) -> None:
        from core_runtime import active_profile_store_v4 as active
        from core_runtime import profile_definition_store_v4 as definition
        from core_runtime.bootstrap import profile_capture as capture

        self.user_data = user_data
        self.corrupt = corrupt
        self.pointer_path = user_data / "profiles" / "active.json"
        self.lock_path = user_data / "profiles" / ".profile-definition.lock"
        self.binding = capture._bootstrap_pack_pin_predecessor_binding.__code__
        self.setup = capture._resolve_bootstrap_candidate.__code__
        self.inactive = capture.inactive_bootstrap_predecessor.__code__
        self.snapshot = definition.ProfileDefinitionStore.snapshot.__code__
        self.get_profile = definition.ProfileDefinitionStore.get_profile.__code__
        self.lock = inspect.unwrap(active.exclusive_profile_lock).__code__
        self.load = active.ActiveProfileStore.load.__code__
        self.integrity_error = active.ActiveProfileStoreIntegrityError
        for source, codes in (
            ("bootstrap/profile_capture.py", (self.binding, self.setup)),
            ("bootstrap/profile_predecessor.py", (self.inactive,)),
            ("profile_definition_store_v4.py", (self.snapshot, self.get_profile)),
            ("active_profile_store_v4.py", (self.lock, self.load)),
        ):
            if any(Path(code.co_filename).resolve() != runtime / "core_runtime" / source
                   for code in codes):
                raise AssertionError("predecessor callable is outside the staged original")
        self.calls: list[str] = []
        self.lock_frames: list[FrameType] = []
        self.snapshots: list[dict] = []
        self.store: Any = None
        self.error: BaseException | None = None
        self.frame: FrameType | None = None

    def trace(self, frame: FrameType, event: str, value: Any, resolved: Any) -> bool:
        code, parent = frame.f_code, frame.f_back
        if code is self.binding and event == "call":
            if (
                self.calls or parent is None or parent.f_code is not self.setup
                or resolved is None or parent.f_locals["resolved"] is not resolved
                or frame.f_locals["user_data"] != self.user_data or self.user_data.exists()
                or any(frame.f_locals[key] is not parent.f_locals[key]
                       for key in ("runtime", "catalog", "profile_id"))
            ):
                raise AssertionError("original Setup did not enter predecessor with the resolved Pack")
            self.frame = frame
            self.calls.append("call-pack-pin-predecessor")
            frame.f_trace_lines = True
            return True
        if code is self.inactive and event == "call":
            if (
                parent is None or parent.f_code is not self.setup
                or self.calls[-1:] != ["return-pack-pin-predecessor-none"]
                or parent.f_locals["pin_predecessor"] is not None
            ):
                raise AssertionError("original Setup passed an unobserved Pack pin predecessor")
            self.calls.append("before-inactive-predecessor")
            raise _BeforeInactivePredecessor
        if self.frame is None or code not in (
            self.binding, self.snapshot, self.get_profile, self.lock, self.load,
        ):
            return False
        if len(self.calls) > 16:
            raise AssertionError("original predecessor observation exceeds its bound")
        local = frame.f_locals
        if code is self.snapshot and event == "call":
            if parent is self.frame:
                self.store = local["self"]
                if self.store is not parent.f_locals["definitions"]:
                    raise AssertionError("predecessor did not snapshot its registry store")
                self.calls.append("call-registry-snapshot")
            elif parent is not None and parent.f_code is self.get_profile:
                if local["self"] is not self.store or parent.f_back is not self.frame:
                    raise AssertionError("registry lookup did not take its own snapshot")
                self.calls.append("call-lookup-snapshot")
            else:
                raise AssertionError("unexpected snapshot in original predecessor")
        elif code is self.lock and event == "return":
            # A generator reports a return at its yield, then again after its cleanup.
            held = frame not in self.lock_frames
            if (
                local["directory"] is not self.store._directory
                or local["directory"].root / local["relative_path"] != self.lock_path
                or local["acquired"] is not True
                or local["process_lock"]._is_owned() != held
            ):
                raise AssertionError("original snapshot did not acquire and release the registry lock")
            if held:
                self.lock_frames.append(frame)
                self.calls.append("acquire-registry-lock")
            else:
                self.calls.append("release-registry-lock")
        elif code is self.snapshot and event == "return":
            if (
                value["generation"] != 0 or value["profiles"]
                or self.snapshots and (value is self.snapshots[0] or value != self.snapshots[0])
            ):
                raise AssertionError("original snapshots did not independently observe an empty registry")
            self.calls.append("return-lookup-snapshot" if self.snapshots else "return-registry-snapshot")
            self.snapshots.append(value)
        elif code is self.get_profile and event == "call":
            if (
                parent is not self.frame or local["self"] is not self.store
                or local["profile_id"] != parent.f_locals["profile_id"]
                or local["include_tombstone"] is not False
            ):
                raise AssertionError("predecessor did not look up its Profile in the same registry")
            self.calls.append("call-registry-get-profile")
        elif code is self.get_profile and event == "return":
            if value is not None:
                raise AssertionError("original lookup did not return an absent definition")
            self.calls.append("return-registry-get-profile-none")
        elif code is self.load and event == "call":
            if (
                parent is not self.frame or local["self"].path != self.pointer_path
                or local["verify_snapshot"] is not True
                or parent.f_locals["registered"] is not None or self.pointer_path.exists()
            ):
                raise AssertionError("predecessor did not request the actual verified active pointer")
            self.calls.append("call-active-pointer-load-verified")
            if self.corrupt:
                # Candidate selection must see the fresh state; only this read sees the bad JSON.
                with self.pointer_path.open("xb") as stream:
                    stream.write(CORRUPT_POINTER)
        elif code is self.load and event == "exception":
            if type(value[1]) is self.integrity_error:
                self.error = value[1]
                self.calls.append("deny-active-pointer-load")
        elif code is self.load and event == "return" and self.error is None:
            if value is not None:
                raise AssertionError("original fresh pointer load did not return None")
            self.calls.append("return-active-pointer-none")
        elif code is self.binding and event == "line" and "source" in local:
            if "lookup-catalog-source" not in self.calls:
                if (
                    self.corrupt or local["pointer"] is not None or local["source"] is None
                    or local["source"] is not local["catalog"].profiles[local["profile_id"]]
                ):
                    raise AssertionError("predecessor did not look up the live catalog source")
                self.calls.append("lookup-catalog-source")
        elif code is self.binding and event == "exception" and value[1] is self.error:
            self.calls.append("deny-pack-pin-predecessor")
        elif code is self.binding and event == "return":
            if self.error is None:
                if value is not None:
                    raise AssertionError("original fresh predecessor did not return None")
                self.calls.append("return-pack-pin-predecessor-none")
            self.frame = None
        return True

    def check(self, outcome: dict) -> list[str]:
        expected = [
            "call-pack-pin-predecessor", "call-registry-snapshot", "acquire-registry-lock",
            "release-registry-lock", "return-registry-snapshot", "call-registry-get-profile",
            "call-lookup-snapshot", "acquire-registry-lock", "release-registry-lock",
            "return-lookup-snapshot", "return-registry-get-profile-none",
            "call-active-pointer-load-verified",
        ]
        if self.corrupt:
            expected.extend(("deny-active-pointer-load", "deny-pack-pin-predecessor"))
            error = outcome["error"]
            canonical = error["cause"]
            decode = canonical["cause"]
            message = "Expecting property name enclosed in double quotes: line 2 column 1 (char 2)"
            if (
                error["type"] != "core_runtime.active_profile_store_v4.ActiveProfileStoreIntegrityError"
                or error["message"] != "active Profile pointer is unavailable"
                or canonical["type"] != "tobkiri_protocol.errors.CanonicalizationError"
                or canonical["message"] != f"invalid JSON: {message}"
                or decode["type"] != "json.decoder.JSONDecodeError"
                or decode["message"] != message
                or self.pointer_path.read_bytes() != CORRUPT_POINTER
            ):
                raise AssertionError("unexpected original corrupt pointer error or file")
        else:
            expected.extend((
                "return-active-pointer-none", "lookup-catalog-source",
                "return-pack-pin-predecessor-none", "before-inactive-predecessor",
            ))
            if outcome != {"outcome": "no-predecessor"}:
                raise AssertionError("unexpected original fresh predecessor outcome")
        if self.calls != expected or len(self.lock_frames) != 2:
            raise AssertionError(("original predecessor call order changed", self.calls))
        paths = sorted(self.pointer_path.parent.iterdir())
        if (
            list(self.user_data.iterdir()) != [self.pointer_path.parent]
            or paths != [self.lock_path, *([self.pointer_path] if self.corrupt else [])]
            or any(path.is_symlink() or not path.is_file() for path in paths)
        ):
            raise AssertionError("original predecessor produced unexpected state")
        return [path.relative_to(self.user_data).as_posix() for path in paths]


def _observe(
    case: str, runtime: Path, user_data: Path, variant: dict, tree_files: list[str],
) -> dict:
    from core_runtime.bootstrap import profile_capture as capture
    from core_runtime.profile_runtime_port import require_profile_runtime
    from ecosystem.defaultspack.domain import profile_resolution
    from tobkiri_protocol import platform_artifact as original
    from tobkiri_protocol.canonical import canonical_digest

    root = runtime / ARTIFACT_ROOT
    parked = root.with_name("reference-parked-platform-artifacts")
    artifact = root / variant["relative_path"]
    entrypoint = root / variant["entrypoint"]
    moved: tuple[Path, Path] | None = None
    changed_entrypoint: bytes | None = None
    verifier = original.verify_platform_artifact.__code__
    pack = profile_resolution.resolve_default_profile.__code__
    validate = profile_resolution.validate_document.__code__
    setup = capture._resolve_bootstrap_candidate.__code__
    review = capture.prepare_bootstrap_profile_review.__code__
    port = require_profile_runtime()
    resolve_port = type(port).resolve_profile.__code__
    if any(Path(code.co_filename).resolve() != runtime / source for code, source in (
        (verifier, "tobkiri_protocol/platform_artifact.py"),
        (pack, "ecosystem/defaultspack/domain/profile_resolution.py"),
        (validate, "tobkiri_protocol/validation.py"),
        (setup, "core_runtime/bootstrap/profile_capture.py"),
        (resolve_port, "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py"),
    )):
        raise AssertionError("Setup, Pack or Shell callable is outside the staged original")
    reader = inspect.unwrap(original._regular_binary_reader).__code__
    tree = original.artifact_digest.__code__
    paths = {
        Path.resolve.__code__: "resolve",
        Path.is_symlink.__code__: "is-symlink",
        Path.is_dir.__code__: "is-dir",
        Path.is_file.__code__: "is-file",
        Path.iterdir.__code__: "iterdir",
        Path.read_bytes.__code__: "read-bytes",
    }
    calls: list[list[str]] = []
    reads: list[dict] = []
    active = False
    predecessor = (
        _PredecessorObservation(runtime, user_data, corrupt=case == PREDECESSOR_CASES[1])
        if case in PREDECESSOR_CASES else None
    )
    composition = case in COMPOSITION_CASES or predecessor is not None
    verified = False
    composition_calls: list[str] = []
    resolve_inputs: dict = {}
    facts: dict = {}
    validated: dict[str, dict] = {}
    result: Any = None
    pack_error: BaseException | None = None

    def record(operation: str, path: Path) -> None:
        if len(calls) >= 128:
            raise AssertionError("original Shell IO observation exceeds its bound")
        calls.append([operation, path.relative_to(runtime).as_posix()])

    def trace(frame: FrameType, event: str, value: Any) -> Any:
        nonlocal active, moved, changed_entrypoint
        nonlocal verified, result, pack_error
        code = frame.f_code
        parent = frame.f_back
        if event == "call":
            frame.f_trace_lines = False
        if predecessor is not None and predecessor.trace(frame, event, value, result):
            return trace
        if event == "call":
            if composition and code is resolve_port:
                local = frame.f_locals
                if (
                    composition_calls or parent is None or parent.f_code is not setup
                    or parent.f_back is None or parent.f_back.f_code is not review
                    or local["self"] is not port
                ):
                    raise AssertionError("original Setup did not call the installed Pack Port")
                owner = parent.f_locals
                kwargs = local["kwargs"]
                if (
                    local["catalog"] is not owner["catalog"]
                    or local["profile_id"] is not owner["profile_id"]
                    or kwargs["approved_artifact_digests"] is not owner["verified_artifacts"]
                    or kwargs["authority_bindings"] is not owner["authority_bindings"]
                    or kwargs["authority_snapshot_digest"] is not owner["snapshot_digest"]
                    or kwargs["additional_pack_ids"] is not owner["additional_pack_ids"]
                    or kwargs["security_epoch"] != 1
                    or kwargs["additional_pack_ids"] != ()
                ):
                    raise AssertionError("original Setup resolve inputs were not retained")
                catalog = local["catalog"]
                source = catalog.profiles[local["profile_id"]]
                [application] = [
                    pack for pack in source["packs"] if pack["pack_id"] == APPLICATION
                ]
                if application["artifact_digest"] != (
                    STALE_PIN if case == PACK_STALE_PIN else None
                ):
                    raise AssertionError("original Setup saw a different Application pin")
                resolve_inputs.update(
                    profile_id=local["profile_id"],
                    profile_definition_digest=canonical_digest(source),
                    bundle_digest=owner["bundle_lock_digest"],
                    authority_snapshot_digest=kwargs["authority_snapshot_digest"],
                    security_epoch=kwargs["security_epoch"],
                    additional_pack_ids=list(kwargs["additional_pack_ids"]),
                    approved_artifact_count=len(kwargs["approved_artifact_digests"]),
                    authority_binding_count=len(kwargs["authority_bindings"]),
                    application_requested_pin=application["artifact_digest"],
                    application_artifact_digest=catalog.packs[APPLICATION]["pack"]["artifact_digest"],
                )
                composition_calls.append("call-resolve-port")
                return trace
            if composition and code is pack:
                if (
                    composition_calls != ["call-resolve-port"]
                    or parent is None or parent.f_code is not resolve_port
                ):
                    raise AssertionError("original Port did not call the pinned Pack resolver")
                forwarded = parent.f_locals
                local = frame.f_locals
                if (
                    any(local[name] is not forwarded[name]
                        for name in ("catalog", "profile_id"))
                    or any(local[name] is not arg for name, arg in forwarded["kwargs"].items())
                ):
                    raise AssertionError("original resolve Port changed Pack inputs")
                composition_calls.append("call-resolve-pack")
                return trace
            if composition and code is validate and parent is not None and parent.f_code is pack:
                if not verified:
                    raise AssertionError("original Pack validated a result before Shell verification")
                return trace
            if code is verifier:
                local = frame.f_locals
                if (
                    active or parent is None or parent.f_code is not pack
                    or local["artifact_root"] is not parent.f_locals["catalog"].artifact_root
                    or local["artifact_root"] != root
                    or local["variant"] is not parent.f_locals["selected_variant"]
                    or local["variant"] != variant
                    or local["require_macos_code_signature"] is not False
                    or local["deadline_monotonic"] is not None
                ):
                    raise AssertionError("actual Pack did not forward its selected Shell")
                active = True
                if case in {"missing-artifact-root", "symlink-artifact-root"}:
                    moved = (root, parked)
                elif case in {"missing-selected-artifact", "symlink-selected-artifact"}:
                    moved = (artifact, parked)
                if moved is not None:
                    path, holding = moved
                    directory = path.is_dir()
                    path.rename(holding)
                    if case.startswith("symlink-"):
                        target = holding.relative_to(path.parent) if path == root else (
                            Path("..") / holding.name
                        )
                        path.symlink_to(target, target_is_directory=directory)
                return trace
            if active and code is reader:
                return trace
            if active and code is tree and parent is not None and parent.f_code is verifier:
                if frame.f_locals["path"] != artifact:
                    raise AssertionError("tree entry did not reuse the selected Shell")
                if case in {"symlink-at-tree-entry", "missing-at-tree-entry"}:
                    if moved is not None:
                        raise AssertionError("the selected Shell was already moved")
                    directory = artifact.is_dir()
                    artifact.rename(parked)
                    moved = (artifact, parked)
                    if case == "symlink-at-tree-entry":
                        artifact.symlink_to(
                            Path("..") / parked.name, target_is_directory=directory,
                        )
                return trace
            if (
                active and code in paths and parent is not None
                and parent.f_code.co_filename == verifier.co_filename
            ):
                if code is Path.resolve.__code__ and frame.f_locals["strict"] is not True:
                    raise AssertionError("original Shell used a non-strict path resolution")
                record(paths[code], frame.f_locals["self"])
            return None
        if composition and code is pack and event == "exception":
            _, pack_error, _ = value
        if composition and code is validate and event == "return" and value is not None:
            schema_name = frame.f_locals["schema_name"]
            if schema_name in validated:
                raise AssertionError("original Pack validated a result schema twice")
            validated[schema_name] = value
        if composition and code is pack and event == "return":
            if value is None:
                composition_calls.append("deny-resolve-pack")
            else:
                if not verified or case == PACK_STALE_PIN:
                    raise AssertionError("original Pack resolved without the expected inputs")
                result = value
                facts.update(_resolved_facts(result, frame.f_locals, variant))
                expected_records = (
                    ("profile_v5.schema.json", result.profile),
                    ("resolved_plan_v2.schema.json", result.plan),
                    ("profile_lock_v5.schema.json", result.lock),
                )
                if list(validated) != [name for name, _ in expected_records] or any(
                    validated[name] is not record for name, record in expected_records
                ):
                    raise AssertionError("original Pack did not return the validated records")
                facts["validated_schema_order"] = list(validated)
                composition_calls.append("return-resolve-pack")
        if composition and code is resolve_port and event == "return":
            if value is None and pack_error is not None:
                composition_calls.append("deny-resolve-port")
            else:
                if result is None or value is not result:
                    raise AssertionError("original Port did not return the same Pack result")
                facts["wrapper"]["same_port_result"] = True
                composition_calls.append("return-resolve-port")
                if predecessor is None:
                    raise _PackResolved
        if event == "return" and code is tree and case.startswith("entrypoint-"):
            if value != variant["artifact_digest"] or moved is not None:
                raise AssertionError("entrypoint mutation preceded the original tree digest")
            if case == "entrypoint-bytes-after-tree":
                changed_entrypoint = entrypoint.read_bytes()
                entrypoint.write_bytes(b"modified after the original tree digest")
            else:
                holding = entrypoint.with_name("reference-parked-entrypoint")
                entrypoint.rename(holding)
                moved = (entrypoint, holding)
                if case == "entrypoint-symlink-after-tree":
                    entrypoint.symlink_to(holding.name)
        if event == "return" and code is reader:
            local = frame.f_locals
            path, header = local["path"], local["header"]
            if value is not None:
                handle, size = value
                if handle.closed or size != local["before"].st_size:
                    raise AssertionError("original verifier did not yield an open regular file")
                record("reader-open-header" if header else "reader-open-member", path)
            elif "after" in local:
                if not local["handle"].closed:
                    raise AssertionError("original verifier retained the file after reading")
                record("reader-closed", path)
                reads.append({
                    "path": path.relative_to(root).as_posix(),
                    "header": header,
                    "bytes": local["before"].st_size,
                    "stable": original._identity(local["before"]) == original._identity(local["after"]),
                })
        if event == "return" and code is verifier and value is not None:
            if value != root / variant["relative_path"]:
                raise AssertionError("original verifier returned a different Shell")
            if composition:
                composition_calls.append("return-shell-verifier")
                verified = True
            else:
                raise _ShellVerified
        return trace

    previous = sys.gettrace()
    with (
        _composition_input(case, runtime),
        patch.dict(os.environ, {
            "TOBKIRI_USER_DATA": str(user_data), "RUMI_USER_DATA": str(user_data),
            "RUMI_ENVIRONMENT": "production", "RUMI_APP_DIR": "",
        }),
    ):
        try:
            sys.settrace(trace)
            capture.prepare_bootstrap_profile_review()
        except _ShellVerified:
            outcome = {"outcome": "verified"}
        except _PackResolved:
            outcome = {"outcome": "resolved", **facts}
        except _BeforeInactivePredecessor:
            outcome = {"outcome": "no-predecessor"}
        except Exception as error:
            if predecessor is not None:
                if error is not predecessor.error:
                    raise
            elif composition and (case != PACK_STALE_PIN or error is not pack_error):
                raise
            outcome = {"outcome": "error", "error": error_graph(error, runtime)}
        else:
            raise AssertionError("Setup proceeded past the observed original return")
        finally:
            sys.settrace(previous)
            if moved is not None and moved[1].exists():
                path, holding = moved
                if path.is_symlink():
                    path.unlink()
                holding.rename(path)
            if changed_entrypoint is not None:
                entrypoint.write_bytes(changed_entrypoint)

    if predecessor is not None:
        resolved = {"outcome": "resolved", **facts}
        _check("selected-shell", variant, tree_files, calls, reads, {"outcome": "verified"})
        _check_composition(case, composition_calls, resolve_inputs, resolved)
        return {
            "case": case,
            "calls": predecessor.calls,
            "composition_calls": composition_calls,
            "inputs": resolve_inputs,
            "files_after": predecessor.check(outcome),
            "expected": outcome,
        }
    if not active or user_data.exists():
        raise AssertionError("Shell reference missed the original call or wrote user state")
    if composition:
        _check("selected-shell", variant, tree_files, calls, reads, {"outcome": "verified"})
        if case == PACK_STALE_PIN and validated:
            raise AssertionError("stale Application pin reached original result validation")
        _check_composition(case, composition_calls, resolve_inputs, outcome)
        return {
            "case": case,
            "calls": composition_calls,
            "inputs": resolve_inputs,
            "user_data_absent_after": True,
            "expected": outcome,
        }
    _check(case, variant, tree_files, calls, reads, outcome)
    if case == "missing-artifact-root":
        error = outcome["error"]
        for field in ("message", "filename"):
            error[field] = error[field].replace("$USER_DATA", "$RUNTIME")
    return {"case": case, "calls": calls, "reads": reads, "expected": outcome}


def _check_composition(case: str, calls: list[str], inputs: dict, outcome: dict) -> None:
    start = ["call-resolve-port", "call-resolve-pack", "return-shell-verifier"]
    if case == PACK_STALE_PIN:
        expected = {
            "outcome": "error",
            "error": {
                "id": 0,
                "type": DENIED_CLASS,
                "message": f"Profile Pack artifact pin for {APPLICATION} is stale or mismatched",
                "suppressed": False,
                "cause": None,
                "context": None,
            },
        }
        if calls != [*start, "deny-resolve-pack", "deny-resolve-port"] or outcome != expected:
            raise AssertionError((case, calls, outcome))
        if inputs["application_artifact_digest"] == inputs["application_requested_pin"]:
            raise AssertionError("stale Application pin unexpectedly names the real artifact")
    else:
        if (
            calls != [*start, "return-resolve-pack", "return-resolve-port"]
            or outcome.get("outcome") != "resolved"
            or outcome["application"]["pack_id"] != APPLICATION
            or outcome["application"]["artifact_digest"] != inputs["application_artifact_digest"]
            or any(outcome["identity"][key] != inputs[key] for key in (
                "profile_id", "profile_definition_digest", "bundle_digest", "security_epoch",
            ))
            or outcome["identity"]["profile_authority_snapshot_digest"]
            != inputs["authority_snapshot_digest"]
            or outcome["record_counts"]["bindings"] != inputs["authority_binding_count"]
        ):
            raise AssertionError((case, calls, inputs, outcome))


def _check(
    case: str, variant: dict, tree_files: list[str], calls: list[list[str]],
    reads: list[dict], outcome: dict,
) -> None:
    root_resolve = ["resolve", ARTIFACT_ROOT]
    root_symlink = ["is-symlink", ARTIFACT_ROOT]
    root_checks = [root_resolve, root_symlink, ["is-dir", ARTIFACT_ROOT]]
    artifact = f"{ARTIFACT_ROOT}/{variant['relative_path']}"
    leaf_symlink = ["is-symlink", artifact]
    entrypoint = variant["entrypoint"]
    if case == "selected-shell":
        resolutions = [
            root_resolve,
            ["resolve", f"{ARTIFACT_ROOT}/{entrypoint}"],
            ["resolve", f"{ARTIFACT_ROOT}/{variant['relative_path']}"],
        ]
        expected_reads = [(name, False) for name in [*tree_files, entrypoint]]
        expected_reads.append((entrypoint, True))
        expected_open_sequence = [
            row
            for name, header in expected_reads
            for row in (
                ["reader-open-header" if header else "reader-open-member",
                 f"{ARTIFACT_ROOT}/{name}"],
                ["reader-closed", f"{ARTIFACT_ROOT}/{name}"],
            )
        ]
        if variant["platform"] == "macos":
            plist = ["read-bytes", f"{ARTIFACT_ROOT}/{tree_files[0]}"]
            expected_open_sequence.insert(-2, plist)
        if (
            outcome != {"outcome": "verified"}
            or calls[:6] != [*root_checks, leaf_symlink, root_symlink, leaf_symlink]
            or [row for row in calls if row[0] == "resolve"] != resolutions
            or [(row["path"], row["header"]) for row in reads] != expected_reads
            or not all(row["stable"] for row in reads)
            or [row for row in calls if row[0].startswith("reader-") or row[0] == "read-bytes"]
            != expected_open_sequence
        ):
            raise AssertionError((case, outcome, calls, reads))
    elif case.startswith("entrypoint-"):
        _check_entrypoint(case, variant, tree_files, calls, reads, outcome)
    else:
        error = outcome.get("error", {})
        if case == "missing-artifact-root":
            expected = (
                calls == [root_resolve]
                and error.get("type") == "builtins.FileNotFoundError"
                and error.get("errno") == 2
                and error.get("filename") == f"$USER_DATA/{ARTIFACT_ROOT}"
                and error.get("cause") is None and error.get("context") is None
            )
        else:
            cause = error.get("cause") or {}
            wanted, reason = {
                "symlink-artifact-root": (
                    [root_resolve, root_symlink],
                    "packaged artifact root must be a real directory",
                ),
                "symlink-selected-artifact": (
                    [*root_checks, leaf_symlink],
                    "packaged artifact path contains a symlink",
                ),
                "missing-selected-artifact": (
                    [*root_checks, leaf_symlink, root_symlink,
                     leaf_symlink, ["is-file", artifact], ["is-dir", artifact]],
                    "packaged artifact entry is unavailable",
                ),
                "symlink-at-tree-entry": (
                    [*root_checks, leaf_symlink, root_symlink, leaf_symlink],
                    "packaged artifact tree contains a symlink",
                ),
                "missing-at-tree-entry": (
                    [*root_checks, leaf_symlink, root_symlink,
                     leaf_symlink, ["is-file", artifact], ["is-dir", artifact]],
                    "packaged artifact entry is unavailable",
                ),
            }[case]
            expected = (
                calls == wanted
                and error.get("type") == DENIED_CLASS
                and error.get("message") == f"verified Shell artifact rejected: {reason}"
                and error.get("suppressed") is True
                and cause.get("type") == "tobkiri_protocol.errors.ProtocolError"
                and cause.get("message") == reason
                and error.get("context") == {"ref": cause["id"]}
            )
        if outcome.get("outcome") != "error" or not expected or reads:
            raise AssertionError((case, outcome, calls, reads))


def _check_entrypoint(
    case: str, variant: dict, tree_files: list[str], calls: list[list[str]],
    reads: list[dict], outcome: dict,
) -> None:
    entrypoint = f"{ARTIFACT_ROOT}/{variant['entrypoint']}"
    artifact = f"{ARTIFACT_ROOT}/{variant['relative_path']}"
    entry_checks = {
        "entrypoint-missing-after-tree": (
            [["resolve", entrypoint]], "packaged artifact entrypoint is missing or outside artifact",
        ),
        "entrypoint-symlink-after-tree": (
            [["resolve", entrypoint], ["resolve", artifact], ["is-symlink", entrypoint]],
            "packaged artifact entrypoint is not a regular file",
        ),
        "entrypoint-bytes-after-tree": (
            [["resolve", entrypoint], ["resolve", artifact], ["is-symlink", entrypoint],
             ["is-file", entrypoint], ["reader-open-member", entrypoint],
             ["reader-closed", entrypoint]],
            "packaged artifact entrypoint digest does not match",
        ),
    }
    expected, reason = entry_checks[case]
    repeated_read = case == "entrypoint-bytes-after-tree"
    expected_reads = [*tree_files, *([variant["entrypoint"]] if repeated_read else [])]
    error = outcome.get("error", {})
    cause = error.get("cause") or {}
    inner = cause.get("cause") or {}
    if (
        outcome.get("outcome") != "error"
        or error.get("type") != DENIED_CLASS
        or error.get("message") != f"verified Shell artifact rejected: {reason}"
        or error.get("context") != {"ref": cause.get("id")}
        or not error.get("suppressed")
        or cause.get("type") != "tobkiri_protocol.errors.ProtocolError"
        or cause.get("message") != reason
        or (case == "entrypoint-missing-after-tree" and (
            inner.get("type") != "builtins.FileNotFoundError" or inner.get("errno") != 2
            or inner.get("filename") != "$USER_DATA/" + entrypoint
            or cause.get("context") != {"ref": inner.get("id")}
        ))
        or (case != "entrypoint-missing-after-tree" and inner)
        or calls[-len(expected):] != expected
        or [row["path"] for row in reads] != expected_reads
        or not all(row["stable"] and not row["header"] for row in reads)
    ):
        raise AssertionError((case, outcome, calls, reads))


def observe_shell(original: Path, runtime: Path, output: Path) -> None:
    """Observe the selected Shell, full Pack return, and first predecessor reads."""
    variant, tree_files = _package(original, runtime, output)
    rows = [
        _observe(case, runtime, output / case / "user-data", variant, tree_files)
        for case in CASES
    ]
    composition = [
        _observe(case, runtime, output / case / "user-data", variant, tree_files)
        for case in COMPOSITION_CASES
    ]
    predecessors = [
        _observe(case, runtime, output / case / "user-data", variant, tree_files)
        for case in PREDECESSOR_CASES
    ]
    write_json(output / "shell.json", {
        "format": FORMAT, "original_head": ORIGINAL_HEAD,
        "variant": variant, "cases": rows, "composition_cases": composition,
        "predecessor_cases": predecessors,
        "header_fixture": True, "qualification": False, "production_enabled": False,
    })
