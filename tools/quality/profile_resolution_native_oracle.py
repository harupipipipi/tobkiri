"""CI-only old/new native oracle over one genuine, shared packaged fixture.

This is not a product launcher, an approval path, Wasm execution or GUI proof.
Native application imports occur only in supervised Linux CI child processes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import sysconfig

BASELINE = "e15caf5c9a3255a87b4b48dcc24617d305a3681b"
LIMIT = 4 * 1024 * 1024
DISK_FLOOR = 8 * 1024 * 1024 * 1024
_CHECKERS = {
    "core_runtime.pack_signature": {"verify_signed_pack", "verify_signed_manifest"},
    "tobkiri_host.artifact_compiler": {"compile_pack_root"},
    "core_runtime.pack_artifact_integrity": {"verify_host_install_binding"},
    "core_runtime.external_pack_catalog_v4": {
        "_read_authenticated_catalog",
        "_verify_entry_root",
        "resolve_admitted_pack_root",
    },
}


def require_ci() -> None:
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError(
            "native old/new application imports are restricted to Linux CI"
        )


def read(path: Path, limit: int = LIMIT) -> bytes:
    meta = path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > limit:
        raise ValueError("bounded regular oracle input required: " + str(path))
    raw = path.read_bytes()
    if len(raw) != meta.st_size:
        raise ValueError("oracle input changed during reading")
    return raw


def identity(path: Path) -> dict:
    meta = path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
        raise ValueError("oracle fixture contains a link or special file")
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    current = path.lstat()
    if (meta.st_ino, meta.st_size, meta.st_mtime_ns) != (
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ):
        raise ValueError("oracle fixture changed while hashed")
    return {"bytes": meta.st_size, "sha256": sha, "mode": stat.S_IMODE(meta.st_mode)}


def inventory(roots: list[Path]) -> dict:
    result = {}
    for ordinal, root in enumerate(roots):
        if root.is_symlink() or not root.is_dir():
            raise ValueError("physical fixture root required")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("oracle fixture symlink")
            if path.is_file():
                result[f"{ordinal}/" + path.relative_to(root).as_posix()] = identity(
                    path
                )
            elif not path.is_dir():
                raise ValueError("oracle fixture contains a special file")
    return result


def write_json(path: Path, value: object) -> None:
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    if len(raw) > LIMIT:
        raise ValueError("native observation exceeds the original JSON output bound")
    path.write_bytes(raw)


def clean_source(root: Path, expected: str | None = None) -> dict:
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("physical source checkout required")

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()

    head = git("rev-parse", "HEAD^{commit}")
    if (expected is not None and head != expected) or git(
        "status", "--porcelain=v1", "--untracked-files=all"
    ):
        raise ValueError("immutable clean oracle source required")
    return {
        "source_commit": head,
        "source_tree": git("rev-parse", "HEAD^{tree}"),
        "source_clean": True,
    }


def install_namespace(root: Path, dependency_paths: list[Path]) -> None:
    """Use a real selected runtime and installed dependencies, never module shims."""
    for path in dependency_paths:
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise ValueError("physical explicit dependency prefix required")
    runtime = root / "tobkiri_runtime"
    sys.path[:0] = [str(runtime), *(str(path) for path in dependency_paths)]


@contextmanager
def checker_calls():
    counts = Counter()
    previous = sys.getprofile()
    if previous is not None:
        raise RuntimeError("oracle requires an independent profiling namespace")

    def profile(frame, event, arg):
        if event == "call":
            module = frame.f_globals.get("__name__")
            name = frame.f_code.co_name
            if name in _CHECKERS.get(module, ()):
                counts[module + "." + name] += 1

    sys.setprofile(profile)
    try:
        yield counts
    finally:
        sys.setprofile(previous)


def configure_fixture(bundle: Path, *, install: bool) -> None:
    """Use only the existing packaged fixture's explicit composition providers."""
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )
    from core_runtime.bootstrap import profile_capture, runtime
    from tests.conformance_support.packaged_profile import (
        inject_packaged_profile_bundle,
    )

    if install:
        install_defaultspack_profile_runtime()

    def provider(_base_dir=None):
        return bundle

    profile_capture._bundle_root = provider
    runtime._bundle_root = provider
    inject_packaged_profile_bundle(bundle)


