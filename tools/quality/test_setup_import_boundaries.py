"""Source and import-boundary checks for data-only Setup imports.

The subprocess test copies only the real, locked third-party package files into
a private temporary directory, then imports the selected definitions under
``-I -S -B`` with native Host/runtime imports blocked. It does not instantiate a
validator or exercise Profile admission. The legacy facade's pickle behavior,
type annotations, and non-empty admission behavior still require the separate
native CI oracle; this source/import test does not establish those properties.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.machinery
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPOSITORY_ROOT / "tobkiri_runtime"
RELOCATION_PATH = (
    REPOSITORY_ROOT / "tools/quality/profile_resolution_source_relocation.json"
)
DATA_MODULE = Path(
    "tobkiri_runtime/ecosystem/defaultspack/domain/profile_resolution.py"
)
LEGACY_SERVICE = Path(
    "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/service.py"
)
BASELINE_COMMIT = "e15caf5c9a3255a87b4b48dcc24617d305a3681b"
BASELINE_PATH = "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/service.py"
BASELINE_SHA256 = "d40d5c88ef21b5cd0db90a46d6e32209cf5160166b0cd258256f47f680d3cc1d"
MAX_DEPENDENCY_COPY_BYTES = 16 * 1024 * 1024

LOCKED_DISTRIBUTIONS = {
    "packaging": "26.2",
    "attrs": "26.1.0",
    "jsonschema": "4.26.0",
    "jsonschema-specifications": "2025.9.1",
    "referencing": "0.37.0",
    "rpds-py": "2026.6.3",
}
REQUIRED_IMPORT_PACKAGES = (
    "packaging",
    "attrs",
    "attr",
    "jsonschema",
    "jsonschema_specifications",
    "referencing",
    "rpds",
)
OPTIONAL_IMPORT_PACKAGES = ("typing_extensions",)

LEGACY_HOST_EXPORTS = (
    "ArtifactVariant",
    "AuthorityV4Adapter",
    "AuthorityCeilings",
    "CompiledPack",
    "ContractOperation",
    "EffectClass",
    "FunctionArtifact",
    "CapabilityProviderRegistration",
    "HostExtensionRegistration",
    "HostExtensionSDK",
    "MacOSVZAgentIdentity",
    "MacOSVZDomainAllocation",
    "MacOSVZDomainAllocator",
    "MacOSVZHelperIdentity",
    "MacOSVZLaunchAssets",
    "MacOSVZRuntime",
    "MacOSVZSupervisorDriver",
    "MacOSVZSupervisorTransport",
    "MacOSVZTransportFactory",
    "InvocationFrame",
    "HostV4Composition",
    "OpaqueAuthorityRef",
    "PackArtifact",
    "ProductionRuntimeV4",
    "RequestContext",
    "V4DispatchSession",
    "install_dispatch_session",
    "production_backend_registry",
    "compile_pack_root",
    "routes_for_plan",
)


def _names_assigned_by_target(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        result: set[str] = set()
        for element in target.elts:
            result.update(_names_assigned_by_target(element))
        return result
    return set()


def _top_level_spans(source: bytes) -> dict[str, list[bytes]]:
    """Return exact source bytes for top-level defs/classes/assignments."""

    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)
    spans: dict[str, list[bytes]] = {}
    for node in tree.body:
        names: set[str] = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_names_assigned_by_target(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_names_assigned_by_target(node.target))
        else:
            continue

        start_line = min(
            [
                node.lineno,
                *(
                    decorator.lineno
                    for decorator in getattr(node, "decorator_list", ())
                ),
            ],
        )
        snippet = b"".join(lines[start_line - 1 : node.end_lineno])
        for name in names:
            spans.setdefault(name, []).append(snippet)
    return spans


def _iter_package_files(package: str, spec: object) -> list[tuple[Path, Path]]:
    """List a real package's files as (source, package-relative path)."""

    locations = getattr(spec, "submodule_search_locations", None)
    if locations:
        result: list[tuple[Path, Path]] = []
        for location in locations:
            root = Path(location)
            if not root.is_dir() or root.is_symlink():
                raise AssertionError(f"unsafe package root for {package}: {root}")
            for current, directories, files in os.walk(root, followlinks=False):
                current_path = Path(current)
                kept_directories = []
                for directory in directories:
                    child = current_path / directory
                    if directory == "__pycache__":
                        continue
                    if child.is_symlink():
                        raise AssertionError(
                            f"symlink in original package {package}: {child}"
                        )
                    kept_directories.append(directory)
                directories[:] = kept_directories
                for filename in files:
                    if filename.endswith((".pyc", ".pyo")):
                        continue
                    source = current_path / filename
                    if source.is_symlink() or not source.is_file():
                        raise AssertionError(
                            f"non-regular original package file: {source}"
                        )
                    result.append((source, source.relative_to(root)))
        if not result:
            raise AssertionError(f"empty original package: {package}")
        return result

    origin = getattr(spec, "origin", None)
    if not origin or origin in {"built-in", "frozen"}:
        raise AssertionError(f"no copyable original source for dependency {package}")
    source = Path(origin)
    if source.is_symlink() or not source.is_file():
        raise AssertionError(
            f"non-regular original module source for {package}: {source}"
        )
    return [(source, Path(source.name))]


