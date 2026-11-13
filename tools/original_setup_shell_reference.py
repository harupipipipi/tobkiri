"""Record actual Shell IO in a pinned, staged original Setup review."""

from __future__ import annotations

import inspect
import os
import plistlib
from pathlib import Path
import subprocess
import sys
from types import FrameType
from typing import Any
from unittest.mock import patch

from original_authority_reference import ORIGINAL_HEAD, error_graph, write_json
from original_catalog_prefix_reference import bind_resources
from original_setup_candidate_selection_reference import DENIED_CLASS

FORMAT = "io.tobkiri.original-setup-shell-reference.v1"
CASES = ("selected-shell", "missing-artifact-root", "symlink-artifact-root")
ARTIFACT_ROOT = "ecosystem/defaultspack/platform-artifacts"


class _ShellVerified(BaseException):
    """Stop after the original verifier returns, before Setup publishes anything."""


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
    bundle = runtime / "ecosystem/defaultspack/v4"
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


def _observe(
    case: str, runtime: Path, user_data: Path, variant: dict, tree_files: list[str],
) -> dict:
    from core_runtime.bootstrap.profile_capture import prepare_bootstrap_profile_review
    from ecosystem.defaultspack.domain.profile_resolution import resolve_default_profile
    from tobkiri_protocol import platform_artifact as original

    root = runtime / ARTIFACT_ROOT
    parked = root.with_name("reference-parked-platform-artifacts")
    verifier = original.verify_platform_artifact.__code__
    pack = resolve_default_profile.__code__
    if Path(verifier.co_filename).resolve() != runtime / "tobkiri_protocol/platform_artifact.py":
        raise AssertionError("Shell verifier is outside the staged original")
    reader = inspect.unwrap(original._regular_binary_reader).__code__
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
    root_moved = False

    def record(operation: str, path: Path) -> None:
        if len(calls) >= 128:
            raise AssertionError("original Shell IO observation exceeds its bound")
        calls.append([operation, path.relative_to(runtime).as_posix()])

    def trace(frame: FrameType, event: str, value: Any) -> Any:
        nonlocal active, root_moved
        code = frame.f_code
        if event == "call":
            frame.f_trace_lines = False
            parent = frame.f_back
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
                if case != "selected-shell":
                    root.rename(parked)
                    root_moved = True
                    if case == "symlink-artifact-root":
                        root.symlink_to(parked.name, target_is_directory=True)
                return trace
            if active and code is reader:
                return trace
            if (
                active and code in paths and parent is not None
                and parent.f_code.co_filename == verifier.co_filename
            ):
                if code is Path.resolve.__code__ and frame.f_locals["strict"] is not True:
                    raise AssertionError("original Shell used a non-strict path resolution")
                record(paths[code], frame.f_locals["self"])
            return None
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
            raise _ShellVerified
        return trace

    previous = sys.gettrace()
    with patch.dict(os.environ, {
        "TOBKIRI_USER_DATA": str(user_data), "RUMI_USER_DATA": str(user_data),
        "RUMI_ENVIRONMENT": "production", "RUMI_APP_DIR": "",
    }):
        try:
            sys.settrace(trace)
            prepare_bootstrap_profile_review()
        except _ShellVerified:
            outcome = {"outcome": "verified"}
        except Exception as error:
            outcome = {"outcome": "error", "error": error_graph(error, runtime)}
        else:
            raise AssertionError("Setup proceeded past the original Shell boundary")
        finally:
            sys.settrace(previous)
            if root_moved:
                if root.is_symlink():
                    root.unlink()
                parked.rename(root)

    if not active or user_data.exists():
        raise AssertionError("Shell reference missed the original call or wrote user state")
    _check(case, variant, tree_files, calls, reads, outcome)
    if case == "missing-artifact-root":
        error = outcome["error"]
        for field in ("message", "filename"):
            error[field] = error[field].replace("$USER_DATA", "$RUNTIME")
    return {"case": case, "calls": calls, "reads": reads, "expected": outcome}


def _check(
    case: str, variant: dict, tree_files: list[str], calls: list[list[str]],
    reads: list[dict], outcome: dict,
) -> None:
    root_resolve = ["resolve", ARTIFACT_ROOT]
    root_symlink = ["is-symlink", ARTIFACT_ROOT]
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
            or calls[:3] != [root_resolve, root_symlink, ["is-dir", ARTIFACT_ROOT]]
            or [row for row in calls if row[0] == "resolve"] != resolutions
            or [(row["path"], row["header"]) for row in reads] != expected_reads
            or not all(row["stable"] for row in reads)
            or [row for row in calls if row[0].startswith("reader-") or row[0] == "read-bytes"]
            != expected_open_sequence
        ):
            raise AssertionError((case, outcome, calls, reads))
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
            reason = "packaged artifact root must be a real directory"
            expected = (
                calls == [root_resolve, root_symlink]
                and error.get("type") == DENIED_CLASS
                and error.get("message") == f"verified Shell artifact rejected: {reason}"
                and error.get("suppressed") is True
                and cause.get("type") == "tobkiri_protocol.errors.ProtocolError"
                and cause.get("message") == reason
                and error.get("context") == {"ref": cause["id"]}
            )
        if outcome.get("outcome") != "error" or not expected or reads:
            raise AssertionError((case, outcome, calls, reads))


def observe_shell(original: Path, runtime: Path, output: Path) -> None:
    """Keep only bounded metadata from the original selected Shell verifier."""
    variant, tree_files = _package(original, runtime, output)
    rows = [
        _observe(case, runtime, output / case / "user-data", variant, tree_files)
        for case in CASES
    ]
    write_json(output / "shell.json", {
        "format": FORMAT, "original_head": ORIGINAL_HEAD,
        "variant": variant, "cases": rows,
        "header_fixture": True, "qualification": False, "production_enabled": False,
    })
