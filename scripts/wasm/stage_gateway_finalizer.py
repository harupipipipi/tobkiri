"""Stage the exact finalizer source closure, never build or execute a guest.

The format-3 SDK source plan contains only finalizer Python, canonical codec and
WIT definitions. Imported provider implementations and source-case answers stay
outside its isolated guest source root; their identities are evidence only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

import prepare_gateway_finalizer as preparation

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "packs/local.ai-gateway-finalizer-python"
STAGER = "scripts/wasm/stage_gateway_finalizer.py"
EXTRACTOR = "scripts/wasm/prepare_gateway_finalizer.py"
INTERFACE = "local:ai-gateway-finalizer-python/finalizer@1.0.0"
REQUIRED_INTERFACES = (
    "local:ai-gateway-tool-caller-python/caller@1.0.0",
    "local:ai-usage-cost-python/usage-cost@1.0.0",
    "local:ai-stream-normalizer-python/stream-normalizer@1.0.0",
)
TRANSITIVE_WIT_INTERFACE = "local:ai-tool-bridge-ordered-python/bridge@2.0.0"
PROTOCOL = "ordered-finite-json-v2"
MAX_TOTAL_BYTES = 32 * 1024 * 1024
NO_EXECUTION = (
    "build_performed", "wasm_execution_performed", "native_core_acceptance",
    "full_host_replay_performed", "production_enabled",
)
SOURCE_MODULES = (
    "original_gateway_finalizer.py", "gateway_finalizer_adapter.py", "app.py",
    "inventory.json", "provenance.json",
)
OBSERVER_EVIDENCE = (f"{PREFIX}/README.md", f"{PREFIX}/source-cases.json")
DESTINATIONS = {
    **{f"python/{name}": f"{PREFIX}/python/{name}" for name in SOURCE_MODULES},
    **{f"python/{Path(path).name}": path for path in preparation.CODEC_PATHS},
    "wit/pack.wit": f"{PREFIX}/wit/pack.wit",
    "wit/deps/tobkiri-core/core.wit": preparation.CORE_WIT,
    "wit/deps/ai-gateway-tool-caller-python/pack.wit": preparation.DEPENDENCY_WIT[0],
    "wit/deps/ai-usage-cost-python/pack.wit": preparation.DEPENDENCY_WIT[1],
    "wit/deps/ai-stream-normalizer-python/pack.wit": preparation.DEPENDENCY_WIT[2],
    "wit/deps/ai-tool-bridge-ordered-python/pack.wit": preparation.TRANSITIVE_WIT,
}
DECLARED_DESTINATIONS = tuple(sorted(DESTINATIONS.items()))
WIT_PACKAGES = {
    "wit/pack.wit": "local:ai-gateway-finalizer-python@1.0.0",
    "wit/deps/tobkiri-core/core.wit": "tobkiri:core@1.0.0",
    "wit/deps/ai-gateway-tool-caller-python/pack.wit":
        "local:ai-gateway-tool-caller-python@1.0.0",
    "wit/deps/ai-usage-cost-python/pack.wit": "local:ai-usage-cost-python@1.0.0",
    "wit/deps/ai-stream-normalizer-python/pack.wit":
        "local:ai-stream-normalizer-python@1.0.0",
    "wit/deps/ai-tool-bridge-ordered-python/pack.wit":
        "local:ai-tool-bridge-ordered-python@2.0.0",
}
# Fixed reviewed pins. Regenerating extraction evidence cannot approve new bytes.
REVIEWED_SHA256 = {
    'python/app.py': (
        '17de95bc837421e64b9cc6b48830e7dcce270cced8b10e4505eae7c3f8b7c8d9'
    ),
    'python/gateway_finalizer_adapter.py': (
        '3292fed55af50ac8252ec6eb3df42b7d884bcc9635df64d25e96759463951a35'
    ),
    'python/inventory.json': (
        '5fb62c5dfa9a5fa2aa4b1f62081394e66484da46d9f536f0cf3af0fcb0d292ca'
    ),
    'python/ordered_json.py': (
        '7df89c2e5275fdc795c13e0d70d6ec69f7f022aa968c58a3c1b1637e0eee2158'
    ),
    'python/ordered_json_v2.py': (
        '495b613a37807cf10e831a2332b7f401148d3aaec29ec84c16a22f0a2c05d3a2'
    ),
    'python/original_gateway_finalizer.py': (
        '73ddc86e677f93ced70e0ce2d352b94cd7a5238e15b438d0bc046b31417a01c2'
    ),
    'python/provenance.json': (
        '129862fc28f1e37643b13e982813997b0b6722fce52841c3bc66c286744e43c0'
    ),
    'wit/deps/ai-gateway-tool-caller-python/pack.wit': (
        '34cad3a098d41f9103386b0dab1988624c0de7f81721807b4da1e5c0f64bdd41'
    ),
    'wit/deps/ai-stream-normalizer-python/pack.wit': (
        '8a080544d35dfcd2a72d7195954c9a09953f7c1d9fe2619ac42bae8b1933bc07'
    ),
    'wit/deps/ai-tool-bridge-ordered-python/pack.wit': (
        '513454d839ac468205bc5b4ed93032625ed4f5d3064aa17f643f907e80bfeb78'
    ),
    'wit/deps/ai-usage-cost-python/pack.wit': (
        '196a11e594cc85d0213a6ad6e5d62e3e5b8921e78e24a75fef647acd28d59e50'
    ),
    'wit/deps/tobkiri-core/core.wit': (
        'f2c69f18f82e6fcea6ab47aa477fc53a81259bac00e3b47aff66b3a58d9d5acb'
    ),
    'wit/pack.wit': (
        'b260bfcf0822d4bcd4006ff041625a6cca472d89cb5232c6188deff7a51ff952'
    ),
}
REVIEWED_EVIDENCE_SHA256 = {
    'packs/local.ai-gateway-finalizer-python/README.md': (
        '9ed3d9aeaab169f6156149be9b5ea8eab9b65eb2882e3652632c4c498c890b57'
    ),
    'packs/local.ai-gateway-finalizer-python/source-cases.json': (
        'dbacb05a9e238b425c0910ca239f09aba3dc8c1ac63a32b2c2a6da734f2d9706'
    ),
}
REVIEWED_EXTRACTOR_SHA256 = (
    '46c49cd9a1d33bbbde8d11c94dc3bf8a240ba15780f47c99e56eb2167698e20e'
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    content = json.dumps(value, sort_keys=True, indent=2, allow_nan=False)
    return (content + "\n").encode()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()


def _relative(value: str) -> PurePosixPath:
    if (type(value) is not str or not value or any(c in value for c in "\\:\0")):
        raise ValueError("source paths must be normalized and relative")
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or ".." in path.parts
            or path.as_posix() != value):
        raise ValueError("source paths must be normalized and relative")
    return path


def _record(path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "bytes": len(data), "sha256": _digest(data)}


def _read(relative: str) -> bytes:
    return preparation.bounded_read(ROOT.joinpath(*_relative(relative).parts))


def _producer_destination(stage_path: str) -> str:
    # Preserve the declared staged python/ layout; the isolated SDK producer
    # captures these bytes at its import root so absolute source imports work.
    return stage_path.removeprefix("python/")


def _closure() -> None:
    """Require exactly the declared files, never observer answers or providers."""
    if tuple(sorted(DESTINATIONS.items())) != DECLARED_DESTINATIONS:
        raise ValueError("finalizer staging closure differs from declaration")
    if len(DESTINATIONS) != 13 or set(REVIEWED_SHA256) != set(DESTINATIONS):
        raise ValueError("reviewed pins must cover the exact 13-file closure")
    if set(REVIEWED_EVIDENCE_SHA256) != set(OBSERVER_EVIDENCE):
        raise ValueError("observer evidence pins differ from declaration")


def _wit_identities(files: dict[str, bytes]) -> None:
    """Check source package identities, not generated bindings or typed ABI."""
    identities = []
    for path, expected in WIT_PACKAGES.items():
        # All input bytes are canonical, pinned sources. These checks catch
        # accidental source-plan/package collisions; they are not a WIT parser.
        source = files[path].decode("utf-8")
        declarations = re.findall(r"(?m)^\s*package\s+([^;]+);", source)
        if declarations != [expected]:
            raise ValueError(f"mismatched WIT package identity: {path}")
        identities.extend(declarations)
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate WIT package identity")
    imports = re.findall(
        r"(?m)^\s*import\s+([^;]+);", files["wit/pack.wit"].decode("utf-8"),
    )
    if imports != list(REQUIRED_INTERFACES):
        raise ValueError("finalizer direct typed imports differ from declaration")
    functions = re.findall(
        r"(?m)^\s*(%?[a-z][\w-]*):\s*func",
        files["wit/pack.wit"].decode("utf-8"),
    )
    # `stream` is a WIT type keyword. Escaping it retains the external name.
    # This static spelling check does not establish generated Python ABI names.
    if functions != ["runtime-info", "generate", "%stream"]:
        raise ValueError("finalizer function spelling requires escaped %stream")
    caller = files["wit/deps/ai-gateway-tool-caller-python/pack.wit"]
    if f"import {TRANSITIVE_WIT_INTERFACE};".encode() not in caller:
        raise ValueError("canonical caller transitive WIT dependency changed")


def _finalizer_tree() -> None:
    """Reject undeclared inputs, linked nodes and unbounded directory listings."""
    allowed = {
        "README.md", "source-cases.json", "wit/pack.wit",
        *(f"python/{name}" for name in SOURCE_MODULES),
    }
    found: set[str] = set()

    def visit(descriptor: int, prefix: str = "") -> None:
        names = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if len(names) >= len(allowed) + 2:
                    raise ValueError("finalizer tree contains undeclared inputs")
                names.append(entry.name)
        for name in sorted(names):
            relative = f"{prefix}/{name}" if prefix else name
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if relative in {"python", "wit"} and stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
            elif (relative in allowed and stat.S_ISREG(info.st_mode)
                  and info.st_nlink == 1):
                found.add(relative)
            else:
                raise ValueError(f"undeclared or unsafe finalizer input: {relative}")

    descriptor = None
    try:
        descriptor = preparation._directory(ROOT / PREFIX)
        visit(descriptor)
    except OSError as error:
        raise ValueError("finalizer source tree is missing or unsafe") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if found != allowed:
        raise ValueError("finalizer source tree is missing reviewed files")


def _review() -> tuple[dict[str, bytes], dict[str, Any]]:
    """Capture exact source evidence without importing a guest or dependency."""
    _closure()
    _finalizer_tree()
    captured: dict[str, bytes] = {}

    def capture(relative: str) -> bytes:
        if relative not in captured:
            data = _read(relative)
            if sum(map(len, captured.values())) + len(data) > MAX_TOTAL_BYTES:
                raise ValueError("captured source set exceeds staging byte bound")
            captured[relative] = data
        return captured[relative]

    # Imported tooling is trusted Python. This detects review drift, not a
    # sandbox or authentication of arbitrary code before importing it.
    if _digest(capture(EXTRACTOR)) != REVIEWED_EXTRACTOR_SHA256:
        raise ValueError("extractor differs from reviewed SHA-256")
    files, bindings = {}, []
    for stage_path, relative in sorted(DESTINATIONS.items()):
        _relative(stage_path)
        data = capture(relative)
        if _digest(data) != REVIEWED_SHA256[stage_path]:
            raise ValueError(f"source differs from reviewed SHA-256: {relative}")
        files[stage_path] = data
        bindings.append({
            **_record(relative, data), "destination": stage_path,
            "producer_destination": _producer_destination(stage_path),
        })
    _wit_identities(files)
    observers = []
    for relative in OBSERVER_EVIDENCE:
        data = capture(relative)
        if _digest(data) != REVIEWED_EVIDENCE_SHA256[relative]:
            raise ValueError(f"observer differs from reviewed SHA-256: {relative}")
        observers.append(_record(relative, data))
    inventory = preparation._strict_json(files["python/inventory.json"])
    for binding in inventory["sources"]:
        relative = binding["path"]
        if _record(relative, capture(relative)) != binding:
            raise ValueError(f"original inventory source changed: {relative}")
    provenance = preparation.verify(ROOT / PREFIX / "python")
    if provenance != preparation._strict_json(files["python/provenance.json"]):
        raise ValueError("reconstructed provenance changed during staging")
    for dependency in provenance["transport_dependencies"]:
        relative = dependency["path"]
        if _record(relative, capture(relative)) != dependency:
            raise ValueError(f"transport dependency source changed: {relative}")
    for relative in (STAGER, EXTRACTOR):
        capture(relative)
    evidence = {
        "format": "tobkiri.gateway-finalizer-source-stage/1",
        "scope": "source-stage-only-not-execution", "protocol": PROTOCOL,
        "finalizer_interface": INTERFACE,
        "source_bindings": bindings,
        "source_bindings_sha256": _digest(_canonical(bindings)),
        "inventory_sources": inventory["sources"],
        "transport_dependency_evidence": provenance["transport_dependencies"],
        "observer_evidence": observers,
        "observer_evidence_in_guest_sources": False,
        "staging_script": _record(STAGER, captured[STAGER]),
        "extraction_script": _record(EXTRACTOR, captured[EXTRACTOR]),
        "original_source_verification": "verbatim-with-two-explicit-rewrites",
        "source_rewrite_evidence": provenance["rewrites"],
        "typed_abi_verification_performed": False,
        "dependency_closure": {
            "required_typed_provider_imports": list(REQUIRED_INTERFACES),
            "transitive_wit_interface": TRANSITIVE_WIT_INTERFACE,
            "staged_source_count": 13, "source_dependency_count": 7,
            "python_module_count": 5, "python_evidence_file_count": 2,
            "wit_file_count": 6,
            "ambient_dependency_directory": False,
            "composition_dependency_directory": "no-ambient-dependencies",
            "other_provider_implementation_included": False,
        },
        **dict.fromkeys(NO_EXECUTION, False),
    }
    _finalizer_tree()
    if any(_read(relative) != data for relative, data in captured.items()):
        raise ValueError("source changed during staging")
    return files, evidence


def reviewed_inputs() -> bytes:
    """Return bound source evidence, never a producer or execution receipt."""
    _, evidence = _review()
    return _json_bytes(evidence)

def _write(descriptor: int, relative: str, content: bytes) -> None:
    """Create a fresh file through anchored, no-follow directory descriptors."""
    path = _relative(relative)
    directory = os.dup(descriptor)
    target = None
    try:
        for part in path.parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        target = os.open(
            path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory,
        )
        remaining = memoryview(content)
        while remaining:
            written = os.write(target, remaining)
            if not written:
                raise ValueError("incomplete stage write")
            remaining = remaining[written:]
    finally:
        if target is not None:
            os.close(target)
        os.close(directory)


def stage(output: Path) -> dict[str, Any]:
    """Create a fresh private source plan without compile/run/activation.

    The existing destination parent must remain trusted. Failed fresh stages
    remain available for diagnosis; no existing path is replaced or deleted.
    """
    output = Path(output)
    if ".." in output.parts or any(c in str(output) for c in "\\:\0"):
        raise ValueError("stage destination must be a normalized no-link path")
    output = output.absolute()
    parent = descriptor = None
    try:
        parent = preparation._directory(output.parent)
        try:
            os.stat(output.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("stage destination must be fresh")
        files, evidence = _review()
        plan = {
            "format": "tobkiri.python-build/3", "stub_wasi": True,
            "wit": "wit/pack.wit", "world": "pack", "entrypoint": "app",
            "sources": [
                {"source": name, "destination": _producer_destination(name),
                 "sha256": _digest(data)} for name, data in sorted(files.items())
            ],
            "outer_exports": ["tobkiri:core/lifecycle@1.0.0", INTERFACE],
            "allowed_imports": [
                *(f"tobkiri:core/{name}@1.0.0"
                  for name in ("types", "flow", "clock", "random", "audit")),
                *REQUIRED_INTERFACES,
            ],
        }
        plan_bytes = _json_bytes(plan)
        evidence["build_plan_sha256"] = _digest(plan_bytes)
        os.mkdir(output.name, mode=0o700, dir_fd=parent)
        descriptor = os.open(
            output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        for name, data in sorted(files.items()):
            _write(descriptor, name, data)
        _write(descriptor, "plan.json", plan_bytes)
        _write(descriptor, "source-stage.json", _json_bytes(evidence))
        actual = os.stat(output.name, dir_fd=parent, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("stage destination changed during write")
        return plan
    except OSError as error:
        raise ValueError(
            "stage requires fresh files and an existing no-link trusted parent",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def main() -> None:
    """Stage only the declared finalizer sources, never a producer or runtime."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.output), sort_keys=True))


if __name__ == "__main__":
    main()
