//! Admission-only proof for an immutable, independently staged ledger Pack.
//! No DATA, Session, Vault, SQL statement, provider or guest export is executed.
use super::validate_abi;
use anyhow::{Context, Result, bail};
use std::{collections::BTreeMap, path::PathBuf};
use tobkiri_manifest::{
    RuntimeSpec, build_tpk, inspect_pack_wit, load_shapes, parse_manifest, verify_tpk,
};
use wasmtime::component::{Component, Linker, types::ComponentItem};

/// Validate the actual built component against its verified WIT and the same
/// production ABI/import validators used before PackHost links callbacks.
/// This does not claim production callback instantiation: that existing linker
/// requires Shared with actual DATA/Audit/Store custody. An empty-linker check
/// below is a negative proof only; it supplies no substitute Core callbacks.
#[tokio::test]
#[ignore = "requires a real immutable DeepThink ledger stage"]
async fn real_ledger_matches_verified_wit_and_storage_1_1_abi() -> Result<()> {
    let directory = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_DEEPTHINK_LEDGER_STAGE")
            .context("set TOBKIRI_TEST_DEEPTHINK_LEDGER_STAGE to the immutable real Pack stage")?,
    );
    let manifest = parse_manifest(&std::fs::read(directory.join("pack.json"))?)?;
    assert_eq!(manifest.id.as_str(), "local.deepthink");
    let mut files = BTreeMap::new();
    for entry in &manifest.files {
        files.insert(
            entry.path.clone(),
            std::fs::read(directory.join(&entry.path))?,
        );
    }
    let pack = verify_tpk(&build_tpk(&manifest, &files, None)?, &BTreeMap::new())?;
    let shapes = load_shapes(&pack.manifest, &pack.files)?;
    let views = shapes
        .iter()
        .map(|shape| (shape.reference.clone(), shape.clone()))
        .collect();
    let inspection = inspect_pack_wit(&pack, &views)?;
    let RuntimeSpec::Wasm { component, .. } = &pack.manifest.runtime else {
        bail!("the ledger Pack must contain a real Wasm component")
    };
    let engine = crate::WasmEngine::new()?;
    let component_bytes = &pack.files[component];
    let component = Component::new(engine.engine(), component_bytes)?;
    let imports =
        crate::validate_imports(engine.engine(), &component, inspection.allowed_imports())?;
    assert!(
        imports
            .iter()
            .any(|name| name == "tobkiri:core/storage@1.1.0")
    );
    assert!(
        !imports
            .iter()
            .any(|name| name == "tobkiri:core/storage@1.0.0")
    );
    // Production validation includes nominal resource/error identities in both
    // Core package versions, not just function names or a component file header.
    validate_abi(engine.engine(), &component, &pack, &shapes)?;
    let component_type = component.component_type();
    for required_interface in [
        "local:deepthink/workflow@1.0.0",
        "local:deepthink/continuation-input@1.0.0",
    ] {
        let exported = component_type
            .exports(engine.engine())
            .find(|(name, _)| *name == required_interface)
            .with_context(|| format!("missing actual ledger export {required_interface}"))?
            .1;
        let ComponentItem::ComponentInstance(instance) = exported.ty else {
            bail!("ledger export must be a typed interface")
        };
        for required_operation in ["status", "reconcile-generation", "regenerate-phase"] {
            assert!(
                instance.exports(engine.engine()).any(|(name, item)| {
                    name == required_operation && matches!(item.ty, ComponentItem::ComponentFunc(_))
                }),
                "missing actual {required_interface}/{required_operation}"
            );
        }
    }
    assert!(crate::validate_imports(engine.engine(), &component, &Default::default()).is_err());
    let mut missing_storage = inspection.allowed_imports().clone();
    assert!(missing_storage.remove("tobkiri:core/storage@1.1.0"));
    assert!(crate::validate_imports(engine.engine(), &component, &missing_storage).is_err());
    let empty: Linker<()> = Linker::new(engine.engine());
    assert!(
        empty.instantiate_pre(&component).is_err(),
        "an unbound component must not pass empty-linker admission"
    );
    println!(
        "verified ledger ABI {} {} component={}",
        pack.manifest.id,
        pack.hash,
        tobkiri_canon::Digest::hash(component_bytes)
    );
    Ok(())
}
