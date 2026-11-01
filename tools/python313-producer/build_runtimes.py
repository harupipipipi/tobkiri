"""Prime Cargo from local verified inputs and replay the original Wasm runtimes."""
import argparse
import json
import os
from pathlib import Path
import sys

from build_native import GIB, HERE, fingerprint, sandbox_command
from build_wasi import run_stage
import fetch_sdk
import runtime_inputs
from verify_git_checkout import verify_cache

RECIPE_SHA256 = "5c520870b55b5b48d2e55359dc6435cfbe176066474159f53485354991933b1f"
CARGO_CONFIG = '''[source.crates-io]
replace-with = "verified-registry"
[source.verified-registry]
directory = "/registry/sources"
[net]
git-fetch-with-cli = true
'''
GIT_CONFIG = '''[credential]
helper =
[core]
hooksPath = /dev/null
fsmonitor = false
[init]
templateDir = /empty-template
[url "file:///git-input/wasm-tools.git"]
insteadOf = https://github.com/dicej/wasm-tools
[url "file:///git-input/testsuite.git"]
insteadOf = https://github.com/WebAssembly/testsuite
'''
CACHE_RECIPE = '''set -eu
export PATH=/rust/bin:/usr/bin:/bin
export RUSTC=/rust/bin/rustc
export HOME=/work/home CARGO_HOME=/work/runtime-cargo-home
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/work/gitconfig
export GIT_TERMINAL_PROMPT=0 GIT_ALLOW_PROTOCOL=file
export GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false
export CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0
cd /source/componentize-py
/rust/bin/rustc --version --verbose
/rust/bin/cargo --version
/rust/bin/cargo fetch --locked
set -- /work/runtime-cargo-home/git/checkouts/*/*
test "$#" -eq 1
test -d "$1/.git"
/usr/bin/git -C "$1" submodule update --init -- tests/testsuite
/rust/bin/cargo fetch --frozen
'''


def mounts(source: Path, registry: Path, rust: Path, git: Path, wasi: Path,
           work: Path, *, frozen: bool) -> tuple:
    """Expose exact source/tool inputs; freeze the completed Git cache for builds."""
    paths = [
        (source / "componentize-py", "/source/componentize-py"),
        (registry / "sources", "/registry/sources"),
        (rust / "toolchain", "/rust"),
        (git / "work/wasm-tools.git", "/git-input/wasm-tools.git"),
        (git / "work/testsuite.git", "/git-input/testsuite.git"),
        (wasi / "sdk-input/sdk", "/sdk"),
        (wasi / "work/wasi", "/work/wasi"),
        (work / "runtime-cargo-home/config.toml", "/work/runtime-cargo-home/config.toml"),
        (work / "gitconfig", "/work/gitconfig"),
        (work / "empty-template", "/empty-template"),
        (work / "package", "/work/package"),
    ]
    if frozen:
        paths.append((work / "runtime-cargo-home/git", "/work/runtime-cargo-home/git"))
    return tuple(paths)


def run(source: Path, registry: Path, rust: Path, git: Path, wasi: Path,
        destination: Path) -> None:
    """Execute both stages only after complete input checks, never on the desktop."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("runtime rebuilding is restricted to Linux CI")
    if (not destination.is_absolute() or not destination.parent.is_dir()
            or destination.parent.resolve() != destination.parent):
        raise ValueError("new absolute destination under physical parent required")
    destination.mkdir(mode=0o700)
    inputs, snapshots = runtime_inputs.verify(source, registry, rust, git, wasi)
    recipe = HERE / "trials/2026-10-19/wasi-runtimes.sh"
    if fingerprint(recipe)["sha256"] != RECIPE_SHA256:
        raise ValueError("original runtime recipe changed")
    work = destination / "work"
    work.mkdir(mode=0o700)
    for name in ("home", "runtime-cargo-home", "empty-template", "package"):
        (work / name).mkdir(mode=0o700)
    (work / "runtime-cargo-home/config.toml").write_text(CARGO_CONFIG)
    (work / "gitconfig").write_text(GIT_CONFIG)
    pyo3_path = source / "componentize-py/pyo3-config.txt"
    fingerprint(pyo3_path, 16384)
    pyo3 = pyo3_path.read_bytes()
    if not pyo3.endswith(b"\n") or any(line.startswith(b"lib_dir=") for line in pyo3.splitlines()):
        raise ValueError("unexpected original PyO3 configuration")
    (work / "package/pyo3-config.txt").write_bytes(pyo3 + b"lib_dir=/work/wasi\n")
    cache_recipe = destination / "prime-cache.sh"
    cache_recipe.write_text(CACHE_RECIPE)
    receipt = {"format": "tobkiri.python313-runtimes-rebuild/1", "outcome": "pending",
               "inputs": inputs, "original_recipe_sha256": RECIPE_SHA256,
               "cache_recipe": fingerprint(cache_recipe), "stages": [],
               "wasmtime_validation": False, "guest_execution": False,
               "componentization": False, "producer_cli": False,
               "production_enabled": False, "historical_binary_identity_claimed": False}
    receipt_path = destination / "runtimes-build.json"
    configs = {str(path): fingerprint(path) for path in (
        work / "runtime-cargo-home/config.toml", work / "gitconfig",
        work / "package/pyo3-config.txt")}
    try:
        for name, script, frozen in (("offline-cache", cache_recipe, False),
                                     ("wasm-runtimes", recipe, True)):
            command = sandbox_command(source, work, script,
                mounts(source, registry, rust, git, wasi, work, frozen=frozen),
                address_space_bytes=4 * GIB)
            supervision = run_stage(name, command, work, destination / name)
            checkout = verify_cache(work / "runtime-cargo-home", git / "work")
            for path, identity in configs.items():
                if fingerprint(Path(path)) != identity:
                    raise ValueError("read-only build configuration changed")
            if frozen:
                if fetch_sdk.inventory(work / "runtime-cargo-home/git") != cache_inputs:
                    raise ValueError("frozen Cargo Git cache changed during build")
            else:
                cache_inputs = fetch_sdk.inventory(work / "runtime-cargo-home/git")
                receipt["git_cache_inventory"] = cache_inputs
            receipt["stages"].append({"name": name, "supervision": supervision,
                                      "verified_git_checkout": checkout})
            receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        outputs = {}
        for variant in ("sync", "async"):
            path = work / f"runtime-{variant}/wasm32-wasip2/release/componentize_py_runtime.wasm"
            with path.open("rb") as stream:
                if stream.read(8) != b"\x00asm\x01\x00\x00\x00":
                    raise ValueError("runtime output is not core-Wasm encoding")
            outputs[variant] = fingerprint(path, 32 * 1024 * 1024)
        if outputs["sync"] == outputs["async"]:
            raise ValueError("sync and async runtime artifacts unexpectedly identical")
        runtime_inputs.unchanged(source, inputs, snapshots)
        receipt.update(outcome="success", outputs=outputs, inputs_unchanged=True)
    except BaseException:
        receipt["outcome"] = "failed"
        raise
    finally:
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("source", "registry", "rust", "git", "wasi", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    run(**vars(parser.parse_args()))
