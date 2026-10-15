use anyhow::{Result, bail};
use std::collections::BTreeSet;
use wasmtime::Engine;
use wasmtime::component::{Component, types::ComponentItem};

/// Restricted Preview 2 interfaces. Socket and HTTP interfaces are never linked.
pub const WASI_SUBSET: &[&str] = &[
    "wasi:clocks/wall-clock",
    "wasi:clocks/monotonic-clock",
    "wasi:random/random",
    "wasi:random/insecure",
    "wasi:random/insecure-seed",
    "wasi:io/error",
    "wasi:io/poll",
    "wasi:io/streams",
    "wasi:cli/environment",
    "wasi:cli/exit",
    "wasi:cli/stdin",
    "wasi:cli/stdout",
    "wasi:cli/stderr",
    "wasi:cli/terminal-input",
    "wasi:cli/terminal-output",
    "wasi:cli/terminal-stdin",
    "wasi:cli/terminal-stdout",
    "wasi:cli/terminal-stderr",
    "wasi:filesystem/types",
    "wasi:filesystem/preopens",
];

fn permitted_wasi(name: &str) -> bool {
    let Some((interface, version)) = name.rsplit_once('@') else {
        return false;
    };
    // Preview 3 streams/futures and unknown WASI versions are outside v1.
    version.starts_with("0.2.") && WASI_SUBSET.contains(&interface)
}

/// Check actual component imports before any instantiation or guest code runs.
/// `manifest_allowed` comes from manifest::allowed_imports, never a guest payload.
pub fn validate_imports(
    engine: &Engine,
    component: &Component,
    manifest_allowed: &BTreeSet<String>,
) -> Result<Vec<String>> {
    let mut actual = Vec::new();
    for (name, import) in component.component_type().imports(engine) {
        if !matches!(import.ty, ComponentItem::ComponentInstance(_)) {
            bail!("component imports a non-interface item: {name}");
        }
        let is_wasi = name.starts_with("wasi:");
        if (is_wasi && !permitted_wasi(name)) || (!is_wasi && !manifest_allowed.contains(name)) {
            bail!("component import is not declared or permitted: {name}");
        }
        actual.push(name.to_owned());
    }
    Ok(actual)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ambient_network_interfaces_never_enter_wasi_subset() {
        assert!(!permitted_wasi("wasi:sockets/tcp@0.2.6"));
        assert!(!permitted_wasi("wasi:http/outgoing-handler@0.2.6"));
        assert!(!permitted_wasi("wasi:io/streams@0.3.0"));
        assert!(permitted_wasi("wasi:cli/environment@0.2.6"));
        assert!(permitted_wasi("wasi:filesystem/types@0.2.6"));
    }
}
