"""Stage exact original finalization-provider sources, never produce/run a guest.

Each independently selected provider gets one SDK format-3 isolated source plan.
Only explicitly reviewed source bytes enter it; dependency directories, generated
bindings, interpreter initialization and local authority substitutes do not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

import prepare_gateway_finalization_providers as preparation

ROOT = Path(__file__).resolve().parents[2]
STAGER = "scripts/wasm/stage_gateway_finalization_providers.py"
EXTRACTOR = "scripts/wasm/prepare_gateway_finalization_providers.py"
PROTOCOL = "ordered-finite-json-v2"
MAX_TOTAL_BYTES = 32 * 1024 * 1024
NO_EXECUTION = (
    "build_performed", "wasm_execution_performed", "native_core_acceptance",
    "full_host_replay_performed", "production_enabled",
)
SOURCE_MODULES = {
    "usage-cost": ("original_usage_cost.py", "usage_cost_adapter.py", "app.py",
                   "inventory.json", "provenance.json"),
    "stream-normalizer": ("original_stream_normalizer.py",
                          "stream_normalizer_adapter.py", "app.py",
                          "inventory.json", "provenance.json"),
}
PREFIXES = {provider: f"packs/local.ai-{provider}-python"
            for provider in SOURCE_MODULES}
INTERFACES = {provider: f"local:ai-{provider}-python/{provider}@1.0.0"
              for provider in SOURCE_MODULES}
DESTINATIONS = {
    provider: {
        **{f"python/{name}": f"{PREFIXES[provider]}/python/{name}" for name in names},
        **{f"python/{Path(path).name}": path for path in preparation.CODEC_PATHS},
        "wit/pack.wit": f"{PREFIXES[provider]}/wit/pack.wit",
        "wit/deps/tobkiri-core/core.wit": preparation.CORE_WIT,
    }
    for provider, names in SOURCE_MODULES.items()
}
# Explicit reviewed pins. Regenerating provenance alone cannot re-approve source.
REVIEWED_SHA256 = {
    "stream-normalizer": {
        "python/app.py": (
            "bfbfc5a8d2d6fa184f162585c370b96eb23b51f46d47a4aa1dd441906eb9e4f2"
        ),
        "python/inventory.json": (
            "24df2873314ce68f26890fbe2bbb8745dc2de9562f18753917bcaffaf6161bb2"
        ),
        "python/ordered_json.py": (
            "7df89c2e5275fdc795c13e0d70d6ec69f7f022aa968c58a3c1b1637e0eee2158"
        ),
        "python/ordered_json_v2.py": (
            "495b613a37807cf10e831a2332b7f401148d3aaec29ec84c16a22f0a2c05d3a2"
        ),
        "python/original_stream_normalizer.py": (
            "611e174b7b101ffe67960b80501421261e6a9322977b2a3c991253a1c28fc38f"
        ),
        "python/provenance.json": (
            "fd09dff6ce0b82489dec86b1c931dd14050ccfb4a8b2d155bce7bb212657c65f"
        ),
        "python/stream_normalizer_adapter.py": (
            "c77c5c0f14c28ec60bba0586102743556e0438e7f581792c336371ebc12fed20"
        ),
        "wit/deps/tobkiri-core/core.wit": (
            "f2c69f18f82e6fcea6ab47aa477fc53a81259bac00e3b47aff66b3a58d9d5acb"
        ),
        "wit/pack.wit": (
            "8a080544d35dfcd2a72d7195954c9a09953f7c1d9fe2619ac42bae8b1933bc07"
        ),
    },
    "usage-cost": {
        "python/app.py": (
            "2226cb07e6b0b6af8a9f8285f0fbebc75b041c7472cf506c8284e5f10a5c204f"
        ),
        "python/inventory.json": (
            "af5dd63cae7fc513d378d937d6777e0a0ae3992dd600a7e28bd465ae47be6d80"
        ),
        "python/ordered_json.py": (
            "7df89c2e5275fdc795c13e0d70d6ec69f7f022aa968c58a3c1b1637e0eee2158"
        ),
        "python/ordered_json_v2.py": (
            "495b613a37807cf10e831a2332b7f401148d3aaec29ec84c16a22f0a2c05d3a2"
        ),
        "python/original_usage_cost.py": (
            "b4eccb3633fba3341599ed017271c427a20b5628a8629fe7e4c626b6261b4954"
        ),
        "python/provenance.json": (
            "4448fe4fec7acdaf1b77aa0a52703ad459eb26396ad769f86fa6db15f2900322"
        ),
        "python/usage_cost_adapter.py": (
            "e300acb490bbc851971c7972be299ae7634949a350644f2b34087e6c382ea2b8"
        ),
        "wit/deps/tobkiri-core/core.wit": (
            "f2c69f18f82e6fcea6ab47aa477fc53a81259bac00e3b47aff66b3a58d9d5acb"
        ),
        "wit/pack.wit": (
            "196a11e594cc85d0213a6ad6e5d62e3e5b8921e78e24a75fef647acd28d59e50"
        ),
    },
}
REVIEWED_EXTRACTOR_SHA256 = (
    "d212e0e3fa782a03dcd4d4a772f8ddf7158dad5eb437b7d7373e24939074fa80"
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


def _provider(provider: str) -> None:
    if type(provider) is not str or provider not in SOURCE_MODULES:
        raise ValueError("unknown finalization provider")


def _producer_destination(stage_path: str) -> str:
    # Preserve the declared staged python/ layout; the isolated SDK producer
    # captures these bytes at its import root so absolute source imports work.
    return stage_path.removeprefix("python/")


def _provider_tree(provider: str) -> None:
    """Reject undeclared provider inputs, hidden files and linked source nodes."""
    allowed = {"README.md", "wit/pack.wit",
               *(f"python/{name}" for name in SOURCE_MODULES[provider])}
    found: set[str] = set()

    def visit(descriptor: int, prefix: str = "") -> None:
        names = []
        # Bound directory-entry allocation before materializing/sorting names.
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if len(names) >= len(allowed) + 2:
                    raise ValueError("provider source tree contains undeclared inputs")
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
                raise ValueError(f"undeclared or unsafe provider input: {relative}")

    descriptor = None
    try:
        descriptor = preparation._directory(ROOT / PREFIXES[provider])
        visit(descriptor)
    except OSError as error:
        raise ValueError("provider source tree is missing or unsafe") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if found != allowed:
        raise ValueError("provider source tree is missing reviewed files")


def _review(provider: str) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Reconstruct every original binding without importing provider code."""
    _provider(provider)
    _provider_tree(provider)
    destinations, pins = DESTINATIONS[provider], REVIEWED_SHA256[provider]
    if set(pins) != set(destinations):
        raise ValueError("reviewed source pins must cover the exact staging closure")
    captured: dict[str, bytes] = {}

    def capture(relative: str) -> bytes:
        if relative not in captured:
            data = _read(relative)
            if sum(map(len, captured.values())) + len(data) > MAX_TOTAL_BYTES:
                raise ValueError("captured source set exceeds staging byte bound")
            captured[relative] = data
        return captured[relative]

    # This pin detects drift of trusted imported tooling. It is not a sandbox
    # or authentication mechanism for arbitrary Python before importing it.
    if _digest(capture(EXTRACTOR)) != REVIEWED_EXTRACTOR_SHA256:
        raise ValueError("extractor differs from reviewed SHA-256")
    files, bindings = {}, []
    for stage_path, relative in sorted(destinations.items()):
        _relative(stage_path)
        data = capture(relative)
        if _digest(data) != pins[stage_path]:
            raise ValueError(f"source differs from reviewed SHA-256: {relative}")
        files[stage_path] = data
        bindings.append({**_record(relative, data), "destination": stage_path,
                         "producer_destination": _producer_destination(stage_path)})
    inventory = preparation._strict_json(files["python/inventory.json"])
    for binding in inventory["sources"]:
        relative = binding["path"]
        if _record(relative, capture(relative)) != binding:
            raise ValueError(f"original inventory source changed: {relative}")
    provenance = preparation.verify(provider, ROOT / PREFIXES[provider] / "python")
    if provenance != preparation._strict_json(files["python/provenance.json"]):
        raise ValueError("reconstructed provenance changed during staging")
    for reference in provenance["staged_references"]:
        if (destinations.get(reference["stage_path"]) != reference["path"]
                or not reference["copy_unchanged"]
                or _record(reference["path"], files[reference["stage_path"]])
                != {key: reference[key] for key in ("path", "bytes", "sha256")}):
            raise ValueError("declared staged reference differs from source plan")
    for relative in (STAGER, EXTRACTOR):
        capture(relative)
    evidence = {
        "format": "tobkiri.gateway-finalization-provider-source-stage/1",
        "scope": "source-stage-only-not-execution", "provider": provider,
        "protocol": PROTOCOL, "provider_interface": INTERFACES[provider],
        "source_bindings": bindings,
        "source_bindings_sha256": _digest(_canonical(bindings)),
        "inventory_sources": inventory["sources"],
        "staging_script": _record(STAGER, captured[STAGER]),
        "extraction_script": _record(EXTRACTOR, captured[EXTRACTOR]),
        "original_source_verification": "verbatim-reconstruction",
        "dependency_closure": {
            "required_typed_provider_imports": [], "source_dependency_count": 3,
            "ambient_dependency_directory": False,
            "composition_dependency_directory": "no-ambient-dependencies",
            "other_provider_implementation_included": False,
        },
        **dict.fromkeys(NO_EXECUTION, False),
    }
    _provider_tree(provider)
    if any(_read(relative) != data for relative, data in captured.items()):
        raise ValueError("source changed during staging")
    return files, evidence


def reviewed_inputs(provider: str) -> bytes:
    """Return source-stage evidence, never a build or replay receipt."""
    _, evidence = _review(provider)
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


def stage(provider: str, output: Path) -> dict[str, Any]:
    """Create one fresh source-only provider plan without compile/run/activation.

    The existing destination parent must remain trusted. A failed fresh stage is
    retained for diagnosis rather than automatically removed or replaced.
    """
    _provider(provider)
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
        files, evidence = _review(provider)
        plan = {
            "format": "tobkiri.python-build/3", "stub_wasi": True,
            "wit": "wit/pack.wit", "world": "pack", "entrypoint": "app",
            "sources": [
                {"source": name, "destination": _producer_destination(name),
                 "sha256": _digest(data)} for name, data in sorted(files.items())
            ],
            "outer_exports": ["tobkiri:core/lifecycle@1.0.0", INTERFACES[provider]],
            "allowed_imports": [
                f"tobkiri:core/{name}@1.0.0"
                for name in ("types", "flow", "clock", "random", "audit")
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
    """Stage one original provider without invoking any compiler or runtime."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=SOURCE_MODULES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.provider, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