def prepare(root: Path, work: Path) -> None:
    """Generate once with the original source, real signatures and real admission."""
    source = clean_source(root, BASELINE)
    from tests.conformance_support.packaged_profile import (
        create_test_source_provenance,
        build_packaged_profile_bundle,
    )

    provenance = create_test_source_provenance(
        root / "tobkiri_runtime",
        work / "packaging",
        provenance_record=source,
    )
    bundle = build_packaged_profile_bundle(
        root / "tobkiri_runtime/ecosystem/defaultspack/v4",
        work / "packaged",
        source_provenance_file=provenance,
    )
    configure_fixture(bundle, install=True)
    from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
    from tests.test_external_pack_catalog_v4 import _signed_external_pack, PACK_ID
    import core_runtime.external_pack_catalog_v4 as catalog

    signed, trust = _signed_external_pack(work / "signed-input", with_frontend=True)
    with checker_calls() as calls:
        admitted = catalog.admit_signed_external_pack(signed, trust_store_path=trust)
    required = {
        "core_runtime.pack_signature.verify_signed_pack",
        "tobkiri_host.artifact_compiler.compile_pack_root",
        "core_runtime.pack_artifact_integrity.verify_host_install_binding",
    }
    if not all(calls[name] > 0 for name in required):
        raise AssertionError(
            "original admission did not execute its real checking chain"
        )
    cas = catalog.resolve_admitted_pack_root(
        PACK_ID, artifact_digest=admitted["artifact_digest"]
    )
    owner = Path(os.environ["TOBKIRI_USER_DATA"])
    artifacts = BundledCatalog.load(bundle).artifact_root
    if artifacts is None:
        raise AssertionError("original packaged fixture has no verified artifact root")
    manifest = {
        "format": "tobkiri.profile-resolution-oracle-input/1",
        "source": source,
        "bundle_root": str(bundle),
        "artifact_root": str(artifacts),
        "owner": str(owner),
        "cas_root": str(cas),
        "signed_source": str(signed),
        "trust_store": str(trust),
        "pack_id": PACK_ID,
        "artifact_digest": admitted["artifact_digest"],
        "catalog_path": str(catalog._catalog_path(owner)),
        "genuine_admission_checker_calls": dict(calls),
        "fixture_artifact_is_real_product_binary": False,
        "full_gui_or_activation_proof": False,
    }
    write_json(work / "inputs.json", manifest)


def result_of(function, expected_error: type[Exception] | None = None) -> dict:
    try:
        return {"outcome": "success", "value": function()}
    except Exception as error:
        if expected_error is not None and not isinstance(error, expected_error):
            raise
        return {
            "outcome": "exception",
            "type": type(error).__module__ + "." + type(error).__qualname__,
            "args": list(error.args),
        }


