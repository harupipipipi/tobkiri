"""Build the reviewed standalone packager and package real producer outputs."""
import argparse
import json
import os
from pathlib import Path
import sys

import asset_inputs
from build_native import GIB, HERE, fingerprint, sandbox_command
from build_runtimes import CARGO_CONFIG
from build_wasi import run_stage
import fetch_sdk
from packager import package, verify_derivation
import runtime_inputs

WORKER_RECIPE = '''set -eu
export PATH=/rust/bin:/usr/bin:/bin RUSTC=/rust/bin/rustc
export HOME=/work/home CARGO_HOME=/work/cargo-home CARGO_TARGET_DIR=/work/target
export CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0 CARGO_PROFILE_RELEASE_DEBUG=0
export CARGO_NET_OFFLINE=true
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=/usr/bin/gcc
export CC=/usr/bin/gcc CXX=/usr/bin/g++
/rust/bin/cargo test --frozen -j1 --release --manifest-path /helper/Cargo.toml
/rust/bin/cargo build --frozen -j1 --release --manifest-path /helper/Cargo.toml
'''


def package_recipe(manifest_sha256: str, worker_sha256: str) -> str:
    """Keep the original invocation with identities of this actual rebuild."""
    for digest in (manifest_sha256, worker_sha256):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("literal SHA-256 required")
    return f'''set -eu
/usr/bin/python3 -I -B /helper/package.py \\
  --input-root /inputs --manifest /manifest.json \\
  --manifest-sha256 {manifest_sha256} \\
  --worker /worker --worker-sha256 {worker_sha256} \\
  --destination /work/assets
'''


def verify_outputs(root: Path, manifest_sha: str, worker_sha: str) -> dict:
    """Require every actual output to match the successful packager receipt."""
    receipt_path = root / "package-receipt.json"
    fingerprint(receipt_path, 1024 * 1024)
    receipt = json.loads(receipt_path.read_bytes())
    if (receipt.get("status") != "packaged; not runtime validated"
            or receipt.get("input_manifest_sha256") != manifest_sha
            or receipt.get("worker_sha256") != worker_sha
            or set(receipt.get("artifacts", {})) != package.ASSETS):
        raise ValueError("packaged assets lack a matching successful receipt")
    if {p.name for p in root.iterdir()} != package.ASSETS | {"package-receipt.json"}:
        raise ValueError("unexpected packaged asset inventory")
    total, outputs = 0, {}
    for name, recorded in receipt["artifacts"].items():
        identity = fingerprint(root / name, package.MAX_OUTPUT)
        if identity != {k: recorded[k] for k in ("bytes", "sha256")}:
            raise ValueError("packaged asset differs from its receipt")
        total += identity["bytes"]
        if total > package.MAX_TOTAL:
            raise ValueError("packaged asset aggregate bound exceeded")
        outputs[name] = identity
    return outputs


