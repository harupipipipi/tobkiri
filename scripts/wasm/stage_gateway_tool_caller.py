"""Stage exact original Gateway helper sources, never build or execute a guest.

The SDK format-3 plan captures only explicitly reviewed files. Its producer uses
an isolated source directory and an absent private dependency directory; this
script does not invoke that producer or create a component or runtime Pack.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

import prepare_gateway_tool_caller as preparation

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "packs/local.ai-gateway-tool-caller-python"
PACK = ROOT / PREFIX
STAGER = "scripts/wasm/stage_gateway_tool_caller.py"
EXTRACTOR = "scripts/wasm/prepare_gateway_tool_caller.py"
INTERFACE = "local:ai-gateway-tool-caller-python/caller@1.0.0"
BRIDGE_INTERFACE = "local:ai-tool-bridge-ordered-python/bridge@2.0.0"
PROTOCOL = "ordered-finite-json-v2"
MAX_TOTAL_BYTES = 32 * 1024 * 1024
NO_EXECUTION = (
    "build_performed", "wasm_execution_performed", "native_core_acceptance",
    "full_host_replay_performed", "production_enabled",
)
DESTINATIONS = {
    **{name: f"{PREFIX}/python/{name}" for name in (
        "original_gateway_tool_caller.py", "gateway_tool_caller_adapter.py",
        "app.py", "inventory.json", "provenance.json",
    )},
    **{Path(path).name: path for path in preparation.CODEC_PATHS},
    "wit/pack.wit": f"{PREFIX}/wit/pack.wit",
    "wit/deps/core.wit": preparation.CORE_WIT,
    "wit/deps/bridge-v2.wit": preparation.BRIDGE_WIT,
}
# Reviewed source bytes, refreshed only after explicit source review.
REVIEWED_SHA256 = {
    "app.py": (
        "5e4b8be7be3fc0909b34e60ecf06094d203387ddf54d65eabd188ed6fd781336"
    ),
    "gateway_tool_caller_adapter.py": (
        "3b606a2ae967d41fba880279f0416c1aec05ca08e4cefdbaa9d2c39955c6a583"
    ),
    "inventory.json": (
        "7416ba4a38059dc3e18452abe947bb8dc6b0cfb82ead6a8a2e2dc95b30d556cc"
    ),
    "ordered_json.py": (
        "7df89c2e5275fdc795c13e0d70d6ec69f7f022aa968c58a3c1b1637e0eee2158"
    ),
    "ordered_json_v2.py": (
        "495b613a37807cf10e831a2332b7f401148d3aaec29ec84c16a22f0a2c05d3a2"
    ),
    "original_gateway_tool_caller.py": (
        "9b0d671807095cf2211b8bb51db703cea563133b2e8d935a50e9f77ea40ff1b1"
    ),
    "provenance.json": (
        "569a9f60a6ce0a72b6f23a91611967c09b646f328f60844de79e9b87e1185c36"
    ),
    "wit/deps/bridge-v2.wit": (
        "513454d839ac468205bc5b4ed93032625ed4f5d3064aa17f643f907e80bfeb78"
    ),
    "wit/deps/core.wit": (
        "f2c69f18f82e6fcea6ab47aa477fc53a81259bac00e3b47aff66b3a58d9d5acb"
    ),
    "wit/pack.wit": (
        "34cad3a098d41f9103386b0dab1988624c0de7f81721807b4da1e5c0f64bdd41"
    ),
}
REVIEWED_EXTRACTOR_SHA256 = (
    "86abbc22c1fdf1c24634e3adc65c0c286b4c253a9d7db1f6ee8ff8542b3c14aa"
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


def _caller_tree() -> None:
    """Reject undeclared caller inputs, including hidden files and linked nodes."""
    allowed = {
        "README.md", "wit/pack.wit",
        *(f"python/{name}" for name in (
            "original_gateway_tool_caller.py", "gateway_tool_caller_adapter.py",
            "app.py", "inventory.json", "provenance.json",
        )),
    }
    found: set[str] = set()

    def visit(descriptor: int, prefix: str = "") -> None:
        names = os.listdir(descriptor)
        if len(names) > len(allowed) + 2:
            raise ValueError("caller source tree contains undeclared inputs")
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
                raise ValueError(f"undeclared or unsafe caller input: {relative}")

    descriptor = None
    try:
        descriptor = preparation._directory(PACK)
        visit(descriptor)
    except OSError as error:
        raise ValueError("caller source tree is missing or unsafe") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if found != allowed:
        raise ValueError("caller source tree is missing reviewed files")


def _review() -> tuple[dict[str, bytes], dict[str, Any]]:
    """Reconstruct provenance and capture every input without importing a guest."""
    _caller_tree()
    if set(REVIEWED_SHA256) != set(DESTINATIONS):
        raise ValueError("reviewed source pins must cover the exact staging closure")
    captured: dict[str, bytes] = {}

    def capture(relative: str) -> bytes:
        if relative not in captured:
            data = _read(relative)
            if sum(map(len, captured.values())) + len(data) > MAX_TOTAL_BYTES:
                raise ValueError("captured source set exceeds staging byte bound")
            captured[relative] = data
        return captured[relative]

    # Imported tooling is trusted code; this pin detects review drift, not a
    # sandbox or authentication of arbitrary Python before importing it.
    if _digest(capture(EXTRACTOR)) != REVIEWED_EXTRACTOR_SHA256:
        raise ValueError("extractor differs from reviewed SHA-256")
    for relative in DESTINATIONS.values():
        capture(relative)
    files = {}
    bindings = []
    for destination, relative in sorted(DESTINATIONS.items()):
        _relative(destination)
        data = captured[relative]
        if _digest(data) != REVIEWED_SHA256[destination]:
            raise ValueError(f"source differs from reviewed SHA-256: {relative}")
        files[destination] = data
        bindings.append({**_record(relative, data), "destination": destination})
    inventory = preparation._strict_json(files["inventory.json"])
    # Capture every original inventory input, not merely the selected slices.
    for binding in inventory["sources"]:
        relative = binding["path"]
        data = captured.get(relative)
        if data is None:
            data = capture(relative)
        if _record(relative, data) != binding:
            raise ValueError(f"original inventory source changed: {relative}")
    provenance = preparation.verify(PACK / "python")
    if provenance != preparation._strict_json(files["provenance.json"]):
        raise ValueError("reconstructed provenance changed during staging")
    for relative in (STAGER, EXTRACTOR):
        capture(relative)
    evidence = {
        "format": "tobkiri.gateway-tool-caller-source-stage/1",
        "scope": "source-stage-only-not-execution",
        "protocol": PROTOCOL,
        "source_bindings": bindings,
        "source_bindings_sha256": _digest(_canonical(bindings)),
        "inventory_sources": inventory["sources"],
        "staging_script": _record(STAGER, captured[STAGER]),
        "extraction_script": _record(EXTRACTOR, captured[EXTRACTOR]),
        "original_source_verification": "verbatim-reconstruction",
        "dependency_closure": {
            "required_typed_import": BRIDGE_INTERFACE,
            "source_dependency_count": 4,
            "ambient_dependency_directory": False,
            "composition_dependency_directory": "no-ambient-dependencies",
            "local_bridge_implementation_included": False,
        },
        **dict.fromkeys(NO_EXECUTION, False),
    }
    _caller_tree()
    if any(_read(relative) != data for relative, data in captured.items()):
        raise ValueError("source changed during staging")
    return files, evidence


def reviewed_inputs() -> bytes:
    """Return deterministic source-stage evidence, not a build or replay receipt."""
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
    """Write a fresh source-only plan; never overwrite, compile, run, or activate.

    The existing destination parent must remain trusted. A failed fresh stage is
    retained for diagnosis, not automatically removed or replaced.
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
                {"source": name, "destination": name, "sha256": _digest(data)}
                for name, data in sorted(files.items())
            ],
            "outer_exports": ["tobkiri:core/lifecycle@1.0.0", INTERFACE],
            "allowed_imports": [
                *(f"tobkiri:core/{name}@1.0.0"
                  for name in ("types", "flow", "clock", "random", "audit")),
                BRIDGE_INTERFACE,
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
    """Stage source evidence only, without invoking any compiler or runtime."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    plan = stage(parser.parse_args().output)
    print(json.dumps(plan, sort_keys=True))


if __name__ == "__main__":
    main()
