//! The complete generic component ABI. Domain interfaces belong to packages.

use anyhow::{Context, Result, bail};
use std::collections::BTreeSet;
use std::path::Path;
use wit_parser::{PackageId, Resolve};
mod abi;
pub use abi::{AbiFunction, AbiInterface, AbiType, core_abi, core_abi_version};

pub const CORE_WIT_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/wit/core");
pub const CORE_WIT_SOURCE: &str = include_str!("../wit/core/core.wit");
pub const CORE_PACKAGE: &str = "tobkiri:core@1.0.0";
pub const CORE_V1_1_WIT_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/wit/core-v1-1");
pub const CORE_V1_1_WIT_SOURCE: &str = include_str!("../wit/core-v1-1/core.wit");
pub const CORE_V1_1_PACKAGE: &str = "tobkiri:core@1.1.0";
/// Unpromoted draft: exact-source consumers must be rebuilt after any change.
pub const CORE_V1_2_WIT_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/wit/core-v1-2");
pub const CORE_V1_2_WIT_SOURCE: &str = include_str!("../wit/core-v1-2/core.wit");
pub const CORE_V1_2_PACKAGE: &str = "tobkiri:core@1.2.0";

/// The frozen 17 interfaces and their issue R1.2 categories.
pub const CORE_INTERFACES: &[(&str, &str)] = &[
    ("types", "shared-types"),
    ("network", "generic"),
    ("fs", "generic"),
    ("proc", "generic"),
    ("pty", "generic"),
    ("clock", "generic"),
    ("secrets", "generic"),
    ("random", "generic"),
    ("storage", "storage"),
    ("gate-mediator", "gate"),
    ("approvals", "gate"),
    ("flow", "flow"),
    ("lifecycle", "flow"),
    ("stream-source", "flow"),
    ("callback-sink", "flow"),
    ("audit", "audit"),
    ("host-io", "host-io"),
];

/// Imports present in every generated world. Permission imports are separate.
pub const MINIMAL_IMPORTS: &[&str] = &["types", "flow", "clock", "random", "audit"];

/// Parse the committed WIT with its authoritative upstream parser.
pub fn resolve_core() -> Result<(Resolve, PackageId)> {
    resolve_core_version("1.0.0")
}

/// Resolve an exact supported ABI package; versions never merge resource identities.
pub fn resolve_core_version(version: &str) -> Result<(Resolve, PackageId)> {
    let (directory, expected) = match version {
        "1.0.0" => (CORE_WIT_DIR, CORE_PACKAGE),
        "1.1.0" => (CORE_V1_1_WIT_DIR, CORE_V1_1_PACKAGE),
        "1.2.0" => (CORE_V1_2_WIT_DIR, CORE_V1_2_PACKAGE),
        _ => bail!("unsupported core ABI version"),
    };
    let mut resolve = Resolve::default();
    let (package, _) = resolve
        .push_dir(Path::new(directory))
        .context("parse the generic component ABI")?;
    validate_package(&resolve, package, expected)?;
    Ok((resolve, package))
}

/// Enforce the closed generic namespace, interfaces and minimal world.
pub fn validate_core(resolve: &Resolve, package: PackageId) -> Result<()> {
    validate_package(resolve, package, CORE_PACKAGE)
}
fn validate_package(resolve: &Resolve, package: PackageId, expected_package: &str) -> Result<()> {
    let package = &resolve.packages[package];
    if package.name.to_string() != expected_package {
        bail!("unexpected core WIT package {}", package.name);
    }
    let actual: BTreeSet<_> = package.interfaces.keys().map(String::as_str).collect();
    let expected: BTreeSet<_> = CORE_INTERFACES.iter().map(|(name, _)| *name).collect();
    if actual != expected {
        bail!("core interface set changed: {actual:?}");
    }
    let world = package
        .worlds
        .get("pack-minimal")
        .context("missing pack-minimal")?;
    let world = &resolve.worlds[*world];
    let actual: BTreeSet<_> = world
        .imports
        .values()
        .filter_map(|item| match item {
            wit_parser::WorldItem::Interface { id, .. } => resolve.interfaces[*id].name.as_deref(),
            _ => None,
        })
        .collect();
    let expected: BTreeSet<_> = MINIMAL_IMPORTS.iter().copied().collect();
    if actual != expected {
        bail!("minimal world has unexpected imports: {actual:?}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frozen_generic_abi_parses_and_has_exact_interface_categories() {
        let (resolve, package) = resolve_core().expect("valid frozen WIT");
        assert_eq!(resolve.packages[package].interfaces.len(), 17);
        assert_eq!(
            CORE_INTERFACES
                .iter()
                .filter(|(_, category)| *category == "generic")
                .count(),
            7
        );
        assert!(!resolve.packages[package].worlds.contains_key("pack-base"));
    }

    #[test]
    fn minimal_world_does_not_import_privileged_effects() {
        let (resolve, package) = resolve_core().unwrap();
        let world = resolve.packages[package].worlds["pack-minimal"];
        for item in resolve.worlds[world].imports.values() {
            if let wit_parser::WorldItem::Interface { id, .. } = item {
                assert!(
                    MINIMAL_IMPORTS.contains(&resolve.interfaces[*id].name.as_deref().unwrap())
                );
            }
        }
    }

    #[test]
    fn typed_storage_is_additive_in_its_exact_package() {
        let old = core_abi().unwrap();
        let new = core_abi_version("1.1.0").unwrap();
        assert_eq!(new.len(), 17);
        let storage = &new["storage"];
        assert!(storage.resources.contains_key("sql-transaction"));
        for method in [
            "[method]store.sql-exec-typed",
            "[method]store.sql-query-typed",
            "[method]store.sql-begin",
            "[method]sql-transaction.sql-exec-typed",
            "[method]sql-transaction.sql-query-typed",
            "[method]sql-transaction.commit",
            "[method]sql-transaction.rollback",
        ] {
            assert!(storage.functions.contains_key(method));
            assert!(!old["storage"].functions.contains_key(method));
        }
        assert!(!old["storage"].resources.contains_key("sql-transaction"));
    }

    #[test]
    fn unpromoted_profile_observation_is_only_in_exact_core_1_2() {
        let old = core_abi().unwrap();
        let previous = core_abi_version("1.1.0").unwrap();
        let draft = core_abi_version("1.2.0").unwrap();
        assert_eq!(draft.len(), 17);
        assert!(!old["flow"].functions.contains_key("current-profile"));
        assert!(!previous["flow"].functions.contains_key("current-profile"));
        let getter = &draft["flow"].functions["current-profile"];
        assert!(getter.params.is_empty());
        assert!(matches!(
            getter.result.as_ref(),
            Some(AbiType::Result(Some(ok), Some(_)))
                if matches!(ok.as_ref(), AbiType::Primitive("string"))
        ));
        assert!(draft["storage"].resources.contains_key("sql-transaction"));
        let (resolve, package) = resolve_core_version("1.2.0").unwrap();
        assert_eq!(resolve.packages[package].interfaces.len(), 17);
        assert!(resolve_core_version("1.3.0").is_err());
    }
}
