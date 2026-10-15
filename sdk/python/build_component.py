"""Build a caller-selected Python WIT world into a typed outer component.

This is a development tool. Runtime execution remains in Rust Core; Python and
the pinned build tools are not runtime dependencies.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from importlib.metadata import distribution, version
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from typing import Any
import unicodedata

from composition import (
    compose,
    is_wit_package_source,
    world_export_names_from_sources,
)

TOOLS = {"componentize-py": "0.25.0", "wasmtime": "48.0.0"}
PLAN_FORMAT = "tobkiri.python-build/3"
MAX_PLAN_BYTES = 1024 * 1024
MAX_SOURCE_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 32 * 1024 * 1024
MAX_BINDING_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BINDING_BYTES = 32 * 1024 * 1024
MAX_COMPONENT_BYTES = 512 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MODULE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*")
_WORLD = re.compile(r"[a-z][a-z0-9-]*")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8"))


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def destination(name: str) -> PurePosixPath:
    """Reject traversal, alternate separators and non-normalized plan paths."""
    if (not isinstance(name, str) or not name or "\\" in name
            or ":" in name or "\0" in name):
        raise ValueError("build paths must be nonempty normalized relative paths")
    path = PurePosixPath(name)
    if (path.is_absolute() or name.startswith("/") or ".." in path.parts
            or "." in path.parts or path.as_posix() != name):
        raise ValueError("build paths must be nonempty normalized relative paths")
    return path


def _path_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _claim_path(path: PurePosixPath, files: set[str], nodes: dict[str, str]) -> None:
    """Reject aliases and file/directory conflicts even on insensitive hosts."""
    for length in range(1, len(path.parts) + 1):
        spelling = "/".join(path.parts[:length])
        key = _path_key(spelling)
        if key in files or (key in nodes and nodes[key] != spelling):
            raise ValueError("duplicate or case-folded build path collision")
        if length == len(path.parts) and key in nodes:
            raise ValueError("build path has a file/directory collision")
        nodes[key] = spelling
    files.add(_path_key(str(path)))


def _reject_symlink_path(root: Path, relative: PurePosixPath) -> Path:
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("build inputs must not pass through symlinks")
    if not path.is_file():
        raise ValueError("build input must be a regular file")
    return path


def capture(path: Path, expected_sha256: str, *, max_bytes: int = MAX_SOURCE_FILE_BYTES) -> bytes:
    """Capture bytes once and validate the reviewed SHA-256 before staging."""
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        raise ValueError("source pin must be 64 lowercase hexadecimal characters")
    if path.is_symlink() or not path.is_file():
        raise ValueError("source must be a regular file, not a symlink")
    if path.stat().st_size > max_bytes:
        raise ValueError("build input exceeds its configured byte limit")
    content = path.read_bytes()
    if len(content) > max_bytes:
        raise ValueError("build input exceeds its configured byte limit")
    if sha256(content) != expected_sha256:
        raise ValueError("source SHA-256 differs from the reviewed build pin")
    return content


def _tool_lock() -> tuple[dict[str, Any], bytes]:
    path = Path(__file__).with_name("toolchain-lock.json")
    if path.is_symlink() or not path.is_file():
        raise ValueError("official build tool lock is missing or unsafe")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("official build tool lock is invalid JSON") from error
    if value.get("format") != "tobkiri.python-toolchain-lock/1":
        raise ValueError("unsupported official build tool lock format")
    return value, raw


def _installed_tool(name: str, expected_version: str,
                    lock: dict[str, Any], wheel_archive: Path | None) -> dict[str, Any]:
    """Record installed package identity and check its official wheel tag/hash pin."""
    dist = distribution(name)
    if dist.version != expected_version or version(name) != expected_version:
        raise ValueError(f"{name} must be version {expected_version}")
    if lock["tools"][name].get("version") != expected_version:
        raise ValueError(f"{name} version differs from the official wheel lock")
    wheel_text = dist.read_text("WHEEL")
    if not wheel_text:
        raise ValueError(f"{name} is missing wheel metadata")
    tags = sorted(line.partition(":")[2].strip()
                  for line in wheel_text.splitlines() if line.startswith("Tag:"))
    if len(tags) != 1:
        raise ValueError(f"{name} must have one explicit installed wheel tag")
    wheel = lock["tools"][name]["wheels"].get(tags[0])
    if not wheel:
        raise ValueError(f"{name} wheel tag {tags[0]} has no official SHA-256 pin")

    archive_hash: str | None = None
    archive_verified = False
    if wheel_archive is not None:
        if wheel_archive.is_symlink() or not wheel_archive.is_file():
            raise ValueError(f"{name} wheel archive must be a regular file")
        if wheel_archive.name != wheel["filename"]:
            raise ValueError(f"{name} wheel archive filename differs from its official pin")
        archive_hash = sha256(wheel_archive.read_bytes())
        if archive_hash != wheel["sha256"]:
            raise ValueError(f"{name} wheel archive differs from its official PyPI SHA-256")
        archive_verified = True

    installation_archive_hash = None
    direct_url = dist.read_text("direct_url.json")
    if direct_url:
        try:
            direct = json.loads(direct_url)
        except json.JSONDecodeError as error:
            raise ValueError(f"{name} has invalid direct URL installation metadata") from error
        installed_hash = direct.get("archive_info", {}).get("hashes", {}).get("sha256")
        if installed_hash:
            if installed_hash != wheel["sha256"]:
                raise ValueError(f"{name} installed archive hash differs from its official pin")
            installation_archive_hash = installed_hash

    manifest: list[dict[str, Any]] = []
    unhashed: list[str] = []
    extension_names: list[str] = []
    for item in sorted(dist.files or [], key=str):
        relative = str(item).replace("\\", "/")
        item_path = Path(dist.locate_file(item))
        if item_path.is_symlink() or not item_path.is_file():
            raise ValueError(f"{name} installed distribution contains a missing or linked file")
        content = item_path.read_bytes()
        if item.size is not None and item.size != len(content):
            raise ValueError(f"{name} installed file size differs from RECORD")
        if item.hash is not None:
            if item.hash.mode not in {"sha256", "sha384", "sha512"}:
                raise ValueError(f"{name} RECORD uses an unsupported hash algorithm")
            actual = base64.urlsafe_b64encode(
                hashlib.new(item.hash.mode, content).digest()
            ).rstrip(b"=").decode("ascii")
            if actual != item.hash.value:
                raise ValueError(f"{name} installed file hash differs from RECORD")
        elif (item_path.suffix == ".pyc"
              or (item_path.name == "RECORD"
                  and item_path.parent.name.endswith(".dist-info"))):
            unhashed.append(relative)
        else:
            raise ValueError(f"{name} installed file is missing its RECORD hash")
        manifest.append({"path": relative, "sha256": sha256(content),
                         "bytes": len(content)})
        path = PurePosixPath(relative)
        if (name == "componentize-py" and path.parent == PurePosixPath("componentize_py")
                and re.fullmatch(r"componentize_py(?:\.[a-zA-Z0-9_-]+)*\.(?:so|pyd)",
                                 path.name)):
            extension_names.append(relative)
    if not manifest:
        raise ValueError(f"{name} installed distribution has no file manifest")
    extension_hash = None
    extension_name = None
    if name == "componentize-py":
        if len(extension_names) != 1:
            raise ValueError("componentize-py must contain exactly one CPython extension")
        extension_name = extension_names[0]
        extension_hash = next(item["sha256"] for item in manifest
                              if item["path"] == extension_name)

    return {
        "version": dist.version,
        "wheel": {"filename": wheel["filename"], "tag": tags[0],
                  "official_pypi_sha256_pin": wheel["sha256"],
                  "archive_sha256": archive_hash,
                  "archive_sha256_verified": archive_verified,
                  "installation_archive_sha256_claim": installation_archive_hash},
        "installed_manifest_sha256": canonical_hash(manifest),
        "installed_manifest_files": len(manifest),
        "installed_manifest_bytes": sum(item["bytes"] for item in manifest),
        "installed_record_consistency_verified": True,
        "installed_record_unhashed_files": unhashed,
        "installed_tree_authenticated_by_archive": False,
        "extension_path": extension_name,
        "extension_sha256": extension_hash,
    }


def _compiler_executable() -> Path:
    """Find the installed console launcher in this interpreter's scripts path."""
    filename = "componentize-py.exe" if sys.platform == "win32" else "componentize-py"
    compiler = Path(sysconfig.get_path("scripts")) / filename
    if compiler.is_symlink() or not compiler.is_file():
        raise ValueError("componentize-py must be installed in this Python's scripts path")
    dist = distribution("componentize-py")
    if not any(entry.group == "console_scripts" and entry.name == "componentize-py"
               and entry.value == "componentize_py:script" for entry in dist.entry_points):
        raise ValueError("componentize-py console entrypoint metadata differs")
    if not any(Path(dist.locate_file(item)).resolve() == compiler.resolve()
               for item in dist.files or []):
        raise ValueError("componentize-py launcher is missing from its installed manifest")
    return compiler


