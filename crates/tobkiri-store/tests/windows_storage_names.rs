#![cfg(windows)]

use tobkiri_canon::ids::{PackId, ProfileId};
use tobkiri_gate::StoreTier;
use tobkiri_store::{DataRoot, TieredStore};

#[test]
fn case_ambiguous_profiles_and_public_stores_are_rejected_without_aliasing() {
    let temporary = tempfile::Builder::new()
        .prefix(".windows-names-")
        .tempdir_in(std::env::current_dir().unwrap())
        .unwrap();
    let root = DataRoot::open(temporary.path().join("data")).unwrap();
    let profile = ProfileId::parse("prof_names").unwrap();
    let alias = ProfileId::parse("Prof_names").unwrap();
    let pack = PackId::parse("local.names-test").unwrap();
    let store = TieredStore::open(&root, &profile).unwrap();
    assert!(TieredStore::open(&root, &alias).is_err());
    store
        .register(&pack, "entries", StoreTier::Public, "local.fixture.entry@1")
        .unwrap();
    assert!(
        store
            .register(
                &pack,
                "Entries",
                StoreTier::ReadOnly,
                "local.fixture.entry@1"
            )
            .is_err()
    );
    assert!(
        store
            .register(&pack, "ENTRIES", StoreTier::Public, "local.fixture.entry@1")
            .is_err()
    );
    assert!(store.describe(&pack, "Entries").is_err());
    assert_eq!(
        store.describe(&pack, "entries").unwrap().tier,
        StoreTier::Public
    );
    store
        .register(
            &pack,
            "separate",
            StoreTier::ReadOnly,
            "local.fixture.entry@1",
        )
        .unwrap();
    assert_eq!(
        store.describe(&pack, "separate").unwrap().tier,
        StoreTier::ReadOnly
    );
}
