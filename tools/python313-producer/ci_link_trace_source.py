"""Derive a CI-only observation of actual producer Linker inputs.

The recorder never selects, rereads or substitutes a library. It observes the
same borrowed values passed to the original Linker and saves its encoded
pre-snapshot component. Final component/provider proof is a separate step.
"""

import hashlib
from pathlib import Path
import tomllib

from build_native import fingerprint
from fetch_sdk import inventory
import ui_clock_source
import ui_monotonic_source

FEATURE = "ci-link-input-trace"
ORIGINAL_LINK_SHA256 = (
    "a663aacbd3b47eb8b674f2a4119e6a1ee089271085a5c89e4ce79620b7999015"
)
ENTRY = b"pub fn link_libraries(libraries: &[Library]) -> Result<Vec<u8>> {\n"
END = b"    linker.encode().map_err(|e| anyhow::anyhow!(e))\n"
RECORDER = b"""
// CI-only observation. The private /work parent is supplied by the existing
// supervised namespace. The feature is disabled in ordinary producer builds.
#[cfg(feature = "ci-link-input-trace")]
struct CiLinkInputTrace {
    root: std::path::PathBuf,
    count: usize,
    bytes: usize,
}

#[cfg(feature = "ci-link-input-trace")]
fn ci_write_new(path: &std::path::Path, bytes: &[u8]) -> Result<()> {
    use std::io::Write as _;
    let mut output = std::fs::OpenOptions::new()
        .write(true).create_new(true).open(path)?;
    output.write_all(bytes)?;
    output.sync_all()?;
    Ok(())
}

#[cfg(feature = "ci-link-input-trace")]
fn ci_record_link_inputs(libraries: &[Library]) -> Result<CiLinkInputTrace> {
    use std::io::Write as _;
    anyhow::ensure!(!libraries.is_empty() && libraries.len() <= 128,
                    "CI link input count outside bound");
    anyhow::ensure!(std::fs::canonicalize("/work")? == std::path::Path::new("/work"),
                    "CI private work path is not physical");
    let root = std::path::PathBuf::from("/work/link-trace");
    // Existing paths are never reused or removed. Names from libraries are
    // encoded only as manifest data, never used as filesystem paths.
    std::fs::create_dir(&root)?;
    let mut manifest = Vec::new();
    writeln!(&mut manifest, "tobkiri-ci-link-inputs/1")?;
    let mut bytes = 0usize;
    for (ordinal, Library { name, module, dl_openable }) in libraries.iter().enumerate() {
        anyhow::ensure!(!name.is_empty() && name.len() <= 4096,
                        "CI library name outside bound");
        anyhow::ensure!(!module.is_empty() && module.len() <= 32 * 1024 * 1024,
                        "CI library bytes outside Pack bound");
        bytes = bytes.checked_add(module.len())
            .ok_or_else(|| anyhow::anyhow!("CI trace size overflow"))?;
        anyhow::ensure!(bytes <= 64 * 1024 * 1024, "CI trace aggregate exceeded");
        let name_hex = name.as_bytes().iter()
            .map(|byte| format!("{byte:02x}")).collect::<String>();
        writeln!(&mut manifest, "{ordinal}\\t{name_hex}\\t{dl_openable}\\t{}", module.len())?;
        ci_write_new(&root.join(format!("{ordinal:04}.wasm")), module)?;
    }
    bytes = bytes.checked_add(manifest.len())
        .ok_or_else(|| anyhow::anyhow!("CI trace manifest overflow"))?;
    anyhow::ensure!(bytes <= 64 * 1024 * 1024, "CI trace manifest exceeded aggregate");
    ci_write_new(&root.join("libraries.tsv"), &manifest)?;
    Ok(CiLinkInputTrace { root, count: libraries.len(), bytes })
}

#[cfg(feature = "ci-link-input-trace")]
impl CiLinkInputTrace {
    fn finish(self, component: &[u8]) -> Result<()> {
        anyhow::ensure!(!component.is_empty() && component.len() <= 32 * 1024 * 1024,
                        "CI linked component outside Pack bound");
        let complete = format!("linked-before-snapshot\\t{}\\t{}\\n", self.count, component.len());
        let total = self.bytes.checked_add(component.len())
            .and_then(|n| n.checked_add(complete.len()))
            .ok_or_else(|| anyhow::anyhow!("CI trace completion overflow"))?;
        anyhow::ensure!(total <= 64 * 1024 * 1024, "CI completed trace exceeded aggregate");
        ci_write_new(&self.root.join("preinit-linked-component.wasm"), component)?;
        ci_write_new(&self.root.join("complete.tsv"), complete.as_bytes())?;
        Ok(())
    }
}
"""