def nonempty_observation(inputs: dict, *, damaged: bool, encode) -> dict:
    """Exercise original nonempty admission, compiler, projection and resolver."""
    from cryptography.hazmat.primitives import serialization
    from core_runtime.pack_signature import verify_signed_pack, PackSignatureError
    from core_runtime.external_pack_catalog_v4 import ExternalPackCatalogDenied
    from core_runtime.profile_pack_projection import admitted_projection_sources
    from core_runtime.profile_content_projection import (
        resolve_profile_projection,
        selected_projection_roots,
    )
    from tobkiri_host.artifact_compiler import compile_pack_root
    from tobkiri_host.errors import InvalidArtifactError
    from tobkiri_protocol.validation import validate_file
    from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
    from tests.test_captured_setup_projection import _resolve, _projections

    cas = Path(inputs["cas_root"])
    pack_id, digest = inputs["pack_id"], inputs["artifact_digest"]
    selection = {
        "projection_id": "oracle.external",
        "kind": "profile_content",
        "artifact_root": "frontend",
        "source_pack_id": pack_id,
        "source_artifact_digest": digest,
        "content_digest": None,
    }
    pins = {pack_id: digest}

    def projected():
        sources = admitted_projection_sources([selection], pins)
        resolved = resolve_profile_projection(selection, pack_sources=sources)
        roots = selected_projection_roots([resolved], pack_sources=sources)
        if len(roots) != 1 or resolved["file_count"] <= 0:
            raise AssertionError("nonempty projection disappeared")
        return {
            "projection": resolved,
            "roots": [[name, str(path)] for name, path in roots],
        }

    def full_setup():
        catalog = BundledCatalog.load(Path(inputs["bundle_root"]))
        intent = json.loads(json.dumps(catalog.profiles["defaults"]))
        intent["packs"].append(
            {"pack_id": pack_id, "artifact_digest": digest, "role": "provider"}
        )
        intent["content_projections"] = [selection]
        manifest = validate_file(cas / "pack.v4.json", "pack")
        augmented = replace(
            catalog,
            packs={**catalog.packs, pack_id: manifest},
            profiles={**catalog.profiles, "defaults": intent},
        )
        output = _projections(augmented, _resolve(augmented))
        output["resolved"] = asdict(output["resolved"])
        return output

    def compiled():
        value = compile_pack_root(cas)
        return {
            "artifact": encode(value.artifact),
            "routes": [
                [encode(key), encode(item)] for key, item in value.routes.items()
            ],
        }

    with checker_calls() as calls:
        expected = ExternalPackCatalogDenied if damaged else None
        output = {
            "nonempty_projection": result_of(projected, expected),
            "complete_setup": result_of(full_setup, expected),
            "real_compiler": result_of(compiled, InvalidArtifactError),
        }
    for name in ("nonempty_projection", "complete_setup"):
        expected = "exception" if damaged else "success"
        if output[name]["outcome"] != expected:
            raise AssertionError(f"{name} did not {expected}: {output[name]}")
    if not damaged and output["real_compiler"]["outcome"] != "success":
        raise AssertionError(
            "the real compiler did not produce a complete valid artifact"
        )
    output["checker_calls"] = dict(calls)
    if not damaged and calls["tobkiri_host.artifact_compiler.compile_pack_root"] <= 0:
        raise AssertionError("nonempty resolver did not use the real compiler")
    if damaged:
        return output

    def rejected(function, expected_error):
        result = result_of(function, expected_error)
        if result["outcome"] != "exception":
            raise AssertionError("original negative case was accepted")
        return result

    sources = admitted_projection_sources([selection], pins)
    resolved = resolve_profile_projection(selection, pack_sources=sources)
    output["outside_closure"] = rejected(
        lambda: admitted_projection_sources([selection], {}),
        ValueError,
    )
    output["stale_pin"] = rejected(
        lambda: admitted_projection_sources(
            [selection], {pack_id: "sha256:" + "0" * 64}
        ),
        ExternalPackCatalogDenied,
    )
    output["unsafe_projection"] = rejected(
        lambda: resolve_profile_projection(
            {**selection, "artifact_root": "../frontend"}, pack_sources=sources
        ),
        ValueError,
    )
    output["duplicate_root"] = rejected(
        lambda: selected_projection_roots(
            [resolved, {**resolved, "projection_id": "oracle.alias"}],
            pack_sources=sources,
        ),
        ValueError,
    )
    signed_root = Path(inputs["signed_source"])
    signed = json.loads(read(signed_root / ".tobkiri/signed-pack.json"))
    trust = json.loads(read(Path(inputs["trust_store"])))
    pem = trust["publishers"][signed["publisher_id"]]["public_key_pem"]
    public = serialization.load_pem_public_key(pem.encode())
    output["publisher_mismatch"] = rejected(
        lambda: verify_signed_pack(
            signed_root, signed, public, expected_publisher_id="another.publisher"
        ),
        PackSignatureError,
    )
    output["revoked_key"] = rejected(
        lambda: verify_signed_pack(
            signed_root, signed, public, revoked_key_ids={signed["signature"]["key_id"]}
        ),
        PackSignatureError,
    )
    output["signature_mismatch"] = rejected(
        lambda: verify_signed_pack(signed_root, {**signed, "version": "9.9.9"}, public),
        PackSignatureError,
    )
    return output