def _assert_compiler_unchanged(compiler: Path, expected_hash: str) -> None:
    if (compiler.is_symlink() or not compiler.is_file()
            or sha256(compiler.read_bytes()) != expected_hash):
        raise ValueError("componentize-py executable changed during the build")


def _verify_plan(plan_path: Path) -> tuple[dict[str, Any], bytes, str, list[tuple[PurePosixPath, bytes]]]:
    if plan_path.is_symlink() or not plan_path.is_file():
        raise ValueError("build plan must be a regular file, not a symlink")
    if plan_path.stat().st_size > MAX_PLAN_BYTES:
        raise ValueError("build plan exceeds its byte limit")
    plan_bytes = plan_path.read_bytes()
    if len(plan_bytes) > MAX_PLAN_BYTES:
        raise ValueError("build plan exceeds its byte limit")
    try:
        plan = json.loads(plan_bytes.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("build plan must be valid UTF-8 JSON without duplicate keys") from error
    if not isinstance(plan, dict) or plan.get("format") != PLAN_FORMAT:
        raise ValueError("unsupported Python build plan format")
    required = {"format", "stub_wasi", "wit", "world", "entrypoint", "sources",
                "outer_exports", "allowed_imports"}
    if set(plan) != required:
        raise ValueError("Python build plan fields differ from the reviewed schema")
    if plan["stub_wasi"] is not True:
        raise ValueError("Python initialization must use reviewed stub-WASI mode")
    if not isinstance(plan["world"], str) or not _WORLD.fullmatch(plan["world"]):
        raise ValueError("build plan world must be a simple WIT identifier")
    if not isinstance(plan["entrypoint"], str) or not _MODULE.fullmatch(plan["entrypoint"]):
        raise ValueError("build plan entrypoint must be a Python module name")
    wit_destination = destination(plan["wit"])
    if not isinstance(plan["sources"], list) or not plan["sources"]:
        raise ValueError("build plan sources must be a nonempty list")

    root = plan_path.parent.resolve(strict=True)
    captures: list[tuple[PurePosixPath, bytes]] = []
    seen_sources: set[str] = set()
    seen_destinations: set[str] = set()
    source_nodes: dict[str, str] = {}
    destination_nodes: dict[str, str] = {}
    total_bytes = 0
    for item in plan["sources"]:
        if not isinstance(item, dict) or set(item) != {"source", "destination", "sha256"}:
            raise ValueError("each source must declare exactly source, destination and sha256")
        source_name = destination(item["source"])
        output_name = destination(item["destination"])
        _claim_path(source_name, seen_sources, source_nodes)
        _claim_path(output_name, seen_destinations, destination_nodes)
        source_path = _reject_symlink_path(root, source_name)
        content = capture(source_path, item["sha256"])
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise ValueError("total captured source bytes exceed the build limit")
        captures.append((output_name, content))
    captured_names = {str(path) for path, _ in captures}
    if str(wit_destination) not in captured_names:
        raise ValueError("WIT file must be captured as a declared build source")
    entrypoint_path = PurePosixPath(*plan["entrypoint"].split(".")).with_suffix(".py")
    if str(entrypoint_path) not in captured_names:
        raise ValueError("Python entrypoint must be one of the captured source files")
    if any(_path_key(path.parts[0]) in {
        "outer.wac", "outer.wasm", "component.wasm", "no-ambient-dependencies",
    } for path, _ in captures):
        raise ValueError("source destinations cannot shadow generated component files")
    if any(_path_key(path.parts[0]) in {"wit_world", "componentize_py_types.py"}
           for path, _ in captures):
        raise ValueError("source destinations cannot shadow generated Python bindings")

    exports = plan["outer_exports"]
    imports = plan["allowed_imports"]
    if (not isinstance(exports, list) or not exports
            or any(not isinstance(name, str) for name in exports)
            or len(exports) != len(set(exports))):
        raise ValueError("outer_exports must be a nonempty unique string list")
    if (not isinstance(imports, list)
            or any(not isinstance(name, str) or not _INTERFACE_NAME.fullmatch(name)
                   for name in imports)
            or len(imports) != len(set(imports))):
        raise ValueError("allowed_imports must be a unique list of fully qualified WIT interfaces")

    wit_root_parts = wit_destination.parent.parts
    captured_wit = []
    for path, content in captures:
        if path.parts[:len(wit_root_parts)] != wit_root_parts:
            continue
        relative = PurePosixPath(*path.parts[len(wit_root_parts):])
        if (len(relative.parts) == 2 and relative.parts[0] == "deps"
                and relative.suffix in {".wasm", ".wat"}):
            raise ValueError("encoded WIT dependencies are unsupported")
        if is_wit_package_source(relative):
            captured_wit.append((str(path), content))
    try:
        declared_exports = world_export_names_from_sources(
            captured_wit, plan["world"], root_source=str(wit_destination))
    except UnicodeDecodeError as error:
        raise ValueError("captured WIT source must be UTF-8") from error
    if sorted(exports) != declared_exports:
        raise ValueError("outer_exports must exactly match the selected WIT world exports")
    return plan, plan_bytes, sha256(plan_bytes), captures


_INTERFACE_NAME = re.compile(
    r"[a-z][a-z0-9-]*:[a-z][a-z0-9-]*/[a-z][a-z0-9-]*"
    r"@[0-9]+\.[0-9]+\.[0-9]+"
)


def _tree_manifest(root: Path, *, exclude: set[str] | frozenset[str] = frozenset()
                   ) -> list[dict[str, Any]]:
    manifest = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("generated Python bindings contain a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in exclude:
            continue
        if path.stat().st_size > MAX_BINDING_FILE_BYTES:
            raise ValueError("generated binding file exceeds its byte limit")
        content = path.read_bytes()
        total += len(content)
        if total > MAX_TOTAL_BINDING_BYTES:
            raise ValueError("generated bindings exceed their cumulative byte limit")
        manifest.append({"path": relative, "sha256": sha256(content), "bytes": len(content)})
    return manifest


def _assert_captured_stage(stage: Path,
                           captures: list[tuple[PurePosixPath, bytes]]) -> None:
    for name, expected in captures:
        path = stage.joinpath(*name.parts)
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            raise ValueError("producer modified a captured source or WIT input")


def _componentize_commands(compiler: Path, plan: dict[str, Any]
                           ) -> tuple[list[str], list[str]]:
    """Use the captured WIT package directory so declared deps resolve locally."""
    wit_directory = str(destination(plan["wit"]).parent)
    command = [str(compiler), "-d", wit_directory, "-w", plan["world"]]
    return ([*command, "bindings", "."],
            [*command, "componentize", "--stub-wasi", plan["entrypoint"],
             "-o", "component.wasm"])


def build(plan_path: Path, output: Path, composer: Path, *,
          componentize_wheel: Path | None = None,
          wasmtime_wheel: Path | None = None) -> dict[str, Any]:
    """Build the captured source plan and record actual tool and ABI evidence."""
    plan, plan_bytes, plan_hash, captures = _verify_plan(plan_path)
    lock_path = Path(__file__).with_name("toolchain-lock.json")
    lock, lock_bytes = _tool_lock()
    toolchain = {name: _installed_tool(
        name, expected, lock,
        componentize_wheel if name == "componentize-py" else wasmtime_wheel,
    ) for name, expected in TOOLS.items()}
    compiler = _compiler_executable()
    compiler_hash = sha256(compiler.read_bytes())
    if output.suffix != ".wasm" or output.is_symlink():
        raise ValueError("output must be a regular .wasm artifact")
    output = output.absolute()
    receipt_path = output.with_suffix(".build.json")
    if receipt_path.is_symlink():
        raise ValueError("build receipt path must not be a symlink")
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < 5 * 1024**3:
        raise ValueError("Python component build requires a 5 GiB free-space floor")

    from wasmtime import Config, Engine
    from wasmtime.component import Component

    source_manifest = [{"source": item["source"], "destination": str(name),
                        "sha256": sha256(content), "bytes": len(content)}
                       for item, (name, content) in zip(plan["sources"], captures)]
    source_names = {str(name) for name, _ in captures}
    producer_paths = [Path(__file__), Path(__file__).with_name("composition.py"), lock_path]
    producer_sources = {lock_path.name: sha256(lock_bytes)}
    for path in producer_paths[:-1]:
        if path.is_symlink() or not path.is_file():
            raise ValueError("producer source provenance must use regular files")
        producer_sources[path.name] = sha256(path.read_bytes())
    with tempfile.TemporaryDirectory(prefix="tobkiri-python-component-") as temporary:
        stage = Path(temporary)
        for name, content in captures:
            target = stage.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        # Credentials, inherited HOME and arbitrary user initialization are absent.
        environment = {"PATH": os.pathsep.join((str(compiler.parent), os.defpath)),
                       "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"}
        wit_source_directory = destination(plan["wit"]).parent
        bindings_command, component_command = _componentize_commands(compiler, plan)
        subprocess.run(bindings_command, cwd=stage, env=environment,
                       check=True, timeout=180)
        _assert_compiler_unchanged(compiler, compiler_hash)
        _assert_captured_stage(stage, captures)
        bindings = _tree_manifest(stage, exclude=source_names)
        if not bindings:
            raise ValueError("componentize-py generated no WIT bindings")
        bindings_hash = canonical_hash(bindings)

        subprocess.run(component_command, cwd=stage, env=environment,
                       check=True, timeout=180)
        _assert_compiler_unchanged(compiler, compiler_hash)
        _assert_captured_stage(stage, captures)
        config = Config()
        config.parallel_compilation = False
        engine = Engine(config)
        inner_path = stage / "component.wasm"
        if inner_path.stat().st_size > MAX_COMPONENT_BYTES:
            raise ValueError("inner Python component exceeds its byte limit")
        inner_binary = inner_path.read_bytes()
        inner_hash = sha256(inner_binary)
        inner_component = Component(engine, inner_binary)
        inner_imports = sorted(inner_component.type.imports(engine))
        allowed = set(plan["allowed_imports"])
        if set(inner_imports) != allowed:
            raise ValueError("actual Python imports differ from the explicit permission world")
        inner_exports = sorted(inner_component.type.exports(engine))
        expected_exports = set(plan["outer_exports"])
        if set(inner_exports) != expected_exports | {"exports"}:
            raise ValueError("actual Python exports differ from the declared world and private initializer")

        wit_directory = stage.joinpath(*wit_source_directory.parts)
        composition = compose(composer.absolute(), inner_path, stage, wit_directory,
                              plan["world"], plan["outer_exports"], environment)
        _assert_captured_stage(stage, captures)
        if sha256(inner_path.read_bytes()) != inner_hash:
            raise ValueError("composition modified its original inner input")
        outer_path = stage / "outer.wasm"
        if outer_path.stat().st_size > MAX_COMPONENT_BYTES:
            raise ValueError("outer Python component exceeds its byte limit")
        outer_binary = outer_path.read_bytes()
        outer_component = Component(engine, outer_binary)
        outer_imports = sorted(outer_component.type.imports(engine))
        outer_exports = sorted(outer_component.type.exports(engine))
        if set(outer_imports) != allowed or set(outer_exports) != expected_exports:
            raise ValueError("actual outer component differs from the exact declared ABI closure")
        if (sha256(lock_path.read_bytes()) != sha256(lock_bytes)
                or any(sha256(path.read_bytes()) != producer_sources[path.name]
                       for path in producer_paths[:-1])):
            raise ValueError("producer source or tool lock changed during the build")

        evidence = {
            "format": "tobkiri.component-build/2",
            "language": "python",
            "build_mode": "official-wheel-pinned-development-producer",
            "production_enabled": False,
            "host": {"python": platform.python_version(),
                     "implementation": platform.python_implementation(),
                     "platform": platform.platform(), "machine": platform.machine()},
            "tools": toolchain,
            "toolchain_lock_sha256": sha256(lock_bytes),
            "producer_sources": producer_sources,
            "compiler_executable_sha256": compiler_hash,
            "build_plan_sha256": plan_hash,
            "source_manifest": source_manifest,
            "source_manifest_sha256": canonical_hash(source_manifest),
            "wit": {"path": plan["wit"],
                    "source_directory": str(wit_source_directory), "sha256": next(
                item["sha256"] for item in source_manifest if item["destination"] == plan["wit"]),
                    "world": plan["world"]},
            "bindings": {"files": bindings, "manifest_sha256": bindings_hash},
            "options": {"stub_wasi": True, "inherited_environment": False,
                         "compiler_process_pythonhashseed": "0",
                         "guest_pythonhashseed": "compiler-default-uncontrolled",
                         "parallel_validation_compilation": False},
            "commands": {"bindings": ["componentize-py", *bindings_command[1:]],
                         "componentize": ["componentize-py", *component_command[1:]]},
            "composition": composition,
            "target_validation": {"tool": "wac-cli", "world": plan["world"],
                                  "exact_exports": True, "typed_target_check": True},
            "inner": {"artifact_sha256": inner_hash, "artifact_bytes": len(inner_binary),
                      "imports": inner_imports, "exports": inner_exports},
            "world": plan["world"], "imports": outer_imports,
            "exports": outer_exports,
            "artifact_sha256": sha256(outer_binary),
            "artifact_bytes": len(outer_binary),
        }
        output.write_bytes(outer_binary)
        receipt_path.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n",
                                encoding="utf-8")
        return evidence


def main() -> None:
    """Build exactly one reviewed plan; no automatic source/provider discovery."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--composer", type=Path, required=True,
                        help="explicit pinned official WAC executable; build-time only")
    parser.add_argument("--componentize-wheel", type=Path,
                        help="optional matching wheel archive for direct hash verification")
    parser.add_argument("--wasmtime-wheel", type=Path,
                        help="optional matching wheel archive for direct hash verification")
    args = parser.parse_args()
    print(json.dumps(build(args.plan, args.output, args.composer,
                           componentize_wheel=args.componentize_wheel,
                           wasmtime_wheel=args.wasmtime_wheel), sort_keys=True))


if __name__ == "__main__":
    main()
