"""Observe the original catalog path up to ProfileDefinitionStore construction.

All execution is CI-only, with the pinned original interpreter and checkout.
Each staged runtime keeps the original Python bytes and real root getters.
Only resource fixture data changes; only the constructor call-site alias is
replaced by a BaseException sentinel. No catalog, validation or getter body is
replaced, and no constructor body, migration, approval or model is executed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from original_authority_reference import ORIGINAL_HEAD, error_graph, write_json
from original_publication_reference import filesystem, seed
from original_setup_review_reference import check_original


FORMAT = "io.tobkiri.original-catalog-prefix-reference.v1"
CASES = (
    "packaged-valid", "source-without-artifact-root", "manifest-missing",
    "manifest-invalid-json", "manifest-invalid-utf8", "manifest-wrong-schema",
    "manifest-crlf", "resource-unlisted", "development-bundled",
)
SOURCES = (
    "core_runtime/bootstrap/profile_capture.py",
    "core_runtime/bootstrap/profile_publication.py",
    "core_runtime/profile_runtime_port.py",
    "core_runtime/profile_definition_store_v4.py",
    "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
    "ecosystem/defaultspack/domain/profile_resolution.py",
    "tobkiri_protocol/bundle_catalog.py",
)
IGNORED = shutil.ignore_patterns(
    "__pycache__", "node_modules", ".venv", ".pytest_cache", "target", "webapp", "tests",
)


class ProfileDefinitionBoundary(BaseException):
    """Test-only reachpoint, never an original application error."""


def code_hashes(runtime: Path) -> dict[str, str]:
    return {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
            for name in SOURCES}


def bind_resources(runtime: Path) -> None:
    """Seed actual fixture bytes, not an observation or permission grant."""
    bundle = runtime / "ecosystem/defaultspack/v4"
    artifacts = bundle.parent / "platform-artifacts"
    artifacts.mkdir(exist_ok=False)
    entries = []
    for directory in (bundle, artifacts):
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                raw = path.read_bytes()
                entries.append({"path": path.relative_to(runtime).as_posix(),
                                "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    write_json(runtime / "runtime-resource-manifest.v1.json", {
        "schema": "io.tobkiri.runtime-resource-manifest.v1", "entries": entries,
    })


def mutate(runtime: Path, name: str) -> dict[str, str | None]:
    manifest = runtime / "runtime-resource-manifest.v1.json"
    bundle = runtime / "ecosystem/defaultspack/v4"
    env: dict[str, str | None] = {"RUMI_ENVIRONMENT": "production", "RUMI_APP_DIR": None}
    if name == "source-without-artifact-root":
        (bundle.parent / "platform-artifacts").rmdir()
    elif name == "manifest-missing":
        manifest.unlink()
    elif name == "manifest-invalid-json":
        manifest.write_bytes(b"{")
    elif name == "manifest-invalid-utf8":
        manifest.write_bytes(b"\xff")
    elif name == "manifest-wrong-schema":
        data = json.loads(manifest.read_text())
        data["schema"] = "unsupported.fixture"
        write_json(manifest, data)
    elif name == "manifest-crlf":
        manifest.write_bytes(manifest.read_bytes().replace(b"\n", b"\r\n"))
    elif name == "resource-unlisted":
        (bundle / "unlisted-fixture.txt").write_bytes(b"unlisted fixture\n")
    elif name == "development-bundled":
        target = runtime / "bundled/dev-defaults"
        target.mkdir(parents=True, exist_ok=False)
        bundle.rename(target / "v4")
        (bundle.parent / "platform-artifacts").rename(target / "platform-artifacts")
        env = {"RUMI_ENVIRONMENT": "development", "RUMI_APP_DIR": str(runtime)}
    elif name != "packaged-valid":
        raise ValueError("unknown closed catalog fixture")
    return env


def intrinsic_parse_errors(error: BaseException) -> list[dict]:
    """Read real parser fields without reconstructing an exception from text."""
    records, visited, pending = [], set(), [error]
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if len(visited) > 16:
            raise ValueError("catalog exception graph exceeds finite fixture bound")
        if type(current) is json.JSONDecodeError:
            records.append({"kind": "json-decode", "msg": current.msg,
                            "doc": current.doc, "pos": current.pos,
                            "lineno": current.lineno, "colno": current.colno})
        elif type(current) is UnicodeDecodeError:
            records.append({"kind": "unicode-decode", "encoding": current.encoding,
                            "object_hex": current.object.hex(), "start": current.start,
                            "end": current.end, "reason": current.reason})
        for link in (BaseException.__cause__, BaseException.__context__):
            child = link.__get__(current)
            if child is not None:
                pending.append(child)
    return records


def probe(original: Path, runtime: Path, user_data: Path, output: Path) -> None:
    check_original(original)
    expected = code_hashes(original / "tobkiri_runtime")
    if code_hashes(runtime) != expected:
        raise ValueError("staged original code bytes differ")
    sys.path.insert(0, str(runtime))
    from core_runtime.bootstrap import profile_capture as capture
    from core_runtime.profile_runtime_port import require_profile_runtime
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )

    if Path(capture.__file__).resolve() != runtime / SOURCES[0]:
        raise ValueError("catalog probe imported another source tree")
    install_defaultspack_profile_runtime()
    port = require_profile_runtime()
    original_store = capture.ProfileDefinitionStore
    observed = []
    identities = (capture.host_profile_catalog, capture._bundle_root,
                  capture._verify_installed_bundle_binding, capture._bootstrap_profile_id,
                  type(port).host_resource_root, type(port).bundled_profile_root,
                  type(port).load_catalog)

    def stop_definition(root):
        if root != user_data.resolve():
            raise AssertionError("constructor received a different user-data root")
        observed.append("profile-definition-store-entry")
        raise ProfileDefinitionBoundary

    before = filesystem(user_data)
    try:
        capture.ProfileDefinitionStore = stop_definition
        try:
            capture._recover_bootstrap_publication(discard_superseded=True)
        except ProfileDefinitionBoundary:
            result = {"outcome": "integration-boundary", "stage": "profile-definition-store-entry"}
        except BaseException as error:
            result = {"outcome": "error", "error": error_graph(error, runtime.parent),
                      "intrinsic_parse_errors": intrinsic_parse_errors(error)}
        else:
            raise AssertionError("recovery unexpectedly returned before the constructor stop")
    finally:
        capture.ProfileDefinitionStore = original_store
    if identities != (capture.host_profile_catalog, capture._bundle_root,
                      capture._verify_installed_bundle_binding, capture._bootstrap_profile_id,
                      type(port).host_resource_root, type(port).bundled_profile_root,
                      type(port).load_catalog):
        raise AssertionError("an original catalog/getter callable was replaced")
    if code_hashes(runtime) != expected or require_profile_runtime() is not port:
        raise AssertionError("original source or immutable runtime changed")
    if len(observed) > 1:
        raise AssertionError("constructor boundary repeated")
    write_json(output, {"expected": result, "constructor_entries": len(observed),
                        "constructor_body_executed": False, "source_hashes": expected,
                        "user_data_before": before, "user_data_after": filesystem(user_data)})


def prepare(original: Path, destination: Path) -> None:
    check_original(original)
    runtime = original / "tobkiri_runtime"
    expected = code_hashes(runtime)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    sys.path.insert(0, str(runtime))
    from core_runtime.bootstrap.profile_publication import ProfilePublicationJournal
    from core_runtime.active_profile_store_v4 import ActiveProfilePointer
    from core_runtime import hmac_key_manager as signing
    from tobkiri_protocol import canonical as protocol

    reports = []
    for name in CASES:
        directory = destination / name
        directory.mkdir(mode=0o700)
        seed_data = directory / "seed-user-data"
        seed("valid", seed_data, ProfilePublicationJournal, ActiveProfilePointer, protocol, signing)
        template = directory / "seed-runtime"
        shutil.copytree(runtime, template, ignore=IGNORED)
        bind_resources(template)
        for role in ("reference", "native"):
            target = directory / role
            target.mkdir(mode=0o700)
            shutil.copytree(template, target / "runtime")
            shutil.copytree(seed_data, target / "user-data")
            environment = mutate(target / "runtime", name)
            write_json(target / "input.json", {"case": name, "runtime": str(target / "runtime"),
                       "user_data": str(target / "user-data"), "environment": environment,
                       "resource_fixture_only": True, "platform_artifact_directory_only": True})
        # The staging template is solely owned CI fixture data. Keep only the
        # two independent input trees used by the original/native comparison.
        shutil.rmtree(template)
        reference = directory / "reference"
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                           TOBKIRI_USER_DATA=str(reference / "user-data"),
                           RUMI_USER_DATA=str(reference / "user-data"))
        for key, value in json.loads((reference / "input.json").read_text())["environment"].items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        output = reference / "observation.json"
        subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "probe",
                        "--original-root", str(original), "--runtime", str(reference / "runtime"),
                        "--user-data", str(reference / "user-data"), "--output", str(output)],
                       check=True, env=environment, timeout=180)
        observation = json.loads(output.read_text())
        reports.append({"name": name, "native_input": str(directory / "native/input.json"),
                        **observation})
    if code_hashes(runtime) != expected:
        raise AssertionError("independent original checkout code changed")
    by_name = {row["name"]: row for row in reports}
    boundary = {"outcome": "integration-boundary", "stage": "profile-definition-store-entry"}
    for name in ("packaged-valid", "manifest-crlf", "development-bundled"):
        if by_name[name]["expected"] != boundary or by_name[name]["constructor_entries"] != 1:
            raise AssertionError((name, "original loader did not reach the natural boundary",
                                  by_name[name]["expected"]))
    for name in ("manifest-missing", "manifest-invalid-json", "manifest-invalid-utf8",
                 "manifest-wrong-schema", "resource-unlisted"):
        if by_name[name]["expected"]["outcome"] != "error" or by_name[name]["constructor_entries"]:
            raise AssertionError((name, "invalid fixture passed the actual original loader"))
    check_original(original)
    write_json(destination / "summary.json", {"format": FORMAT, "original_head": ORIGINAL_HEAD,
               "python": "3.13.16", "cases": reports, "case_count": len(reports),
               "native_comparison": False, "qualification": False, "production_enabled": False,
               "scope": "staged original Python bytes and real getters/catalog; resource fixture inputs; "
                        "constructor call-site sentinel only; no constructor body, migrations, Authority, "
                        "actual installed executable, approval, GUI, native comparison or full goal"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "probe"))
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--user-data", type=Path)
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    if args.command == "prepare":
        prepare(original, args.output.resolve())
    else:
        if args.runtime is None or args.user_data is None:
            parser.error("probe requires --runtime and --user-data")
        probe(original, args.runtime.resolve(strict=True), args.user_data.resolve(strict=True), args.output)


if __name__ == "__main__":
    main()
