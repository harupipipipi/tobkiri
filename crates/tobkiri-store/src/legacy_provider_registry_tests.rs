use super::*;
use std::{
    fs::{self, OpenOptions},
    io::ErrorKind,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
};

const MAX: usize = 1024;
const PROFILE_PATH: &str = "packs/rumi_provider_registry_pack/profiles/defaults";
const STORE_NAME: &str = "provider_registry.store.json";
const LOCK_PATH: &str = "locks/provider-registry.lock";

fn fixture() -> (tempfile::TempDir, DataRoot, ProfileId, PathBuf) {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
    let registry = root.path().join(PROFILE_PATH);
    (temp, root, ProfileId::parse("defaults").unwrap(), registry)
}

fn assert_busy(result: Result<LegacyProviderRegistryTransaction, StoreError>) {
    assert!(
        matches!(result, Err(StoreError::Io(error)) if error.kind() == ErrorKind::AlreadyExists)
    );
}

fn assert_no_staged_files(registry: &Path) {
    for entry in fs::read_dir(registry).unwrap() {
        let name = entry.unwrap().file_name();
        assert!(name == STORE_NAME || name == "locks");
    }
}

#[test]
fn read_only_registry_observes_stable_absence_without_creating_directories_or_lock() {
    let (_temp, root, profile, registry) = fixture();
    assert_eq!(
        root.read_legacy_provider_registry(&profile, MAX).unwrap(),
        None
    );
    assert!(!root.path().join("packs").exists());
    fs::create_dir_all(registry.parent().unwrap()).unwrap();
    assert_eq!(
        root.read_legacy_provider_registry(&profile, MAX).unwrap(),
        None
    );
    assert!(!registry.exists());
    fs::create_dir(&registry).unwrap();
    assert_eq!(
        root.read_legacy_provider_registry(&profile, MAX).unwrap(),
        None
    );
    assert_eq!(fs::read_dir(&registry).unwrap().count(), 0);
}

#[test]
fn read_only_registry_respects_the_cap_even_while_the_publication_lock_is_held() {
    let (_temp, root, profile, registry) = fixture();
    fs::create_dir_all(&registry).unwrap();
    fs::set_permissions(&registry, fs::Permissions::from_mode(0o755)).unwrap();
    let store = registry.join(STORE_NAME);
    fs::write(&store, b"original").unwrap();

    assert_eq!(
        root.read_legacy_provider_registry(&profile, 8)
            .unwrap()
            .as_deref(),
        Some(b"original".as_slice())
    );
    assert!(!registry.join("locks").exists());

    let transaction = root.lock_legacy_provider_registry(&profile).unwrap();
    let lock = fs::read(registry.join(LOCK_PATH)).unwrap();
    assert!(matches!(
        root.read_legacy_provider_registry(&profile, 7),
        Err(StoreError::Quota)
    ));
    assert_eq!(
        root.read_legacy_provider_registry(&profile, 8)
            .unwrap()
            .as_deref(),
        Some(b"original".as_slice())
    );
    assert_eq!(fs::read(&store).unwrap(), b"original");
    assert_eq!(fs::read(registry.join(LOCK_PATH)).unwrap(), lock);
    assert_no_staged_files(&registry);
    drop(transaction);
}

#[test]
fn read_only_registry_rejects_a_detached_or_replaced_root_as_a_conflict() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().canonicalize().unwrap().join("data");
    let root = DataRoot::open(&path).unwrap();
    let profile = ProfileId::parse("defaults").unwrap();
    fs::create_dir_all(path.join(PROFILE_PATH)).unwrap();
    fs::write(path.join(PROFILE_PATH).join(STORE_NAME), b"detached").unwrap();
    fs::rename(&path, path.with_file_name("old-data")).unwrap();
    assert!(matches!(
        root.read_legacy_provider_registry(&profile, MAX),
        Err(StoreError::Conflict(_))
    ));
    fs::create_dir(&path).unwrap();
    assert!(matches!(
        root.read_legacy_provider_registry(&profile, MAX),
        Err(StoreError::Conflict(_))
    ));
    assert_eq!(fs::read_dir(&path).unwrap().count(), 0);
}

