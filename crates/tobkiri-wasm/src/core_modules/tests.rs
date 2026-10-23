use super::*;
use std::io::Read;
use tobkiri_gate::{ControlKind, CoreControl};
use tobkiri_manifest::source::SourceCatalog;
use wasmtime::component::{Component, Linker};

fn capture(catalog: &SourceCatalog, input: &mut impl Read, length: u64) -> StagedArtifact {
    let mut staging = catalog.begin_snapshot().unwrap();
    let range = staging.append(input, length).unwrap();
    staging.finish().unwrap().artifact(&range).unwrap()
}

#[test]
fn source_limits_count_every_root_and_only_share_distinct_explicit_module_code() {
    let control = CoreControl::default();
    let catalog = SourceCatalog::new(&control).unwrap();
    let _metadata = control.reserve(ControlKind::Catalog, 40_960).unwrap();
    let root = capture(&catalog, &mut std::io::repeat(1).take(20 << 20), 20 << 20);
    let module = capture(&catalog, &mut std::io::repeat(2).take(12 << 20), 12 << 20);
    let extra = capture(&catalog, &mut &b"x"[..], 1);
    let mut budget = SourceBudget::default();
    budget
        .add_pack(&root, [Ok(&module), Ok(&module)].into_iter())
        .unwrap();
    assert_eq!(budget.bytes, 32 << 20);
    budget.add_pack(&root, [Ok(&module)].into_iter()).unwrap();
    assert_eq!(
        budget.bytes,
        52 << 20,
        "identical root bytes still count twice"
    );
    budget.add_pack(&module, std::iter::empty()).unwrap();
    assert_eq!(
        budget.bytes,
        64 << 20,
        "a dependency reused as a root also counts"
    );
    assert!(
        budget
            .add_pack(&extra, std::iter::empty())
            .unwrap_err()
            .to_string()
            .contains("catalog")
    );
    let mut independent = SourceBudget::default();
    assert!(
        independent
            .add_pack(&root, [Ok(&module), Ok(&extra)].into_iter())
            .unwrap_err()
            .to_string()
            .contains("dependency closure")
    );
}

fn component(engine: &Engine, source: &str) -> Component {
    Component::new(engine, wat::parse_str(source).unwrap()).unwrap()
}

const IMPORT: &str = r#"(component
    (core module $code (import "unbundled-module0")
        (export "value" (func (result i32))))
    (core instance (instantiate $code)))"#;

#[test]
fn shared_code_reuses_only_authenticated_bytes_and_exact_root_module_bindings() {
    let engine = Engine::default();
    let control = CoreControl::default();
    let catalog = SourceCatalog::new(&control).unwrap();
    let _metadata = control.reserve(ControlKind::Catalog, 40_960).unwrap();
    let code =
        wat::parse_str("(module (func (export \"value\") (result i32) i32.const 7))").unwrap();
    let other =
        wat::parse_str("(module (func (export \"value\") (result i32) i32.const 8))").unwrap();
    let first = capture(&catalog, &mut code.as_slice(), code.len() as u64);
    let duplicate = capture(&catalog, &mut code.as_slice(), code.len() as u64);
    let different = capture(&catalog, &mut other.as_slice(), other.len() as u64);
    let mut cache = CodeCache::default();
    let used = control.usage();
    let a = cache.load(&engine, &first).unwrap();
    assert_eq!(
        control.usage(),
        used,
        "temporary source bytes must be released"
    );
    let b = cache.load(&engine, &duplicate).unwrap();
    assert!(
        Module::same(&a, &b),
        "equal bytes from separate snapshots share compiled code"
    );
    let c = cache.load(&engine, &different).unwrap();
    assert!(!Module::same(&a, &c), "different actual bytes cannot alias");
    assert!(cache.load(&Engine::default(), &first).is_err());
    let wrapper = component(&engine, IMPORT);
    let modules = BTreeMap::from([("unbundled-module0".into(), a)]);
    crate::imports::validate_imports_with_modules(&engine, &wrapper, &BTreeSet::new(), &modules)
        .unwrap();
    assert!(
        crate::validate_imports(&engine, &wrapper, &BTreeSet::new()).is_err(),
        "ordinary interface validation must not allow undeclared code"
    );
    let mut linker = Linker::<()>::new(&engine);
    linker.root().module("unbundled-module0", &b).unwrap();
    linker.instantiate_pre(&wrapper).unwrap();
    let wrong_type = component(
        &engine,
        r#"(component
        (core module (import "unbundled-module0") (export "value" (func (result i64)))))"#,
    );
    assert!(
        linker.instantiate_pre(&wrong_type).is_err(),
        "type errors fail before any guest code"
    );
    let extra = component(&engine, "(component)");
    assert!(
        crate::imports::validate_imports_with_modules(&engine, &extra, &BTreeSet::new(), &modules)
            .is_err()
    );
    let wrong_kind = component(
        &engine,
        r#"(component (import "unbundled-module0" (instance)))"#,
    );
    assert!(
        crate::imports::validate_imports_with_modules(
            &engine,
            &wrong_kind,
            &BTreeSet::from(["unbundled-module0".into()]),
            &modules
        )
        .is_err()
    );
    let ambient = component(
        &engine,
        r#"(component
        (import "wasi:sockets/tcp@0.2.6" (instance))
        (core module (import "unbundled-module0") (export "value" (func (result i32)))))"#,
    );
    assert!(
        crate::imports::validate_imports_with_modules(
            &engine,
            &ambient,
            &BTreeSet::new(),
            &modules
        )
        .is_err()
    );
    let malformed = capture(&catalog, &mut &b"bad!"[..], 4);
    let count = cache.0.len();
    assert!(cache.load(&engine, &malformed).is_err());
    assert_eq!(
        cache.0.len(),
        count,
        "failed compilation cannot publish a cache entry"
    );
    assert_eq!(control.usage().count(ControlKind::Load), 0);
    let holds: Vec<_> = (0..4)
        .map(|_| control.reserve(ControlKind::Load, 1).unwrap())
        .collect();
    assert!(
        cache.load(&engine, &first).is_err(),
        "a hit still needs funded verification of its own source"
    );
    drop(holds);
    assert!(Module::same(&b, &cache.load(&engine, &first).unwrap()));
}