def patch_link_source(original: bytes) -> bytes:
    """Insert observation around the original unchanged library-selection loop."""
    if hashlib.sha256(original).hexdigest() != ORIGINAL_LINK_SHA256:
        raise ValueError("exact pinned original Linker wrapper required")
    if original.count(ENTRY) != 1 or original.count(END) != 1:
        raise ValueError("original Linker entry or completion is not unique")
    return (
        original.replace(
            ENTRY,
            ENTRY + b'    #[cfg(feature = "ci-link-input-trace")]\n'
            b"    let ci_trace = ci_record_link_inputs(libraries)?;\n",
            1,
        ).replace(
            END,
            b"    let component = linker.encode().map_err(|e| anyhow::anyhow!(e))?;\n"
            b'    #[cfg(feature = "ci-link-input-trace")]\n'
            b"    ci_trace.finish(&component)?;\n    Ok(component)\n",
            1,
        )
        + RECORDER
    )


def feature_manifest(original: bytes) -> bytes:
    """Add only an explicit observation feature; leave dependencies/lock untouched."""
    document = tomllib.loads(original.decode())
    features = document.get("features", {})
    allowed = (
        {},
        {ui_clock_source.FEATURE: []},
        {
            ui_clock_source.FEATURE: [],
            ui_monotonic_source.FEATURE: [ui_clock_source.FEATURE],
        },
    )
    if (
        document.get("package", {}).get("name") != "componentize-py"
        or features not in allowed
    ):
        raise ValueError("original or authenticated clock feature manifest required")
    suffix = b"" if "features" in document else b"\n[features]\n"
    updated = original + suffix + b"ci-link-input-trace = []\n"
    if tomllib.loads(updated.decode()) != {
        **document,
        "features": {**features, FEATURE: []},
    }:
        raise ValueError("trace feature changed unrelated manifest values")
    return updated


def build_recipe(original: bytes) -> bytes:
    """Append the observer feature to exactly one recorded producer build line."""
    base = ui_clock_source.BUILD.rstrip(b"\n")
    candidates = [
        base + b"\n",
        base + b" --features ui-wall-clock\n",
        base + b" --features ui-monotonic-clock\n",
    ]
    matches = [line for line in candidates if original.count(line) == 1]
    if len(matches) != 1 or b"ci-link-input-trace" in original:
        raise ValueError("one original or clock producer build command required")
    line = matches[0]
    feature = (
        b",ci-link-input-trace"
        if b" --features " in line
        else b" --features ci-link-input-trace"
    )
    return original.replace(line, line.rstrip(b"\n") + feature + b"\n", 1)


def enable(stage: Path, receipt: dict) -> dict:
    """Derive only from a fully inventoried separate publishing source tree."""
    if not stage.is_absolute() or stage.resolve() != stage or not stage.is_dir():
        raise ValueError("physical publishing source required")
    before = inventory(stage)
    if (
        not stage.is_absolute()
        or stage.resolve() != stage
        or before != receipt["files"]
        or any("link" in item for item in before.values())
        or "ci_link_trace" in receipt
    ):
        raise ValueError("authenticated physical publishing source required")
    changed = {
        "Cargo.toml": feature_manifest((stage / "Cargo.toml").read_bytes()),
        "src/link.rs": patch_link_source((stage / "src/link.rs").read_bytes()),
    }
    expected = dict(before)
    for name, raw in changed.items():
        (stage / name).write_bytes(raw)
        expected[name] = {**before[name], **fingerprint(stage / name)}
    after = inventory(stage)
    if after != expected:
        raise ValueError("link observer changed unrelated source or producer assets")
    return {
        **receipt,
        "files": after,
        "delta": receipt["delta"]
        + "; explicit CI-only actual Linker input observation",
        "ci_link_trace": {
            "feature": FEATURE,
            "upstream_link_sha256": ORIGINAL_LINK_SHA256,
            "path": "/work/link-trace",
            "maximum_members": 128,
            "maximum_file_bytes": 32 * 1024 * 1024,
            "maximum_total_bytes": 64 * 1024 * 1024,
            "stage": "actual selected libraries and linked component before snapshot",
            "final_component_binding_verified": False,
            "final_provider_resolution_verified": False,
            "production_enabled": False,
            "changed_files": {
                name: {"before": before[name], "after": after[name]} for name in changed
            },
        },
    }
