//! Admission regression for the independently built read-only reader Pack.
use super::validate_abi;
use anyhow::{Context, Result, bail};
use std::collections::BTreeMap;
use tobkiri_manifest::{
    RuntimeSpec, build_tpk, inspect_pack_wit, load_shapes, parse_manifest, verify_tpk,
};
use wasmtime::component::Component;

/// Archive verification alone does not prove WIT resolution or component ABI.
/// This test never opens DATA, starts a Session, reads Vault or executes a GET.
#[tokio::test]
#[ignore = "requires a real independently built and staged generation reader"]
async fn real_reader_matches_verified_wit_and_closed_core_abi() -> Result<()> {
    let directory = std::path::PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_GENERATION_READER_STAGE")
            .context("set TOBKIRI_TEST_GENERATION_READER_STAGE to the real stage")?,
    );
    let manifest = parse_manifest(&std::fs::read(directory.join("pack.json"))?)?;
    assert_eq!(manifest.id.as_str(), "local.openrouter-generation-reader");
    let mut files = BTreeMap::new();
    for entry in &manifest.files {
        files.insert(
            entry.path.clone(),
            std::fs::read(directory.join(&entry.path))?,
        );
    }
    let pack = verify_tpk(&build_tpk(&manifest, &files, None)?, &BTreeMap::new())?;
    let shapes = load_shapes(&pack.manifest, &pack.files)?;
    assert_eq!(shapes.len(), 1);
    let views = shapes
        .iter()
        .map(|shape| (shape.reference.clone(), shape.clone()))
        .collect();
    let inspection = inspect_pack_wit(&pack, &views)?;
    let RuntimeSpec::Wasm { component, .. } = &pack.manifest.runtime else {
        bail!("the reader must be a real Wasm component")
    };
    let engine = crate::WasmEngine::new()?;
    let component = Component::new(engine.engine(), &pack.files[component])?;
    crate::validate_imports(engine.engine(), &component, inspection.allowed_imports())?;
    validate_abi(engine.engine(), &component, &pack, &shapes)?;
    assert!(crate::validate_imports(engine.engine(), &component, &Default::default()).is_err());
    println!(
        "verified reader component ABI {} {}",
        pack.manifest.id, pack.hash
    );
    Ok(())
}
