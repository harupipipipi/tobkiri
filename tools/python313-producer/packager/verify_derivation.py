"""Pure source/lock derivation verification; never builds or imports upstream."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tomllib

HERE = Path(__file__).resolve().parent


def validate_graph(packages: list[dict], root: tuple[str, str]) -> None:
    """Require unique IDs, resolvable edges and an exact reachable closure."""
    identities = [(p["name"], p["version"], p.get("source")) for p in packages]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate lock package")
    starts = [i for i, p in enumerate(packages)
              if (p["name"], p["version"]) == root and "source" not in p]
    if len(starts) != 1:
        raise ValueError("invalid graph root")
    edges = {}
    for index, package in enumerate(packages):
        resolved = []
        for dependency in package.get("dependencies", []):
            parts = dependency.split()
            if len(parts) not in (1, 2):
                raise ValueError("unsupported lock dependency identity")
            matches = [i for i, candidate in enumerate(packages)
                       if candidate["name"] == parts[0]
                       and (len(parts) == 1 or candidate["version"] == parts[1])]
            if len(matches) != 1:
                raise ValueError("dangling or ambiguous dependency edge")
            resolved.append(matches[0])
        if len(set(resolved)) != len(resolved):
            raise ValueError("duplicate dependency edge")
        edges[index] = resolved
    pending, visited = starts[:], set()
    while pending:
        index = pending.pop()
        if index not in visited:
            visited.add(index)
            pending.extend(edges[index])
    if len(visited) != len(packages):
        raise ValueError("unreachable lock package")


def resolved_edges(package: dict, packages: list[dict]) -> set[tuple]:
    """Resolve lock edge spellings to exact package identities, failing closed."""
    result = set()
    for dependency in package.get("dependencies", []):
        parts = dependency.split()
        if len(parts) not in (1, 2):
            raise ValueError("unsupported lock dependency identity")
        matches = [p for p in packages if p["name"] == parts[0]
                   and (len(parts) == 1 or p["version"] == parts[1])]
        if len(matches) != 1:
            raise ValueError("dangling or ambiguous dependency edge")
        p = matches[0]
        identity = (p["name"], p["version"], p.get("source"))
        if identity in result:
            raise ValueError("duplicate dependency edge")
        result.add(identity)
    return result


def verify(upstream_lock: Path, upstream_build: Path) -> dict:
    """Compare registry IDs/checksums/dependencies and the exact strip function."""
    evidence = json.loads((HERE / "upstream-lock-identities.json").read_text())
    lock_bytes = upstream_lock.read_bytes()
    build_bytes = upstream_build.read_bytes()
    if hashlib.sha256(lock_bytes).hexdigest() != evidence["upstream_lock_sha256"]:
        raise ValueError("upstream lock hash mismatch")
    if hashlib.sha256(build_bytes).hexdigest() != evidence["upstream_build_rs_sha256"]:
        raise ValueError("reviewed upstream build.rs hash mismatch")
    upstream = tomllib.loads(lock_bytes.decode())["package"]
    local = tomllib.loads((HERE / "Cargo.lock").read_text())["package"]
    originals = {(p["name"], p["version"], p.get("source")): p for p in upstream}
    registry = [p for p in local if "source" in p]
    if registry != evidence["registry_packages"]:
        raise ValueError("registry derivation evidence mismatch")
    for original in evidence["upstream_registry_packages"]:
        identity = (original["name"], original["version"], original["source"])
        if originals.get(identity) != original:
            raise ValueError("upstream registry evidence changed")
    for package in registry:
        if package["source"] != "registry+https://github.com/rust-lang/crates.io-index":
            raise ValueError("non-registry dependency forbidden")
        identity = (package["name"], package["version"], package["source"])
        original = originals.get(identity)
        metadata = lambda p: {k: v for k, v in p.items() if k != "dependencies"}
        if original is None or metadata(original) != metadata(package):
            raise ValueError("registry identity or checksum changed")
        if not resolved_edges(package, local) <= resolved_edges(original, upstream):
            raise ValueError("dependency edge not in upstream")
    roots = [p for p in local if "source" not in p]
    expected_root = {
        "name": "tobkiri-python313-packager", "version": "0.1.0",
        "dependencies": ["anyhow", "wasm-encoder", "wasmparser", "zstd"],
    }
    if roots != [expected_root]:
        raise ValueError("unexpected local package identity or dependencies")
    validate_graph(local, (expected_root["name"], expected_root["version"]))
    original_function = build_bytes.decode().split("fn strip(input:", 1)[1]
    local_source = (HERE / "src/upstream_strip.rs").read_text()
    if local_source.split("pub fn strip(input:", 1)[1] != original_function:
        raise ValueError("upstream strip function changed")
    manifest = tomllib.loads((HERE / "Cargo.toml").read_text())
    expected_dependencies = {
        "anyhow": "=1.0.102", "zstd": "=0.13.3", "wasmparser": "=0.245.1",
        "wasm-encoder": {"version": "=0.245.1", "features": ["wasmparser"]},
    }
    expected_manifest = {
        "package": {
            "name": "tobkiri-python313-packager", "version": "0.1.0",
            "edition": "2024",
            "license": "Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT",
            "publish": False,
        },
        "workspace": {}, "dependencies": expected_dependencies,
    }
    if manifest != expected_manifest:
        raise ValueError("manifest differs from exact allowed schema")
    if (HERE / "build.rs").exists():
        raise ValueError("unexpected helper build script")
    return {"registry_packages": len(registry), "git_packages": 0,
            "strip_function": "exact upstream body", "compiled": False}


def main() -> None:
    """Print pure derivation evidence for supplied reviewed upstream bytes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-lock", type=Path, required=True)
    parser.add_argument("--upstream-build", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.upstream_lock, args.upstream_build), indent=2))


if __name__ == "__main__":
    main()
