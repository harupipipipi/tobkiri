//! Actual upstream WIT parsing, not nativeKit component or GUI execution.
use std::{collections::BTreeSet, path::Path};
use wit_parser::{Resolve, Type, TypeDefKind, WorldItem};

fn unalias(resolve: &Resolve, mut ty: Type) -> Type {
    while let Type::Id(id) = ty {
        if let TypeDefKind::Type(next) = resolve.types[id].kind {
            ty = next;
        } else {
            break;
        }
    }
    ty
}

#[test]
fn whole_native_contract_keeps_original_methods_and_only_fixed_native_inspection_imports() {
    let mut resolve = Resolve::default();
    let directory =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/python313-producer/harness_native");
    let (package, _) = resolve
        .push_dir(&directory)
        .expect("parse actual nativeKit WIT");
    let package = &resolve.packages[package];
    assert_eq!(package.name.to_string(), "tobkiri-ui:harness-native@1.0.0");
    let world = &resolve.worlds[package.worlds["harness-native"]];
    assert_eq!(world.imports.len(), 1);
    let WorldItem::Interface { id, .. } = world.imports.values().next().unwrap() else {
        panic!("only the fixed native inspection interface is allowed");
    };
    let native = &resolve.interfaces[*id];
    assert_eq!(native.name.as_deref(), Some("bindings"));
    assert_eq!(
        resolve.packages[native.package.unwrap()].name.to_string(),
        "tobkiri-native:harness@1.0.0"
    );
    assert!(native.types.is_empty());
    assert_eq!(
        native
            .functions
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["inventory", "inspect"]
    );
    let inventory = &native.functions["inventory"];
    assert!(inventory.params.is_empty());
    assert_eq!(inventory.result, Some(Type::String));
    let inspect = &native.functions["inspect"];
    assert_eq!(inspect.params.len(), 1);
    assert_eq!(inspect.params[0].name, "payload");
    assert_eq!(inspect.params[0].ty, Type::String);
    assert_eq!(inspect.result, Some(Type::String));
    let interfaces: BTreeSet<_> = world
        .exports
        .values()
        .map(|item| match item {
            WorldItem::Interface { id, .. } => resolve.interfaces[*id].name.as_deref().unwrap(),
            _ => panic!("only the two nativeKit DATA interfaces may be exported"),
        })
        .collect();
    assert_eq!(interfaces, BTreeSet::from(["runtime", "transport"]));
    let runtime = &resolve.interfaces[package.interfaces["runtime"]];
    let names = [
        "init",
        "bundled-profile-root",
        "host-resource-root",
        "bootstrap-profile-id",
        "setup-profile-preview",
        "confirmation",
        "bootstrap-confirmation",
        "profile-confirmation",
        "setup-listing",
        "setup-preview",
        "setup-activation-decision",
        "setup-activation-success",
        "setup-activation-failure",
        "retired-setup-response",
        "load-catalog",
        "catalog-with-packs",
        "catalog-with-profiles",
        "resolve-profile",
        "dynamic-profile-edges",
        "denied",
    ];
    assert_eq!(
        runtime
            .functions
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        names
    );
    let transport = &resolve.interfaces[package.interfaces["transport"]];
    let helpers = [
        "import-values",
        "describe-value",
        "copy-data-dictionary",
        "release-values",
        "append-data-item",
        "set-data-item",
        "native-inputs",
    ];
    assert_eq!(
        transport
            .functions
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        helpers
    );
    let TypeDefKind::Record(error) = &resolve.types[runtime.types["error"]].kind else {
        panic!("closed error record required");
    };
    assert_eq!(
        error
            .fields
            .iter()
            .map(|f| f.name.as_str())
            .collect::<Vec<_>>(),
        ["code", "message", "details"]
    );
    let Type::Id(code) = unalias(&resolve, error.fields[0].ty) else {
        panic!("closed error enum required");
    };
    let TypeDefKind::Enum(codes) = &resolve.types[code].kind else {
        panic!("closed error enum required");
    };
    assert_eq!(
        codes
            .cases
            .iter()
            .map(|c| c.name.as_str())
            .collect::<Vec<_>>(),
        [
            "not-initialized",
            "already-initialized",
            "invalid-frame",
            "invalid-reference",
            "unsupported-value",
            "method-raised"
        ]
    );
    assert!(
        error.fields[1..]
            .iter()
            .all(|f| matches!(f.ty, Type::String))
    );
    for (name, function) in runtime.functions.iter().chain(&transport.functions) {
        assert_eq!(function.params.len(), 1);
        assert_eq!(function.params[0].name, "payload");
        assert!(matches!(function.params[0].ty, Type::String));
        let Some(Type::Id(result)) = function.result else {
            panic!("all nativeKit calls require an explicit result");
        };
        let TypeDefKind::Result(result) = &resolve.types[result].kind else {
            panic!("all nativeKit calls require an explicit result");
        };
        let unit = matches!(
            name.as_str(),
            "init" | "release-values" | "append-data-item" | "set-data-item"
        );
        assert_eq!(result.ok, if unit { None } else { Some(Type::String) });
        assert_eq!(
            unalias(&resolve, result.err.expect("closed error")),
            Type::Id(runtime.types["error"])
        );
    }
}