def _copy_locked_dependency_sources(destination: Path) -> dict[str, object]:
    """Copy real pinned imports, excluding bytecode and enforcing a total cap."""

    versions = {}
    for distribution, expected in LOCKED_DISTRIBUTIONS.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise AssertionError(
                f"required genuine dependency {distribution}=={expected} is missing"
            ) from exc
        if actual != expected:
            raise AssertionError(
                f"dependency {distribution} has version {actual}, "
                f"expected locked {expected}"
            )
        versions[distribution] = actual

    search_path = [entry for entry in sys.path if entry]
    package_specs: dict[str, object] = {}
    missing = []
    for package in (*REQUIRED_IMPORT_PACKAGES, *OPTIONAL_IMPORT_PACKAGES):
        spec = importlib.machinery.PathFinder.find_spec(package, search_path)
        if spec is None:
            if package in REQUIRED_IMPORT_PACKAGES:
                missing.append(package)
            continue
        package_specs[package] = spec
    if missing:
        raise AssertionError(
            "required genuine dependency imports are unavailable: " + ", ".join(missing)
        )

    pending: list[tuple[str, Path, Path, int, bool]] = []
    total_bytes = 0
    for package, spec in package_specs.items():
        is_package = bool(getattr(spec, "submodule_search_locations", None))
        for source, relative in _iter_package_files(package, spec):
            size = source.stat(follow_symlinks=False).st_size
            total_bytes += size
            if total_bytes > MAX_DEPENDENCY_COPY_BYTES:
                raise AssertionError(
                    "genuine dependency source copy exceeds "
                    f"{MAX_DEPENDENCY_COPY_BYTES} bytes"
                )
            pending.append((package, source, relative, size, is_package))

    copied_files = 0
    copied_bytes = 0
    for package, source, relative, size, is_package in pending:
        target = (
            destination / package / relative if is_package else destination / relative
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        file_bytes = 0
        with source.open("rb") as source_stream, target.open("xb") as target_stream:
            while chunk := source_stream.read(
                min(1024 * 1024, MAX_DEPENDENCY_COPY_BYTES - copied_bytes + 1)
            ):
                if copied_bytes + len(chunk) > MAX_DEPENDENCY_COPY_BYTES:
                    raise AssertionError(
                        "genuine dependency source copy exceeded "
                        f"{MAX_DEPENDENCY_COPY_BYTES} bytes"
                    )
                target_stream.write(chunk)
                file_bytes += len(chunk)
                copied_bytes += len(chunk)
        if file_bytes != size:
            raise AssertionError(
                f"dependency source changed while being copied: {source}"
            )
        copied_files += 1

    return {
        "distributions": versions,
        "packages": sorted(package_specs),
        "file_count": copied_files,
        "bytes": copied_bytes,
    }


CHILD_IMPORT_SCRIPT = textwrap.dedent(
    r"""
    import importlib
    import importlib.abc
    import json
    from pathlib import Path
    import sys

    dependency_root = Path(sys.argv[1])
    runtime_root = Path(sys.argv[2])
    sys.path[:0] = [str(dependency_root), str(runtime_root)]

    blocked_prefixes = (
        "ctypes",
        "cryptography",
        "Crypto",
        "tobkiri_protocol.secure_persistence",
        "ecosystem.defaultspack.domain.runtime_v4",
        "core_runtime.external_pack_catalog_v4",
        "core_runtime.runtime_v4",
        "core_runtime.host_vm",
        "tobkiri_host.authority_v4",
        "tobkiri_host.backends",
        "tobkiri_host.macos_vz_supervisor",
        "tobkiri_host.extension_sdk",
        "tobkiri_host.composition",
        "tobkiri_host.runtime",
        "wasmtime",
    )

    class NativeImportBlocker(importlib.abc.MetaPathFinder):
        attempts = []

        def find_spec(self, fullname, path=None, target=None):
            if any(
                fullname == prefix or fullname.startswith(prefix + ".")
                for prefix in blocked_prefixes
            ):
                self.attempts.append(fullname)
                raise AssertionError("blocked native import: " + fullname)
            return None

    blocker = NativeImportBlocker()
    sys.meta_path.insert(0, blocker)

    data = importlib.import_module(
        "ecosystem.defaultspack.domain.profile_resolution"
    )
    composition = importlib.import_module(
        "ecosystem.defaultspack.defaultspack.profile_runtime_composition"
    )
    import tobkiri_host

    assert data.ProfileResolutionDenied.__module__ == (
        "ecosystem.defaultspack.domain.runtime_v4.service"
    )
    assert data.ResolvedDefaultProfile.__module__ == (
        "ecosystem.defaultspack.domain.runtime_v4.service"
    )
    assert data.resolve_default_profile.__module__ == (
        "ecosystem.defaultspack.domain.runtime_v4.service"
    )
    assert data.ProtocolError.__module__ == "tobkiri_protocol.errors"
    assert data.DefaultProfileV4Error.__module__ == "tobkiri_protocol.bundle_catalog"
    assert hasattr(composition, "DefaultspackProfileRuntime")

    expected_exports = json.loads(sys.argv[3])
    assert tuple(tobkiri_host.__all__) == tuple(expected_exports)
    assert set(expected_exports).issubset(dir(tobkiri_host))
    assert "tobkiri_host.models" in sys.modules
    assert tobkiri_host.models is sys.modules["tobkiri_host.models"]
    assert {
        "authority_v4", "backends", "macos_vz_supervisor", "models",
        "extension_sdk", "artifact_compiler", "composition", "runtime",
    }.issubset(dir(tobkiri_host))
    model_names = (
        "ArtifactVariant",
        "ContractOperation",
        "EffectClass",
        "FunctionArtifact",
        "InvocationFrame",
        "OpaqueAuthorityRef",
        "PackArtifact",
        "RequestContext",
    )
    model_module = sys.modules["tobkiri_host.models"]
    for name in model_names:
        assert getattr(tobkiri_host, name) is getattr(model_module, name)
        assert name in dir(tobkiri_host)

    import core_runtime.profile_pack_projection as projection

    assert hasattr(projection, "admitted_projection_sources")
    assert "core_runtime.external_pack_catalog_v4" not in sys.modules

    try:
        getattr(tobkiri_host, "__setup_import_boundary_unknown_export__")
    except AttributeError:
        pass
    else:
        raise AssertionError("unknown Host export did not raise AttributeError")

    compiler = importlib.import_module("tobkiri_host.artifact_compiler")
    for name in ("CompiledPack", "compile_pack_root", "routes_for_plan"):
        assert getattr(tobkiri_host, name) is getattr(compiler, name)
    # Reload must evict cached compiler exports before resolving them again.
    tobkiri_host.CompiledPack = object()
    tobkiri_host.compile_pack_root = object()
    reloaded_host = importlib.reload(tobkiri_host)
    assert "CompiledPack" not in reloaded_host.__dict__
    assert "compile_pack_root" not in reloaded_host.__dict__
    assert reloaded_host.CompiledPack is compiler.CompiledPack
    assert reloaded_host.compile_pack_root is compiler.compile_pack_root

    forbidden_loaded = sorted(
        name
        for name in sys.modules
        if any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in blocked_prefixes
        )
    )
    assert not blocker.attempts, "blocked imports attempted: " + repr(blocker.attempts)
    assert not forbidden_loaded, "blocked modules loaded: " + repr(forbidden_loaded)
    print(json.dumps({
        "imports": [
            "ecosystem.defaultspack.domain.profile_resolution",
            "ecosystem.defaultspack.defaultspack.profile_runtime_composition",
            "core_runtime.profile_pack_projection",
            "tobkiri_host.artifact_compiler",
        ],
        "host_export_count": len(expected_exports),
        "blocked_import_attempts": blocker.attempts,
        "blocked_modules_loaded": forbidden_loaded,
    }, separators=(",", ":")))
    """
)


class SetupImportBoundaryTests(unittest.TestCase):
    def test_relocated_profile_definitions_match_recorded_source(self) -> None:
        record = json.loads(RELOCATION_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            record["format"], "tobkiri.profile-resolution-source-relocation/1"
        )
        self.assertEqual(record["baseline_commit"], BASELINE_COMMIT)
        self.assertEqual(record["baseline_path"], BASELINE_PATH)
        self.assertEqual(record["baseline_sha256"], BASELINE_SHA256)
        self.assertEqual(record["destination"], DATA_MODULE.as_posix())
        self.assertRegex(record["baseline_sha256"], r"^[0-9a-f]{64}$")
        expected_names = set(record["owned_definition_names"]) | set(
            record["moved_constant_names"]
        )
        self.assertEqual(expected_names, set(record["definitions"]))

        moved_source = (REPOSITORY_ROOT / DATA_MODULE).read_bytes()
        moved_spans = _top_level_spans(moved_source)
        for name, definition_record in record["definitions"].items():
            with self.subTest(definition=name):
                matches = moved_spans.get(name, [])
                self.assertEqual(
                    len(matches), 1, f"expected one data definition for {name}"
                )
                snippet = matches[0]
                self.assertTrue(
                    snippet.endswith(b"\n"), f"{name} must retain its ending LF"
                )
                self.assertEqual(
                    hashlib.sha256(snippet).hexdigest(), definition_record["sha256"]
                )

        legacy_source = (REPOSITORY_ROOT / LEGACY_SERVICE).read_bytes()
        legacy_spans = _top_level_spans(legacy_source)
        for name in expected_names:
            with self.subTest(legacy_redefinition=name):
                self.assertNotIn(name, legacy_spans)

    def test_data_imports_stay_outside_native_host_boundaries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="setup-import-deps-") as temporary:
            dependency_root = Path(temporary)
            try:
                copy_receipt = _copy_locked_dependency_sources(dependency_root)
            except AssertionError as exc:
                self.fail(str(exc))

            command = [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                CHILD_IMPORT_SCRIPT,
                str(dependency_root),
                str(RUNTIME_ROOT),
                json.dumps(LEGACY_HOST_EXPORTS),
            ]
            try:
                result = subprocess.run(
                    command,
                    cwd=REPOSITORY_ROOT,
                    text=True,
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                self.fail(
                    f"isolated definition-import child exceeded 20 seconds: {exc}"
                )
            self.assertEqual(
                result.returncode,
                0,
                "isolated definition imports failed; "
                f"copied {copy_receipt['file_count']} genuine files / "
                f"{copy_receipt['bytes']} bytes\nstdout={result.stdout[-1000:]}\n"
                f"stderr={result.stderr[-3000:]}",
            )
            child_result = json.loads(result.stdout)
            self.assertEqual(child_result["blocked_import_attempts"], [])
            self.assertEqual(child_result["blocked_modules_loaded"], [])
            self.assertLessEqual(copy_receipt["bytes"], MAX_DEPENDENCY_COPY_BYTES)


if __name__ == "__main__":
    unittest.main()