def run(source: Path, registry: Path, rust: Path, git: Path, wasi: Path,
        runtimes: Path, destination: Path) -> None:
    """Use only successful verified predecessor outputs in Linux CI isolation."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("asset rebuilding is restricted to Linux CI")
    if (not destination.is_absolute() or not destination.parent.is_dir()
            or destination.parent.resolve() != destination.parent
            or not runtimes.is_absolute() or runtimes.resolve() != runtimes):
        raise ValueError("physical input and new destination paths required")
    destination.mkdir(mode=0o700)
    inputs, snapshots = runtime_inputs.verify(source, registry, rust, git, wasi)
    runtime_receipt_path = runtimes / "runtimes-build.json"
    runtime_report = runtime_inputs.report(runtime_receipt_path)
    if runtime_report["inputs"] != inputs or not runtime_report["inputs_unchanged"]:
        raise ValueError("runtime build does not match verified inputs")
    for variant in ("sync", "async"):
        path = runtimes / f"work/runtime-{variant}" / asset_inputs.RUNTIME_PATH
        if fingerprint(path, 32 * 1024 * 1024) != runtime_report["outputs"][variant]:
            raise ValueError("actual runtime differs from successful build receipt")
    helper = HERE / "packager"
    derivation = verify_derivation.verify(source / "componentize-py/Cargo.lock",
                                          source / "componentize-py/build.rs")
    snapshots[str(helper)] = fetch_sdk.inventory(helper)
    manifest, input_mounts, identities = asset_inputs.manifest(
        asset_inputs.selected(source, wasi, runtimes))
    manifest_path = destination / "asset-inputs.json"
    manifest_path.write_bytes(manifest)
    manifest_identity = fingerprint(manifest_path, package.MAX_MANIFEST)
    work = destination / "work"
    work.mkdir(mode=0o700)
    for name in ("home", "cargo-home"):
        (work / name).mkdir(mode=0o700)
    config = work / "cargo-home/config.toml"
    config.write_text(CARGO_CONFIG)
    config_identity = fingerprint(config)
    common_mounts = (
        (helper, "/helper"), (registry / "sources", "/registry/sources"),
        (rust / "toolchain", "/rust"), (config, "/work/cargo-home/config.toml"),
    )
    runtime_identity = fingerprint(runtime_receipt_path)
    receipt = {"format": "tobkiri.python313-assets-rebuild/1", "outcome": "pending",
               "inputs": inputs, "runtime_receipt": runtime_identity,
               "helper_derivation": derivation, "manifest": manifest_identity,
               "helper_source_files": snapshots[str(helper)],
               "input_records": len(identities),
               "input_bytes": sum(item["bytes"] for item in identities.values()),
               "stages": [], "guest_execution": False, "componentization": False,
               "producer_cli": False, "production_enabled": False,
               "wasmtime_validation": False, "historical_binary_identity_claimed": False,
               "recipe_delta": "same packaging invocation with current input and worker hashes",
               "tar_delta": "sorted regular members; normalized uid/gid/mtime and modes"}
    receipt_path = destination / "assets-build.json"
    try:
        worker_recipe = destination / "build-worker.sh"
        worker_recipe.write_text(WORKER_RECIPE)
        command = sandbox_command(source, work, worker_recipe, common_mounts,
                                  address_space_bytes=4 * GIB)
        receipt["stages"].append({"name": "packager-worker",
            "recipe": fingerprint(worker_recipe),
            "supervision": run_stage("packager-worker", command, work,
                                      destination / "packager-worker")})
        worker = work / "target/release/tobkiri-python313-packager"
        worker_identity = fingerprint(worker, package.MAX_FILE)
        recipe = destination / "package-assets.sh"
        recipe.write_text(package_recipe(manifest_identity["sha256"],
                                          worker_identity["sha256"]))
        command = sandbox_command(source, work, recipe, (
            (helper, "/helper"), (manifest_path, "/manifest.json"),
            (worker, "/worker"), *input_mounts))
        receipt["stages"].append({"name": "package-assets", "recipe": fingerprint(recipe),
            "supervision": run_stage("package-assets", command, work,
                                      destination / "package-assets")})
        outputs = verify_outputs(work / "assets", manifest_identity["sha256"],
                                  worker_identity["sha256"])
        runtime_inputs.unchanged(source, inputs, snapshots)
        if (fingerprint(runtime_receipt_path) != runtime_identity
                or fingerprint(manifest_path) != manifest_identity
                or fingerprint(worker) != worker_identity
                or fingerprint(config) != config_identity):
            raise ValueError("packager configuration or predecessor receipt changed")
        for path, identity in identities.items():
            if fingerprint(Path(path), package.MAX_FILE) != identity:
                raise ValueError("selected asset input changed during packaging")
        receipt.update(outcome="success", worker=worker_identity, outputs=outputs,
                       inputs_unchanged=True, wasmparser_before_after_validation=True,
                       package_receipt=fingerprint(work / "assets/package-receipt.json"))
    except BaseException:
        receipt["outcome"] = "failed"
        raise
    finally:
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("source", "registry", "rust", "git", "wasi", "runtimes", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    run(**vars(parser.parse_args()))
