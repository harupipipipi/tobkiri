"""Derive a separate producer candidate retaining only genuine wall-clock IO."""

from pathlib import Path
import tomllib

from build_native import fingerprint
from fetch_sdk import inventory

FEATURE = "ui-wall-clock"
INTERFACE_PREFIX = "wasi:clocks/wall-clock@"
ANCHOR = b"""                                if let TypeRef::Func(ty) = import.ty {
                                    imports.entry(import.module).or_default().insert(
"""
REPLACEMENT = b"""                                if let TypeRef::Func(ty) = import.ty {
                                    if cfg!(feature = "ui-wall-clock")
                                        && import.module.starts_with("wasi:clocks/wall-clock@")
                                    {
                                        let version = semver::Version::parse(
                                            import.module.rsplit_once('@').unwrap().1,
                                        )?;
                                        let signature = &types[usize::try_from(ty).unwrap()];
                                        if version.major != 0 || version.minor != 2
                                            || !version.pre.is_empty() || !version.build.is_empty()
                                            || !matches!(import.name, "now" | "resolution")
                                            || signature.params() != [wasmparser::ValType::I32]
                                            || !signature.results().is_empty()
                                        {
                                            bail!("unsupported UI wall-clock import or ABI");
                                        }
                                        eprintln!("tobkiri-ui-wall-clock {}::{}", import.module, import.name);
                                        continue;
                                    }
                                    imports.entry(import.module).or_default().insert(
"""
BUILD = b"/rust/bin/cargo build --frozen -j1 --release -p componentize-py --bin componentize-py\n"


def patch_stub_source(original: bytes) -> bytes:
    """Keep the original stubs except the exact allowlisted clock signatures."""
    if original.count(ANCHOR) != 1 or b"tobkiri-ui-wall-clock" in original:
        raise ValueError("unexpected original WASI stub generator")
    return original.replace(ANCHOR, REPLACEMENT, 1)


def feature_manifest(original: bytes) -> bytes:
    """Add an explicit build feature without changing dependencies or defaults."""
    document = tomllib.loads(original.decode())
    if document.get("package", {}).get("name") != "componentize-py":
        raise ValueError("original producer package required")
    if "features" in document:
        raise ValueError("unexpected original producer features")
    updated = original + b"\n[features]\nui-wall-clock = []\n"
    if tomllib.loads(updated.decode()) != {**document, "features": {FEATURE: []}}:
        raise ValueError("clock feature changed unrelated manifest fields")
    return updated


def build_recipe(original: bytes) -> bytes:
    """Keep the recorded build recipe and opt into only the new feature."""
    if original.count(BUILD) != 1:
        raise ValueError("unexpected original producer build command")
    return original.replace(
        BUILD, BUILD.rstrip(b"\n") + b" --features ui-wall-clock\n", 1
    )


def enable(stage: Path, receipt: dict) -> dict:
    """Apply the reviewed delta only to the authenticated private CLI source."""
    before = inventory(stage)
    if before != receipt["files"] or any("link" in row for row in before.values()):
        raise ValueError("original publishing source changed before clock derivation")
    changes = {
        "Cargo.toml": feature_manifest((stage / "Cargo.toml").read_bytes()),
        "src/stubwasi.rs": patch_stub_source((stage / "src/stubwasi.rs").read_bytes()),
    }
    expected = dict(before)
    for name, raw in changes.items():
        (stage / name).write_bytes(raw)
        expected[name] = {**before[name], **fingerprint(stage / name)}
    after = inventory(stage)
    if after != expected:
        raise ValueError("UI clock producer changed unrelated source or asset bytes")
    return {
        **receipt,
        "files": after,
        "delta": receipt["delta"]
        + "; explicit ui-wall-clock feature and exact wall-clock stub exception",
        "ui_wall_clock": {
            "feature": FEATURE,
            "interface_prefix": INTERFACE_PREFIX,
            "functions": ["now", "resolution"],
            "stub_wasi_retained": True,
            "changed_files": {
                name: {"original": before[name], "candidate": after[name]}
                for name in changes
            },
        },
    }