#[test]
fn interoperates_with_legacy_named_lock_and_never_breaks_a_stale_file() {
    let (_temp, root, profile, registry) = fixture();
    let lock_path = registry.join(LOCK_PATH);
    let transaction = root.lock_legacy_provider_registry(&profile).unwrap();
    let info: serde_json::Value = serde_json::from_slice(&fs::read(&lock_path).unwrap()).unwrap();
    let info = info.as_object().unwrap();
    assert_eq!(info.len(), 4);
    assert!(!info["owner"].as_str().unwrap().is_empty());
    assert_eq!(info["pid"], std::process::id());
    assert!(info["acquired_at"].as_f64().unwrap() > 0.0);
    assert!(info["stale_after_seconds"].is_null());
    assert_eq!(
        fs::metadata(&lock_path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&lock_path)
            .unwrap_err()
            .kind(),
        ErrorKind::AlreadyExists
    );
    assert_busy(root.lock_legacy_provider_registry(&profile));
    drop(transaction);
    assert!(!lock_path.exists());

    let legacy = br#"{"acquired_at":0.0,"owner":"pid:999","pid":999,"stale_after_seconds":0}"#;
    fs::write(&lock_path, legacy).unwrap();
    assert_busy(root.lock_legacy_provider_registry(&profile));
    assert_eq!(fs::read(&lock_path).unwrap(), legacy);
    assert!(!registry.join(STORE_NAME).exists());
}