def evaluate(
    root: Path,
    work: Path,
    output: Path,
    reverse: bool,
    legacy: Path | None,
    expected_pickle_sha: str | None,
    damaged: bool,
) -> None:
    inputs = json.loads(read(work / "inputs.json"))
    spec = importlib.util.spec_from_file_location(
        "native_resolution_observation",
        Path(__file__).with_name("profile_resolution_native_observation.py"),
    )
    observer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = observer
    spec.loader.exec_module(observer)
    prior = None
    if legacy is not None:
        prior = read(legacy)
        if hashlib.sha256(prior).hexdigest() != expected_pickle_sha:
            raise ValueError("trusted original pickle identity differs")
    observation, serialized = observer.observe(
        Path(inputs["bundle_root"]),
        legacy_pickle=prior,
        reverse_access=reverse,
    )
    configure_fixture(Path(inputs["bundle_root"]), install=False)
    observation["nonempty"] = nonempty_observation(
        inputs,
        damaged=damaged,
        encode=observer._json_value,
    )
    write_json(output, observation)
    if len(serialized) > LIMIT:
        raise ValueError("trusted original pickle exceeds its bound")
    output.with_suffix(".pickle").write_bytes(serialized)


def run(baseline: Path, candidate: Path, work: Path) -> None:
    require_ci()
    original = clean_source(baseline, BASELINE)
    current = clean_source(candidate, os.environ.get("GITHUB_SHA"))
    if (
        work.exists()
        or work.parent.resolve() != work.parent
        or shutil.disk_usage(work.parent).free < DISK_FLOOR
    ):
        raise ValueError(
            "fresh private CI work directory and original disk floor required"
        )
    same = [
        "tobkiri_runtime/requirements.txt",
        "tobkiri_runtime/requirements-dev.txt",
        "tobkiri_runtime/schemas/pack_v4_catalog.v1.json",
        "tobkiri_runtime/tests/test_captured_setup_projection.py",
        "tobkiri_runtime/tests/test_external_pack_catalog_v4.py",
        "tobkiri_runtime/tests/conformance_support/packaged_profile.py",
        "tobkiri_runtime/tests/test_activation_validation_cache_freshness.py",
        "tobkiri_runtime/tests/test_pack_signature.py",
        "tobkiri_runtime/tests/test_pack_authoring.py",
        "tobkiri_runtime/tests/test_bundled_host_baseline_v4.py",
    ]
    for name in same:
        if read(baseline / name) != read(candidate / name):
            raise ValueError(
                "independent original fixture/lock/catalog source changed: " + name
            )
    schema_directories = (
        "tobkiri_runtime/schemas",
        "tobkiri_runtime/tobkiri_protocol/schemas",
    )
    if inventory([baseline / p for p in schema_directories]) != inventory(
        [candidate / p for p in schema_directories]
    ):
        raise ValueError(
            "original schema resource bytes differ between native namespaces"
        )
    work.mkdir(mode=0o700)
    for name in ("home", "cache", "tmp"):
        (work / name).mkdir(mode=0o700)
    dependency_paths = sorted(
        {
            str(Path(sysconfig.get_paths()[key]).resolve())
            for key in ("purelib", "platlib")
        }
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work / "home"),
        "XDG_CACHE_HOME": str(work / "cache"),
        "TMPDIR": str(work / "tmp"),
        "GITHUB_ACTIONS": "true",
        "TOBKIRI_USER_DATA": str(work / "shared-owner"),
        "TOBKIRI_PACKAGING_GIT": shutil.which("git") or "/usr/bin/git",
    }
    script = Path(__file__).resolve()

    def child(root, phase, extra=()):
        command = [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(script),
            "--phase",
            phase,
            "--source",
            str(root),
            "--work",
            str(work),
        ]
        for path in dependency_paths:
            command += ["--dependency-path", path]
        command += list(extra)
        label = phase + "-" + str(len(list(work.glob("*.log"))))
        with (work / (label + ".log")).open("wb") as log:
            subprocess.run(
                command,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=work,
                check=True,
                timeout=180,
            )

    child(baseline, "prepare")
    inputs = json.loads(read(work / "inputs.json"))
    roots = [
        Path(inputs[key])
        for key in ("bundle_root", "artifact_root", "owner", "signed_source")
    ]
    roots.append(Path(inputs["trust_store"]).parent)
    unchanged = inventory(roots)
    cases = []

    def pair(label, reverse=False, damaged=False):
        before = inventory(roots)
        old = work / (label + "-original.json")
        new = work / (label + "-candidate.json")
        options = ["--reverse"] if reverse else []
        if damaged:
            options += ["--damaged"]
        child(baseline, "evaluate", ["--output", str(old), *options])
        legacy = old.with_suffix(".pickle")
        child(
            candidate,
            "evaluate",
            [
                "--output",
                str(new),
                "--legacy-pickle",
                str(legacy),
                "--pickle-sha",
                identity(legacy)["sha256"],
                *options,
            ],
        )
        a, b = read(old), read(new)
        if a != b:
            raise AssertionError("original/candidate observations differ: " + label)
        compiler = json.loads(a)["nonempty"]["real_compiler"]["outcome"]
        if compiler != ("exception" if label == "cas-content" else "success"):
            raise AssertionError(
                "compiler did not preserve its original successful/tampered behavior"
            )
        if inventory(roots) != before:
            raise AssertionError(
                "native reads changed the common fixture bytes: " + label
            )
        cases.append(
            {
                "name": label,
                "exact_observations_match": True,
                "observation": identity(new),
                "trusted_original_pickle": identity(legacy),
                "candidate_pickle_diagnostic": identity(new.with_suffix(".pickle")),
            }
        )

    pair("forward")
    pair("reverse", reverse=True)
    mutations = [
        ("catalog-authentication", Path(inputs["catalog_path"])),
        ("cas-content", Path(inputs["cas_root"]) / "frontend/contributions/input.json"),
    ]
    for label, path in mutations:
        raw, mode = read(path), stat.S_IMODE(path.stat().st_mode)
        modified = raw + b" "
        if label == "catalog-authentication":
            digest = inputs["artifact_digest"].encode()
            replacement = b"sha256:" + (
                b"0" * 64 if digest != b"sha256:" + b"0" * 64 else b"1" * 64
            )
            if digest not in raw:
                raise ValueError(
                    "expected exact artifact pin is absent from the authenticated catalog"
                )
            modified = raw.replace(digest, replacement, 1)
            json.loads(modified)
        try:
            path.chmod(0o600)
            path.write_bytes(modified)
            path.chmod(mode)
            pair(label, damaged=True)
        finally:
            path.chmod(0o600)
            path.write_bytes(raw)
            path.chmod(mode)
    if inventory(roots) != unchanged:
        raise AssertionError("original fixture was not restored exactly")
    clean_source(baseline, BASELINE)
    clean_source(candidate, current["source_commit"])
    write_json(
        work / "summary.json",
        {
            "format": "tobkiri.profile-resolution-native-oracle/1",
            "outcome": "success",
            "original_source": original,
            "candidate_source": current,
            "cases": cases,
            "same_shared_fixture_bytes": True,
            "all_native_checkers_original": True,
            "fixture_manifest": identity(work / "inputs.json"),
            "native_capture_or_gui_proof": False,
            "wasm_execution": False,
            "production_enabled": False,
            "full_goal_complete": False,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--phase", choices=("prepare", "evaluate"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--dependency-path", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--legacy-pickle", type=Path)
    parser.add_argument("--pickle-sha")
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--damaged", action="store_true")
    args = parser.parse_args()
    require_ci()
    if args.phase:
        install_namespace(args.source, args.dependency_path)
        if args.phase == "prepare":
            prepare(args.source, args.work)
        else:
            evaluate(
                args.source,
                args.work,
                args.output,
                args.reverse,
                args.legacy_pickle,
                args.pickle_sha,
                args.damaged,
            )
    else:
        run(args.baseline, args.candidate, args.work)


if __name__ == "__main__":
    main()
