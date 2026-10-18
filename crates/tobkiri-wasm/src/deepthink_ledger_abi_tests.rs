//! Admission-only proof for an immutable, independently staged ledger Pack.
//! No DATA, Session, Vault, SQL statement, provider or guest export is executed.
use super::validate_abi;
use anyhow::{Context, Result, bail};
use std::{collections::BTreeMap, path::PathBuf};
use tobkiri_manifest::{
    RuntimeSpec, VerifiedPack, build_tpk, inspect_pack_wit, load_shapes, parse_manifest, verify_tpk,
};
use wasmtime::component::{Component, Linker, types::ComponentItem};

fn verified_stage(directory: &std::path::Path) -> Result<VerifiedPack> {
    let manifest = parse_manifest(&std::fs::read(directory.join("pack.json"))?)?;
    let mut files = BTreeMap::new();
    for entry in &manifest.files {
        files.insert(
            entry.path.clone(),
            std::fs::read(directory.join(&entry.path))?,
        );
    }
    Ok(verify_tpk(
        &build_tpk(&manifest, &files, None)?,
        &BTreeMap::new(),
    )?)
}

/// Additional test-only check for record aliases in the unchanged public WIT.
/// Call after validate_imports has enforced the existing Core/WASI allowlist.
/// This proves structural type agreement, not a new foreign nominal-identity
/// guarantee from the production validator's Core-only named-type branch.
pub(super) fn check_input_type_dependency_imports(
    engine: &wasmtime::Engine,
    component: &Component,
    pack: &VerifiedPack,
    shapes: &[tobkiri_manifest::ShapeDef],
) -> Result<()> {
    let mut owners = BTreeMap::new();
    for (interface, view) in [
        (
            "local:deepthink/workflow@1.0.0",
            "local.deepthink.workflow@1.0",
        ),
        (
            "local:deepthink/tool-receipt-reader@1.0.0",
            "local.deepthink.tool-receipt-reader@1.0",
        ),
        (
            "local:deepthink/goal-review-worker@1.0.0",
            "local.deepthink.goal-review-worker@1.0",
        ),
    ] {
        let reference = tobkiri_manifest::ShapeRef::parse(view)?;
        let owner = shapes
            .iter()
            .find(|shape| shape.reference == reference)
            .context("selected worker's authoritative record-owner shape is required")?;
        assert_eq!(
            pack.files
                .get("wit/deps/deepthink.wit")
                .context("adapter must bind the shared public WIT")?
                .as_slice(),
            owner.wit.as_bytes()
        );
        owners.insert(interface, owner);
    }
    let component_type = component.component_type();
    let mut interfaces = 0;
    let mut types = 0;
    let mut functions = 0;
    for (name, item) in component_type.imports(engine) {
        println!("input actual import {name}: {:?}", item.ty);
        if name.starts_with("wasi:")
            || matches!(name, "tobkiri:core/types@1.0.0" | "tobkiri:core/flow@1.0.0")
        {
            continue;
        }
        let owner = owners
            .get(name)
            .with_context(|| format!("unexpected foreign input dependency interface: {name}"))?;
        interfaces += 1;
        let ComponentItem::ComponentInstance(instance) = item.ty else {
            bail!("workflow type dependency must be an interface")
        };
        for (member, item) in instance.exports(engine) {
            println!("input workflow dependency member {member}: {:?}", item.ty);
            match item.ty {
                ComponentItem::Type(actual) => {
                    let expected = owner
                        .types
                        .get(member)
                        .context("unknown named type in workflow dependency")?;
                    if !super::matches_type(&actual, &super::from_type(expected), &BTreeMap::new())
                    {
                        bail!("workflow dependency type differs from authoritative WIT: {member}")
                    }
                    types += 1;
                }
                ComponentItem::ComponentFunc(_) => functions += 1,
                _ => bail!("workflow dependency is not a plain named type: {member}"),
            }
        }
    }
    println!(
        "input workflow dependency interfaces={interfaces} types={types} functions={functions}"
    );
    assert_eq!(interfaces, 3, "exact old and new record-owner dependencies");
    assert!(
        types > 0,
        "the dependency must contain actual record aliases"
    );
    assert_eq!(
        functions, 0,
        "record aliases must import no worker functions"
    );
    Ok(())
}

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
    let pack = verified_stage(&directory)?;
    assert_eq!(pack.manifest.id.as_str(), "local.deepthink");
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
    let required: [(&str, &[&str]); 4] = [
        (
            "local:deepthink/workflow@1.0.0",
            &["status", "reconcile-generation", "regenerate-phase"],
        ),
        (
            "local:deepthink/input-context@1.0.0",
            &["continuation", "regeneration", "control-result"],
        ),
        ("local:deepthink/tool-receipt-reader@1.0.0", &["page"]),
        ("local:deepthink/goal-review-worker@1.0.0", &["review"]),
    ];
    for (required_interface, operations) in required {
        let exported = component_type
            .exports(engine.engine())
            .find(|(name, _)| *name == required_interface)
            .with_context(|| format!("missing actual ledger export {required_interface}"))?
            .1;
        let ComponentItem::ComponentInstance(instance) = exported.ty else {
            bail!("ledger export must be a typed interface")
        };
        for required_operation in operations.iter().copied() {
            assert!(
                instance.exports(engine.engine()).any(|(name, item)| {
                    name == required_operation && matches!(item.ty, ComponentItem::ComponentFunc(_))
                }),
                "missing actual {required_interface}/{required_operation}"
            );
        }
    }
    assert!(
        !component_type
            .exports(engine.engine())
            .any(|(name, _)| name == "local:deepthink/continuation-input@1.0.0"),
        "the worker no longer implements the public input provider"
    );
    for public in [
        "local:deepthink/tool-diagnostics@1.0.0",
        "local:deepthink/goal-review-input@1.0.0",
    ] {
        assert!(
            !component_type
                .exports(engine.engine())
                .any(|(name, _)| name == public),
            "the worker must not provide a public input implementation"
        );
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

/// A distinct component provides the unchanged foreign-owned input contract.
/// Both artifacts are verified, and no callback or guest code is executed.
#[tokio::test]
#[ignore = "requires real immutable DeepThink ledger and input adapter stages"]
async fn real_input_adapter_matches_owned_wit_and_closed_core_abi() -> Result<()> {
    let worker_directory = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_DEEPTHINK_LEDGER_STAGE")
            .context("set TOBKIRI_TEST_DEEPTHINK_LEDGER_STAGE to the immutable worker stage")?,
    );
    let directory = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_DEEPTHINK_INPUT_STAGE")
            .context("set TOBKIRI_TEST_DEEPTHINK_INPUT_STAGE to the immutable adapter stage")?,
    );
    let worker = verified_stage(&worker_directory)?;
    let pack = verified_stage(&directory)?;
    assert_eq!(worker.manifest.id.as_str(), "local.deepthink");
    assert_eq!(pack.manifest.id.as_str(), "local.deepthink-input");
    assert!(pack.manifest.shapes.is_empty());
    assert!(pack.manifest.stores.is_empty() && pack.manifest.flows.is_empty());
    assert_eq!(
        pack.files.get("wit/deps/deepthink.wit"),
        worker.files.get("wit/deps/deepthink.wit")
    );
    let shapes = load_shapes(&worker.manifest, &worker.files)?;
    let views = shapes
        .iter()
        .map(|shape| (shape.reference.clone(), shape.clone()))
        .collect();
    let reference = tobkiri_manifest::ShapeRef::parse("local.deepthink.continuation-input@1.0")?;
    assert!(shapes.iter().any(|shape| shape.reference == reference));
    let inspection = inspect_pack_wit(&pack, &views)?;
    let dependency = pack
        .manifest
        .ports
        .consumes
        .iter()
        .find(|port| port.shape.as_str() == "local.deepthink.workflow")
        .context("public record aliases need their declared workflow type owner")?;
    assert!(!dependency.optional);
    assert!(
        !pack
            .manifest
            .permissions
            .ports
            .contains("local.deepthink.workflow@1.0")
    );
    for view in [
        "local.deepthink.tool-receipt-reader@1.0",
        "local.deepthink.goal-review-worker@1.0",
    ] {
        let reference = tobkiri_manifest::ShapeRef::parse(view)?;
        assert!(
            pack.manifest
                .ports
                .consumes
                .iter()
                .any(|p| p.shape == reference.name
                    && p.version == reference.version
                    && !p.optional)
        );
        assert!(
            !pack.manifest.permissions.ports.contains(view),
            "type dependency is not worker call authority"
        );
    }
    // Reproduce the original rejection with a normally built/verified negative
    // artifact. The runtime bytes and public WIT are unchanged; only the missing
    // dependency declaration differs, without adding any direct call authority.
    let mut missing_dependency_manifest = pack.manifest.clone();
    missing_dependency_manifest
        .ports
        .consumes
        .retain(|port| port.shape.as_str() != "local.deepthink.workflow");
    let missing_dependency = verify_tpk(
        &build_tpk(&missing_dependency_manifest, &pack.files, None)?,
        &BTreeMap::new(),
    )?;
    let missing_error = inspect_pack_wit(&missing_dependency, &views)
        .err()
        .context("an undeclared operational type owner must fail inspection")?;
    assert!(
        missing_error
            .to_string()
            .contains("undeclared type dependency interface has operations")
    );
    for operation in [
        "status",
        "submit",
        "reconcile-generation",
        "regenerate-phase",
        "pull",
    ] {
        assert!(inspection.core_errors().has_core_error_result(
            tobkiri_manifest::WitBindingDirection::Export,
            &reference,
            &tobkiri_manifest::OpName::parse(operation)?,
        ));
    }
    for (view, operation) in [
        ("local.deepthink.tool-diagnostics@1.0", "page"),
        ("local.deepthink.goal-review-input@1.0", "review"),
    ] {
        assert!(inspection.core_errors().has_core_error_result(
            tobkiri_manifest::WitBindingDirection::Export,
            &tobkiri_manifest::ShapeRef::parse(view)?,
            &tobkiri_manifest::OpName::parse(operation)?
        ));
    }
    let RuntimeSpec::Wasm { component, .. } = &pack.manifest.runtime else {
        bail!("the input adapter must be an actual Wasm component")
    };
    let engine = crate::WasmEngine::new()?;
    let bytes = &pack.files[component];
    let component = Component::new(engine.engine(), bytes)?;
    let imports =
        crate::validate_imports(engine.engine(), &component, inspection.allowed_imports())?;
    println!("input actual import names: {imports:?}");
    check_input_type_dependency_imports(engine.engine(), &component, &pack, &shapes)?;
    assert!(imports.iter().any(|name| name == "tobkiri:core/flow@1.0.0"));
    validate_abi(engine.engine(), &component, &pack, &shapes)?;
    let component_type = component.component_type();
    let exported = component_type
        .exports(engine.engine())
        .find(|(name, _)| *name == "local:deepthink/continuation-input@1.0.0")
        .context("the adapter lacks the exact foreign-owned public interface")?
        .1;
    let ComponentItem::ComponentInstance(instance) = exported.ty else {
        bail!("the input export must be a typed interface")
    };
    for operation in [
        "status",
        "submit",
        "reconcile-generation",
        "regenerate-phase",
        "pull",
        "cancel",
    ] {
        assert!(
            instance
                .exports(engine.engine())
                .any(|(name, item)| name == operation
                    && matches!(item.ty, ComponentItem::ComponentFunc(_)))
        );
    }
    for (interface, operation) in [
        ("local:deepthink/tool-diagnostics@1.0.0", "page"),
        ("local:deepthink/goal-review-input@1.0.0", "review"),
    ] {
        let item = component_type
            .exports(engine.engine())
            .find(|(name, _)| *name == interface)
            .context("new public ordinary input interface missing")?
            .1;
        let ComponentItem::ComponentInstance(instance) = item.ty else {
            bail!("public input must be an interface")
        };
        assert!(
            instance
                .exports(engine.engine())
                .any(|(name, item)| name == operation
                    && matches!(item.ty, ComponentItem::ComponentFunc(_)))
        );
    }
    assert!(crate::validate_imports(engine.engine(), &component, &Default::default()).is_err());
    let mut missing_flow = inspection.allowed_imports().clone();
    missing_flow.retain(|name| !name.starts_with("tobkiri:core/flow@"));
    assert!(crate::validate_imports(engine.engine(), &component, &missing_flow).is_err());
    let empty: Linker<()> = Linker::new(engine.engine());
    assert!(empty.instantiate_pre(&component).is_err());
    println!(
        "verified input ABI {} {} worker={} component={}",
        pack.manifest.id,
        pack.hash,
        worker.hash,
        tobkiri_canon::Digest::hash(bytes)
    );
    Ok(())
}