#[test]
fn reads_and_replaces_only_the_original_owner_only_store() {
    let (_temp, root, profile, registry) = fixture();
    let transaction = root.lock_legacy_provider_registry(&profile).unwrap();
    assert_eq!(transaction.read(MAX).unwrap(), None);
    assert!(!transaction.confirm_exact(b"next", MAX).unwrap());
    assert_eq!(
        fs::metadata(&registry).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(registry.join("locks"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    transaction
        .publish(None, b"next", MAX, || Ok::<_, ()>(()))
        .unwrap();
    assert_eq!(
        transaction.read(MAX).unwrap().as_deref(),
        Some(b"next".as_slice())
    );
    assert!(matches!(transaction.read(3), Err(StoreError::Quota)));
    assert!(!transaction.confirm_exact(b"next", MAX).unwrap());
    assert_no_staged_files(&registry);
    drop(transaction);
    let store = registry.join(STORE_NAME);
    assert_eq!(fs::read(&store).unwrap(), b"next");
    assert_eq!(
        fs::metadata(&store).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(fs::read_dir(registry.join("locks")).unwrap().count(), 0);

    let second = root.lock_legacy_provider_registry(&profile).unwrap();
    second
        .publish(Some(b"next"), b"updated", MAX, || Ok::<_, ()>(()))
        .unwrap();
    assert_eq!(fs::read(store).unwrap(), b"updated");
}

#[test]
fn authority_denial_occurs_after_staging_and_cleans_up_the_private_file() {
    let (_temp, root, profile, registry) = fixture();
    let transaction = root.lock_legacy_provider_registry(&profile).unwrap();
    let result = transaction.publish(None, b"intended", MAX, || {
        assert!(registry.join(LOCK_PATH).is_file());
        assert!(!registry.join(STORE_NAME).exists());
        let staged = fs::read_dir(&registry)
            .unwrap()
            .map(Result::unwrap)
            .find(|entry| entry.file_name().to_string_lossy().starts_with(".tmp-"))
            .unwrap();
        assert_eq!(
            staged.metadata().unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(fs::read(staged.path()).unwrap(), b"intended");
        Err("lease expired")
    });
    assert!(matches!(
        result,
        Err(LegacyProviderRegistryPublishError::Guard("lease expired"))
    ));
    assert!(!transaction.confirm_exact(b"intended", MAX).unwrap());
    assert_eq!(transaction.read(MAX).unwrap(), None);
    assert_no_staged_files(&registry);
    drop(transaction);
    assert!(!registry.join(LOCK_PATH).exists());
}

#[test]
fn cas_rejects_stale_bytes_and_a_file_that_changes_during_the_guard() {
    let (_temp, root, profile, registry) = fixture();
    let transaction = root.lock_legacy_provider_registry(&profile).unwrap();
    let store = registry.join(STORE_NAME);
    fs::write(&store, b"actual").unwrap();
    assert!(matches!(
        transaction.publish(Some(b"stale"), b"next", MAX, || -> Result<(), ()> {
            panic!("stale data must fail before the final authority check")
        }),
        Err(LegacyProviderRegistryPublishError::BeforePublish(
            StoreError::Conflict(_)
        ))
    ));
    assert_no_staged_files(&registry);

    assert!(matches!(
        transaction.publish(Some(b"actual"), b"next", MAX, || {
            fs::write(&store, b"changed-during-guard").unwrap();
            Ok::<_, ()>(())
        }),
        Err(LegacyProviderRegistryPublishError::BeforePublish(
            StoreError::Conflict(_)
        ))
    ));
    assert!(!transaction.confirm_exact(b"next", MAX).unwrap());
    assert_eq!(fs::read(&store).unwrap(), b"changed-during-guard");
    assert_no_staged_files(&registry);
}

#[test]
fn replacement_lock_is_never_removed_or_used_for_publication() {
    let (_temp, root, profile, registry) = fixture();
    let transaction = root.lock_legacy_provider_registry(&profile).unwrap();
    let lock_path = registry.join(LOCK_PATH);
    assert!(matches!(
        transaction.publish(None, b"next", MAX, || {
            fs::remove_file(&lock_path).unwrap();
            fs::write(&lock_path, b"successor").unwrap();
            Ok::<_, ()>(())
        }),
        Err(LegacyProviderRegistryPublishError::BeforePublish(_))
    ));
    assert!(transaction.read(MAX).is_err());
    assert_no_staged_files(&registry);
    drop(transaction);
    assert_eq!(fs::read(&lock_path).unwrap(), b"successor");
    assert!(!registry.join(STORE_NAME).exists());
}

#[test]
fn detached_registry_directory_never_receives_a_publish_or_leaks_staged_files() {
    let (_temp, root, profile, registry) = fixture();
    let transaction = root.lock_legacy_provider_registry(&profile).unwrap();
    let detached = registry.with_file_name("detached");
    assert!(matches!(
        transaction.publish(None, b"next", MAX, || {
            fs::rename(&registry, &detached).unwrap();
            fs::create_dir_all(registry.join("locks")).unwrap();
            fs::write(registry.join(LOCK_PATH), b"successor").unwrap();
            Ok::<_, ()>(())
        }),
        Err(LegacyProviderRegistryPublishError::BeforePublish(_))
    ));
    assert_no_staged_files(&detached);
    assert!(!detached.join(STORE_NAME).exists());
    assert!(!registry.join(STORE_NAME).exists());
    drop(transaction);
    assert!(!detached.join(LOCK_PATH).exists());
    assert_eq!(fs::read(registry.join(LOCK_PATH)).unwrap(), b"successor");
}

#[test]
fn refuses_symlinks_and_multilink_store_entries() {
    let (temp, root, profile, registry) = fixture();
    let outside = temp.path().join("outside");
    fs::create_dir(&outside).unwrap();
    fs::create_dir_all(registry.parent().unwrap()).unwrap();
    symlink(&outside, &registry).unwrap();
    assert!(root.lock_legacy_provider_registry(&profile).is_err());
    assert!(root.read_legacy_provider_registry(&profile, MAX).is_err());
    assert_eq!(fs::read_dir(&outside).unwrap().count(), 0);
    fs::remove_file(&registry).unwrap();

    let transaction = root.lock_legacy_provider_registry(&profile).unwrap();
    let outside_file = outside.join("foreign");
    fs::write(&outside_file, b"unchanged").unwrap();
    let store = registry.join(STORE_NAME);
    symlink(&outside_file, &store).unwrap();
    assert!(matches!(transaction.read(MAX), Err(StoreError::Denied(_))));
    assert!(matches!(
        root.read_legacy_provider_registry(&profile, MAX),
        Err(StoreError::Denied(_))
    ));
    assert!(matches!(
        transaction.publish(None, b"next", MAX, || Ok::<_, ()>(())),
        Err(LegacyProviderRegistryPublishError::BeforePublish(
            StoreError::Denied(_)
        ))
    ));
    fs::remove_file(&store).unwrap();
    fs::hard_link(&outside_file, &store).unwrap();
    assert!(matches!(transaction.read(MAX), Err(StoreError::Denied(_))));
    assert!(matches!(
        root.read_legacy_provider_registry(&profile, MAX),
        Err(StoreError::Denied(_))
    ));
    assert_eq!(fs::read(outside_file).unwrap(), b"unchanged");
    assert_no_staged_files(&registry);
    drop(transaction);

    symlink(outside, registry.join(LOCK_PATH)).unwrap();
    assert!(matches!(
        root.lock_legacy_provider_registry(&profile),
        Err(StoreError::Denied(_))
    ));
}

#[test]
fn uncertain_replace_can_be_confirmed_only_by_exact_owned_inode_and_bytes() {
    let (_temp, root, profile, registry) = fixture();
    let transaction = root.lock_legacy_provider_registry(&profile).unwrap();
    let result = transaction.inner.publish(
        None,
        b"next",
        MAX,
        || Ok::<_, ()>(()),
        |_| Err(std::io::Error::other("injected directory sync failure")),
    );
    assert!(matches!(
        result,
        Err(LegacyProviderRegistryPublishError::Uncertain(_))
    ));
    assert!(transaction.confirm_exact(b"next", MAX).unwrap());
    assert!(!transaction.confirm_exact(b"different bytes", MAX).unwrap());
    assert!(matches!(
        transaction.publish(Some(b"next"), b"another", MAX, || Ok::<_, ()>(())),
        Err(LegacyProviderRegistryPublishError::BeforePublish(
            StoreError::Conflict(_)
        ))
    ));
    assert_no_staged_files(&registry);

    let store = registry.join(STORE_NAME);
    fs::remove_file(&store).unwrap();
    fs::write(&store, b"next").unwrap();
    assert!(!transaction.confirm_exact(b"next", MAX).unwrap());
}
