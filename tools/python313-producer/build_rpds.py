"""Build a genuine rpds abi3 candidate without changing the saved producer."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from build_native import GIB, HERE, fingerprint, sandbox_command
from build_wasi import run_stage
import archive_inputs
import fetch_rpds
import fetch_sdk
import fetch_toolchain
import fetch_validation_tools
import runtime_inputs

PYO3_CONFIG = """implementation=CPython
version=3.13
shared=true
abi3=true
pointer_width=32
build_flags=
suppress_build_script_link_lines=true
"""
CARGO_CONFIG = """[source.crates-io]
replace-with = "verified-rpds-registry"
[source.verified-rpds-registry]
directory = "/vendor"
[net]
offline = true
"""
RECIPE = """set -eu
export PATH=/rust/bin:/usr/bin:/bin
export RUSTC=/rust/bin/rustc
export CARGO_HOME=/work/cargo-home CARGO_TARGET_DIR=/work/target
export CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0 CARGO_NET_OFFLINE=true
export CARGO_PROFILE_RELEASE_DEBUG=0
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=/usr/bin/gcc
export CARGO_TARGET_WASM32_WASIP2_LINKER=/sdk/bin/clang
export PYO3_CONFIG_FILE=/build-config/pyo3-config.txt
export RUSTFLAGS='--cfg pyo3_disable_reference_pool -Clink-args=-Wl,--skip-wit-component -Clink-args=-shared -Clink-args=-Wl,--unresolved-symbols=import-dynamic -Clink-self-contained=n'
cd /rpds
/rust/bin/rustc --version --verbose
/rust/bin/cargo --version
/rust/bin/cargo --config /build-config/cargo.toml build --frozen -j1 --release --target wasm32-wasip2 --features pyo3/abi3-py313 -vv
"""


def physical(path: Path) -> None:
    """Refuse input root aliases before any compiler receives them."""
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise ValueError("physical absolute input directory required")


def verify_sources(source: Path, inputs: dict) -> None:
    """Require both the receipt and expanded sources to match pinned archives."""
    if inputs["commit"] != fetch_rpds.COMMIT or inputs["version"] != fetch_rpds.VERSION:
        raise ValueError("rpds source selection differs")
    source_archive = source / "archives/rpds.tar.gz"
    source_files = fetch_rpds.source_inventory(source_archive)
    identity = fingerprint(source_archive, fetch_rpds.MAX_SOURCE_ARCHIVE)
    if inputs["archive"] != identity or inputs["source_files"] != source_files:
        raise ValueError("rpds source receipt differs from pinned archive")
    runtime_inputs.recorded_files(source / "source", source_files)
    expected = fetch_rpds.packages((source / "source/Cargo.lock").read_bytes())
    if len(expected) != len(inputs["packages"]):
        raise ValueError("rpds registry closure differs")
    vendor_files, totals = {}, {"bytes": 0, "members": 0}
    downloaded = identity["bytes"]
    for pin, record in zip(expected, inputs["packages"]):
        if any(record.get(key) != value for key, value in pin.items()):
            raise ValueError("rpds registry source differs from lock")
        name = pin["name"] + "-" + pin["version"]
        archive = source / "archives" / (name + ".crate")
        files = fetch_rpds.registry_inventory(archive, pin, totals)
        identity = fingerprint(archive, fetch_rpds.MAX_CRATE_ARCHIVE)
        downloaded += identity["bytes"]
        if downloaded > fetch_rpds.MAX_DOWNLOADS:
            raise ValueError("rpds download closure exceeds its bound")
        checksum = files[".cargo-checksum.json"]
        if (
            record["archive_bytes"] != identity["bytes"]
            or record["files"]
            != {k: v for k, v in files.items() if k != ".cargo-checksum.json"}
            or record["cargo_checksums"]
            != {k: checksum[k] for k in ("bytes", "sha256")}
        ):
            raise ValueError("rpds registry receipt differs from pinned archive")
        vendor_files.update({name + "/" + key: value for key, value in files.items()})
    if inputs["vendor_files"] != vendor_files:
        raise ValueError("rpds vendor receipt differs from pinned archives")
    runtime_inputs.recorded_files(source / "vendor", vendor_files)


def verify_rust(rust: Path, toolchain: dict) -> None:
    """Recreate the merged Rust identity from release-selected pinned archives."""
    if (
        toolchain["version"] != fetch_toolchain.VERSION
        or fingerprint(rust / "release.toml", 1024 * 1024)
        != toolchain["release_manifest"]
    ):
        raise ValueError("Rust toolchain receipt differs")
    pins = fetch_toolchain.selected_inputs((rust / "release.toml").read_bytes())
    if len(pins) != len(toolchain["inputs"]):
        raise ValueError("Rust toolchain input count differs")
    files, downloaded, expanded = {}, 0, 0
    for pin, record in zip(pins, toolchain["inputs"]):
        if any(record.get(key) != value for key, value in pin.items()):
            raise ValueError("Rust target differs")
        archive = rust / (pin["prefix"] + ".tar.xz")
        inspected = archive_inputs.inspect(
            archive,
            pin["prefix"],
            pin["sha256"],
            maximum=fetch_toolchain.MAX_ARCHIVE,
            max_file=fetch_sdk.MAX_FILE,
            max_expanded=fetch_toolchain.MAX_EXPANDED - expanded,
            max_members=fetch_sdk.MAX_MEMBERS,
            allow_links=True,
        )
        if (
            inspected["archive"] != record["archive"]
            or inspected["archive"]["sha256"] != pin["sha256"]
            or record["extracted_files"] != len(inspected["files"])
        ):
            raise ValueError("Rust target archive differs")
        downloaded += inspected["archive"]["bytes"]
        expanded += sum(entry.get("bytes", 0) for entry in inspected["files"].values())
        if (
            downloaded > fetch_toolchain.MAX_DOWNLOADS
            or expanded > fetch_toolchain.MAX_EXPANDED
        ):
            raise ValueError("Rust input closure exceeds its bound")
        archive_inputs.merge_component(files, inspected, pin["component"])
    if toolchain["files"] != files:
        raise ValueError("Rust receipt differs from pinned component archives")
    runtime_inputs.recorded_files(rust / "toolchain", files)


def verify_sdk(sdk: Path, sdk_report: dict) -> None:
    """Compare SDK bytes/modes/links against its original pinned tar expansion."""
    if (
        sdk_report["archive_sha256"] != fetch_sdk.SHA256
        or sdk_report["archive_bytes"] != fetch_sdk.ARCHIVE_BYTES
    ):
        raise ValueError("WASI SDK selection differs")
    inspected = archive_inputs.inspect(
        sdk / "sdk.tar.gz",
        fetch_sdk.PREFIX,
        fetch_sdk.SHA256,
        maximum=fetch_sdk.ARCHIVE_BYTES,
        expected_bytes=fetch_sdk.ARCHIVE_BYTES,
        max_file=fetch_sdk.MAX_FILE,
        max_expanded=fetch_sdk.MAX_EXPANDED,
        max_members=fetch_sdk.MAX_MEMBERS,
        allow_links=True,
    )
    if sdk_report["files"] != inspected["files"]:
        raise ValueError("SDK receipt differs from pinned archive")
    runtime_inputs.recorded_files(sdk / "sdk", inspected["files"])


def verify_validation(validation: Path, tools: dict) -> None:
    """Authenticate WAC directly and derive Wasmtime files from its pinned wheel."""
    if tools["inputs"] != list(fetch_validation_tools.INPUTS):
        raise ValueError("validation tool selection differs")
    for pin in fetch_validation_tools.INPUTS:
        if fingerprint(validation / pin["filename"], pin["bytes"]) != {
            k: pin[k] for k in ("bytes", "sha256")
        }:
            raise ValueError("validation tool archive differs")
    wheel = fetch_validation_tools.INPUTS[1]
    files = archive_inputs.inspect_wheel(
        validation / wheel["filename"],
        wheel["sha256"],
        maximum=wheel["bytes"],
        expected_bytes=wheel["bytes"],
        max_file=fetch_validation_tools.MAX_FILE,
        max_expanded=fetch_validation_tools.MAX_EXPANDED,
        max_members=fetch_validation_tools.MAX_ENTRIES,
    )
    if tools["files"] != files:
        raise ValueError("validation receipt differs from pinned wheel")
    runtime_inputs.recorded_files(validation / "wasmtime", files)


def verify(
    source: Path, rust: Path, sdk: Path, producer: Path, validation: Path
) -> dict:
    """Bind actual inputs to immutable pins before consuming stage receipts."""
    for path in (source, rust, sdk, producer, validation):
        physical(path)
    verify_sources(source, runtime_inputs.report(source / "rpds-inputs.json"))
    verify_rust(rust, runtime_inputs.report(rust / "rust-manifest.json"))
    sdk_manifest = sdk / "sdk-manifest.json"
    fingerprint(sdk_manifest, 16 * 1024 * 1024)
    verify_sdk(sdk, json.loads(sdk_manifest.read_bytes()))
    cli = runtime_inputs.report(producer / "cli-build.json")
    if (
        cli["producer_cli"] is not True
        or cli["inputs_unchanged"] is not True
        or fingerprint(producer / "work/target/release/componentize-py")
        != cli["binary"]
    ):
        raise ValueError("saved producer identity differs")
    source_pin = [
        pin
        for name, pin in cli["predecessors"].items()
        if Path(name).name == "cli-source.json"
    ]
    if (
        len(source_pin) != 1
        or fingerprint(producer / "cli-source.json") != source_pin[0]
    ):
        raise ValueError("saved producer publishing source differs")
    verify_validation(
        validation, runtime_inputs.report(validation / "validation-inputs.json")
    )
    return {
        str(path): fingerprint(path)
        for path in (
            source / "rpds-inputs.json",
            rust / "rust-manifest.json",
            sdk / "sdk-manifest.json",
            producer / "cli-build.json",
            producer / "cli-source.json",
            validation / "validation-inputs.json",
        )
    }


def run(
    source: Path,
    rust: Path,
    sdk: Path,
    producer: Path,
    validation: Path,
    destination: Path,
) -> None:
    """Build/import/run only inside the existing supervised CI namespace."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("rpds building and guest execution are restricted to Linux CI")
    if (
        not destination.is_absolute()
        or destination.parent.resolve() != destination.parent
        or shutil.disk_usage(destination.parent).free < fetch_rpds.DISK_FLOOR
    ):
        raise ValueError("new physical destination and existing disk floor required")
    receipts = verify(source, rust, sdk, producer, validation)
    destination.mkdir(mode=0o700)
    work = destination / "work"
    work.mkdir(mode=0o700)
    for name in ("cargo-home", "home", "tmp", "empty-venv"):
        (work / name).mkdir(mode=0o700)
    config_root = destination / "build-config"
    config_root.mkdir(mode=0o700)
    config = config_root / "cargo.toml"
    config.write_text(CARGO_CONFIG)
    pyo3 = config_root / "pyo3-config.txt"
    pyo3.write_text(PYO3_CONFIG)
    recipe = destination / "build.sh"
    recipe.write_text(RECIPE)
    config.chmod(0o400)
    pyo3.chmod(0o400)
    config_root.chmod(0o500)
    probe_source = HERE / "rpds_probe"
    scripts = fetch_sdk.inventory(probe_source)
    composition = HERE.parents[1] / "sdk/python/composition.py"
    private = {
        str(path): fingerprint(path) for path in (config, pyo3, recipe, composition)
    }
    report = {
        "format": "tobkiri.ui-rpds-candidate/1",
        "outcome": "pending",
        "inputs": receipts,
        "configuration": private,
        "probe_source": scripts,
        "target": "wasm32-wasip2",
        "feature": "pyo3/abi3-py313",
        "stages": [],
        "guest_executed": False,
        "native_capture_binding": False,
        "full_setup_gui": False,
        "production_enabled": False,
    }
    output = destination / "rpds-build.json"
    try:
        command = sandbox_command(
            None,
            work,
            recipe,
            (
                (source / "source", "/rpds"),
                (source / "vendor", "/vendor"),
                (rust / "toolchain", "/rust"),
                (sdk / "sdk", "/sdk"),
                (config_root, "/build-config"),
            ),
            address_space_bytes=4 * GIB,
        )
        report["stages"].append(
            run_stage("rpds-abi3-build", command, work, destination / "build")
        )
        replay = destination / "probe.sh"
        replay.write_text("set -eu\n/usr/bin/python3 -I -B /probe-source/driver.py\n")
        command = sandbox_command(
            None,
            work,
            replay,
            (
                (probe_source, "/probe-source"),
                (validation / "wasmtime", "/validation-deps"),
                (validation / "wac-cli", "/wac"),
                (composition, "/composition.py"),
                (producer / "work/target/release/componentize-py", "/producer"),
                (work / "empty-venv", "/empty-venv"),
            ),
            address_space_bytes=8 * GIB,
        )
        report["stages"].append(
            run_stage("rpds-component-import-run", command, work, destination / "probe")
        )
        observation = json.loads((work / "probe.json").read_bytes())
        if (
            observation.get("passed") is not True
            or observation.get("actual_limited_api") is not True
        ):
            raise ValueError("real rpds extension did not pass its declared scope")
        verify(source, rust, sdk, producer, validation)
        for name, identity in private.items():
            if fingerprint(Path(name)) != identity:
                raise ValueError("readonly build configuration changed")
        if fetch_sdk.inventory(probe_source) != scripts:
            raise ValueError("probe source changed")
        report.update(
            outcome="success",
            guest_executed=True,
            extension=observation["extension_sha256"],
            observation=fingerprint(work / "probe.json"),
        )
    except BaseException:
        report["outcome"] = "failed"
        raise
    finally:
        output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("source", "rust", "sdk", "producer", "validation", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    run(**vars(parser.parse_args()))
