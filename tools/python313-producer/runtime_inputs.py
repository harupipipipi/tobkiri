"""Check the actual preceding CI inputs before invoking the restored toolchain."""
import json
from pathlib import Path

from build_native import fingerprint, verify_tree
import fetch_git
import fetch_registry
import fetch_sdk
import fetch_toolchain


def report(path: Path) -> dict:
    """Read bounded stage evidence, requiring its successful terminal outcome."""
    fingerprint(path, 16 * 1024 * 1024)
    result = json.loads(path.read_bytes())
    if result.get("outcome") != "success":
        raise ValueError(f"preceding input stage did not succeed: {path.name}")
    return result


def recorded_files(root: Path, expected: dict) -> dict:
    """Verify complete inventories, including missing/unlisted files and links."""
    if not root.is_dir() or root.resolve() != root:
        raise ValueError("physical recorded input directory required")
    actual = fetch_sdk.inventory(root)
    if actual != expected:
        raise ValueError(f"recorded input inventory changed: {root.name}")
    return actual


def verify(source: Path, registry: Path, rust: Path, git: Path, wasi: Path) -> tuple:
    """Verify actual stage output bytes and retain all read-only input identities."""
    for path in (source, registry, rust, git, wasi):
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("physical absolute input roots required")
    source_identity = verify_tree(source)
    registry_report = report(registry / "registry-manifest.json")
    packages = fetch_registry.registry_packages((source / "componentize-py/Cargo.lock").read_bytes())
    if len(packages) != len(registry_report["packages"]):
        raise ValueError("registry package inventory changed")
    registry_expected = {}
    for pin, recorded in zip(packages, registry_report["packages"]):
        if any(pin[k] != recorded.get(k) for k in pin):
            raise ValueError("registry source differs from original lock")
        stem = pin["name"] + "-" + pin["version"]
        identity = fingerprint(registry / "archives" / (stem + ".crate"),
                               fetch_registry.MAX_ARCHIVE)
        if identity != {"bytes": recorded["archive_bytes"], "sha256": pin["sha256"]}:
            raise ValueError("registry archive identity changed")
        for name, entry in recorded["files"].items():
            registry_expected[stem + "/" + name] = entry
        checksum = registry / "sources" / stem / ".cargo-checksum.json"
        if fingerprint(checksum) != recorded["cargo_checksums"]:
            raise ValueError("Cargo directory-source checksum file changed")
        data = json.loads(checksum.read_bytes())
        if data != {"package": pin["sha256"], "files": {
                name: entry["sha256"] for name, entry in recorded["files"].items()}}:
            raise ValueError("Cargo directory-source derivation changed")
        registry_expected[stem + "/.cargo-checksum.json"] = {
            **recorded["cargo_checksums"], "mode": oct(checksum.stat().st_mode & 0o777)}
    snapshots = {str(registry / "sources"): recorded_files(registry / "sources", registry_expected)}
    rust_report = report(rust / "rust-manifest.json")
    if rust_report["release_manifest"] != fingerprint(rust / "release.toml"):
        raise ValueError("Rust release evidence changed")
    selected = fetch_toolchain.selected_inputs((rust / "release.toml").read_bytes())
    if len(selected) != len(rust_report["inputs"]):
        raise ValueError("Rust archive inventory changed")
    for pin, recorded in zip(selected, rust_report["inputs"]):
        if any(pin[k] != recorded.get(k) for k in pin):
            raise ValueError("Rust archive selection changed")
        archive_identity = fingerprint(rust / (pin["prefix"] + ".tar.xz"),
                                       fetch_toolchain.MAX_ARCHIVE)
        if (archive_identity != recorded["archive"]
                or archive_identity["sha256"] != pin["sha256"]):
            raise ValueError("Rust archive identity changed")
    snapshots[str(rust / "toolchain")] = recorded_files(rust / "toolchain", rust_report["files"])
    git_report = report(git / "git-manifest.json")
    if len(git_report["inputs"]) != len(fetch_git.INPUTS):
        raise ValueError("Git input inventory changed")
    for pin, recorded in zip(fetch_git.INPUTS, git_report["inputs"]):
        if any(pin[k] != recorded.get(k) for k in pin):
            raise ValueError("Git input differs from recorded commit/tree")
        path = git / "work" / (pin["name"] + ".git")
        snapshots[str(path)] = recorded_files(path, recorded["git_files"])
        if (path / "shallow").exists():
            raise ValueError("shallow Git inputs cannot prime ordinary Cargo fetch")
    wasi_report = report(wasi / "wasi-build.json")
    if wasi_report["source"] != source_identity or not wasi_report["inputs_unchanged"]:
        raise ValueError("WASI libraries do not match the original source")
    for stage in wasi_report["stages"]:
        for name, identity in stage["outputs"].items():
            if fingerprint(wasi / "work" / name) != identity:
                raise ValueError("recorded WASI output changed")
    snapshots[str(wasi / "work/wasi")] = fetch_sdk.inventory(wasi / "work/wasi")
    sdk_manifest = wasi / "sdk-input/sdk-manifest.json"
    if fingerprint(sdk_manifest) != wasi_report["sdk_manifest"]:
        raise ValueError("SDK receipt identity changed")
    sdk_report = json.loads(sdk_manifest.read_bytes())
    if sdk_report["archive_sha256"] != fetch_sdk.SHA256:
        raise ValueError("SDK release identity changed")
    snapshots[str(wasi / "sdk-input/sdk")] = recorded_files(wasi / "sdk-input/sdk", sdk_report["files"])
    receipts = {"source": source_identity, "stage_receipts": {
        str(path): fingerprint(path) for path in (
            registry / "registry-manifest.json", rust / "rust-manifest.json",
            git / "git-manifest.json", wasi / "wasi-build.json")}}
    return receipts, snapshots


def unchanged(source: Path, receipts: dict, snapshots: dict) -> None:
    """Check actual immutable inputs again after cache preparation and builds."""
    if verify_tree(source) != receipts["source"]:
        raise ValueError("original source changed during execution")
    for path, identity in receipts["stage_receipts"].items():
        if fingerprint(Path(path)) != identity:
            raise ValueError("input stage receipt changed during execution")
    for path, expected in snapshots.items():
        recorded_files(Path(path), expected)
