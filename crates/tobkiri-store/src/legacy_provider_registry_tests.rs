use super::*;
use crate::{LegacyProviderRegistryPublishError, LegacyProviderRegistryTransaction};
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
const MODEL_PROFILE_PATH: &str = "packs/rumi_model_registry_pack/profiles/defaults";
const MODEL_STORE_NAME: &str = "model_registry.store.json";
const MODEL_LOCK_PATH: &str = "locks/model-registry.lock";
const CONVERSATION_PROFILE_PATH: &str = "packs/rumi_conversation_store_pack/profiles/defaults";
const CONVERSATION_STORE_NAME: &str = "conversations.json";
const CONVERSATION_LOCK_PATH: &str = "locks/conversations.lock";

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
fn model_registry_absence_never_creates_directories_or_locks() {
    let (_temp, root, profile, _) = fixture();
    let registry = root.path().join(MODEL_PROFILE_PATH);
    assert_eq!(
        root.read_legacy_model_registry(&profile, MAX).unwrap(),
        None
    );
    assert!(!root.path().join("packs").exists());

    fs::create_dir_all(registry.parent().unwrap()).unwrap();
    assert_eq!(
        root.read_legacy_model_registry(&profile, MAX).unwrap(),
        None
    );
    assert!(!registry.exists());

    fs::create_dir(&registry).unwrap();
    assert_eq!(
        root.read_legacy_model_registry(&profile, MAX).unwrap(),
        None
    );
    assert_eq!(fs::read_dir(&registry).unwrap().count(), 0);
}

#[test]
fn conversation_read_uses_only_the_original_pack_and_selected_profile() {
    let (_temp, root, profile, provider) = fixture();
    let conversation = root.path().join(CONVERSATION_PROFILE_PATH);
    let other = conversation.with_file_name("other");
    assert_eq!(
        root.read_legacy_conversation_store(&profile, MAX).unwrap(),
        None
    );
    assert!(!root.path().join("packs").exists());

    for directory in [&provider, &other] {
        fs::create_dir_all(directory).unwrap();
        fs::write(directory.join(CONVERSATION_STORE_NAME), b"unrelated").unwrap();
    }
    assert_eq!(
        root.read_legacy_conversation_store(&profile, MAX).unwrap(),
        None
    );
    assert!(!conversation.exists());
    fs::create_dir(&conversation).unwrap();
    assert_eq!(
        root.read_legacy_conversation_store(&profile, MAX).unwrap(),
        None
    );
    assert_eq!(fs::read_dir(&conversation).unwrap().count(), 0);

    let store = conversation.join(CONVERSATION_STORE_NAME);
    let contents = b"{\r\n  \"model\": \"model\"\r\n}";
    fs::write(&store, contents).unwrap();
    assert_eq!(
        root.read_legacy_conversation_store(&profile, contents.len())
            .unwrap()
            .as_deref(),
        Some(contents.as_slice())
    );
    assert!(matches!(
        root.read_legacy_conversation_store(&profile, contents.len() - 1),
        Err(StoreError::Quota)
    ));
    assert_eq!(fs::read(&store).unwrap(), contents);
    assert_eq!(fs::read_dir(&conversation).unwrap().count(), 1);
}

#[test]
fn conversation_read_rejects_linked_directories_and_files() {
    let (_temp, root, profile, provider) = fixture();
    let conversation = root.path().join(CONVERSATION_PROFILE_PATH);
    let foreign = provider.join(CONVERSATION_STORE_NAME);
    fs::create_dir_all(&provider).unwrap();
    fs::write(&foreign, b"foreign").unwrap();
    fs::create_dir_all(conversation.parent().unwrap()).unwrap();
    symlink(&provider, &conversation).unwrap();
    assert!(root.read_legacy_conversation_store(&profile, MAX).is_err());
    fs::remove_file(&conversation).unwrap();

    fs::create_dir(&conversation).unwrap();
    let store = conversation.join(CONVERSATION_STORE_NAME);
    symlink(&foreign, &store).unwrap();
    assert!(matches!(
        root.read_legacy_conversation_store(&profile, MAX),
        Err(StoreError::Denied(_))
    ));
    fs::remove_file(&store).unwrap();
    fs::hard_link(&foreign, &store).unwrap();
    assert!(matches!(
        root.read_legacy_conversation_store(&profile, MAX),
        Err(StoreError::Denied(_))
    ));
    assert_eq!(fs::read(&foreign).unwrap(), b"foreign");
    assert_eq!(fs::read_dir(&conversation).unwrap().count(), 1);
}

#[test]
fn conversation_lock_requires_an_existing_owned_store_without_creating_ancestors() {
    let (temp, root, profile, _) = fixture();
    let conversation = root.path().join(CONVERSATION_PROFILE_PATH);
    let locks = conversation.join("locks");
    assert!(matches!(
        root.lock_legacy_conversation_store(&profile),
        Err(StoreError::NotFound)
    ));
    assert!(!root.path().join("packs").exists());

    fs::create_dir_all(conversation.parent().unwrap()).unwrap();
    assert!(matches!(
        root.lock_legacy_conversation_store(&profile),
        Err(StoreError::NotFound)
    ));
    assert!(!conversation.exists());

    fs::create_dir(&conversation).unwrap();
    assert!(matches!(
        root.lock_legacy_conversation_store(&profile),
        Err(StoreError::NotFound)
    ));
    assert!(!locks.exists());

    let foreign = temp.path().join("foreign");
    let store = conversation.join(CONVERSATION_STORE_NAME);
    fs::write(&foreign, b"foreign").unwrap();
    symlink(&foreign, &store).unwrap();
    assert!(matches!(
        root.lock_legacy_conversation_store(&profile),
        Err(StoreError::Denied(_))
    ));
    fs::remove_file(&store).unwrap();
    fs::hard_link(&foreign, &store).unwrap();
    assert!(matches!(
        root.lock_legacy_conversation_store(&profile),
        Err(StoreError::Denied(_))
    ));
    assert!(!locks.exists());
    assert_eq!(fs::read(&foreign).unwrap(), b"foreign");
}

#[test]
fn conversation_transaction_uses_the_original_lock_and_replaces_only_the_original_store() {
    let (_temp, root, profile, provider) = fixture();
    let conversation = root.path().join(CONVERSATION_PROFILE_PATH);
    let other = conversation.with_file_name("other");
    for (directory, bytes) in [
        (&provider, b"provider".as_slice()),
        (&other, b"unrelated".as_slice()),
    ] {
        fs::create_dir_all(directory).unwrap();
        fs::write(directory.join(CONVERSATION_STORE_NAME), bytes).unwrap();
    }
    let store = conversation.join(CONVERSATION_STORE_NAME);
    let lock = conversation.join(CONVERSATION_LOCK_PATH);
    fs::create_dir_all(lock.parent().unwrap()).unwrap();
    fs::set_permissions(&conversation, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(&store, b"prior").unwrap();
    let legacy = br#"{"acquired_at":0.0,"owner":"pid:999","pid":999,"stale_after_seconds":0}"#;
    fs::write(&lock, legacy).unwrap();
    assert_busy(root.lock_legacy_conversation_store(&profile));
    assert_eq!(fs::read(&lock).unwrap(), legacy);
    fs::remove_file(&lock).unwrap();

    let transaction = root.lock_legacy_conversation_store(&profile).unwrap();
    let prior = transaction.read(MAX).unwrap();
    assert_eq!(prior.as_deref(), Some(b"prior".as_slice()));
    assert_busy(root.lock_legacy_conversation_store(&profile));
    assert_eq!(
        fs::metadata(&lock).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert!(matches!(
        transaction.publish(prior.as_deref(), b"denied", MAX, || {
            assert_eq!(fs::read(&store).unwrap(), b"prior");
            assert!(lock.is_file());
            Err("lease expired")
        }),
        Err(LegacyRegistryPublishError::Guard("lease expired"))
    ));
    assert_eq!(fs::read(&store).unwrap(), b"prior");
    assert_eq!(fs::read_dir(&conversation).unwrap().count(), 2);
    assert_eq!(
        fs::metadata(&conversation).unwrap().permissions().mode() & 0o777,
        0o755
    );
    drop(transaction);
    assert!(!lock.exists());

    let transaction = root.lock_legacy_conversation_store(&profile).unwrap();
    assert!(matches!(
        transaction.publish(None, b"created", MAX, || -> Result<(), ()> {
            panic!("confirmation must not recreate a removed store")
        }),
        Err(LegacyRegistryPublishError::BeforePublish(
            StoreError::NotFound
        ))
    ));
    transaction
        .publish(prior.as_deref(), b"updated", MAX, || Ok::<_, ()>(()))
        .unwrap();
    assert_eq!(fs::read(&store).unwrap(), b"updated");
    assert_eq!(
        fs::metadata(&store).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        fs::metadata(&conversation).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        fs::read(provider.join(CONVERSATION_STORE_NAME)).unwrap(),
        b"provider"
    );
    assert_eq!(
        fs::read(other.join(CONVERSATION_STORE_NAME)).unwrap(),
        b"unrelated"
    );
    drop(transaction);
    assert!(!lock.exists());
    assert_eq!(fs::read_dir(&conversation).unwrap().count(), 2);
}

#[test]
fn model_registry_reads_only_the_fixed_pack_filename_and_requested_profile() {
    let (_temp, root, profile, provider) = fixture();
    let registry = root.path().join(MODEL_PROFILE_PATH);
    let other = registry.with_file_name("other");
    for directory in [&provider, &registry, &other] {
        fs::create_dir_all(directory).unwrap();
    }
    fs::write(provider.join(STORE_NAME), b"provider").unwrap();
    fs::write(provider.join(MODEL_STORE_NAME), b"wrong pack").unwrap();
    fs::write(registry.join(STORE_NAME), b"wrong filename").unwrap();
    fs::write(other.join(MODEL_STORE_NAME), b"wrong profile").unwrap();
    assert_eq!(
        root.read_legacy_model_registry(&profile, MAX).unwrap(),
        None
    );

    fs::write(registry.join(MODEL_STORE_NAME), b"model").unwrap();
    assert_eq!(
        root.read_legacy_model_registry(&profile, 5)
            .unwrap()
            .as_deref(),
        Some(b"model".as_slice()),
    );
    assert!(matches!(
        root.read_legacy_model_registry(&profile, 4),
        Err(StoreError::Quota)
    ));
    assert!(!registry.join("locks").exists());
    assert_eq!(
        root.read_legacy_provider_registry(&profile, MAX)
            .unwrap()
            .as_deref(),
        Some(b"provider".as_slice()),
    );
}

#[test]
fn model_registry_rejects_symlinks_writable_directories_and_multilink_stores() {
    let (temp, root, profile, _) = fixture();
    let registry = root.path().join(MODEL_PROFILE_PATH);
    let profiles = registry.parent().unwrap();
    let outside = temp.path().join("outside");
    fs::create_dir(&outside).unwrap();
    fs::create_dir_all(profiles).unwrap();
    symlink(&outside, &registry).unwrap();
    assert!(root.read_legacy_model_registry(&profile, MAX).is_err());
    assert_eq!(fs::read_dir(&outside).unwrap().count(), 0);
    fs::remove_file(&registry).unwrap();

    fs::create_dir(&registry).unwrap();
    fs::set_permissions(profiles, fs::Permissions::from_mode(0o775)).unwrap();
    assert!(matches!(
        root.read_legacy_model_registry(&profile, MAX),
        Err(StoreError::Denied(_))
    ));
    fs::set_permissions(profiles, fs::Permissions::from_mode(0o755)).unwrap();

    let outside_file = outside.join(MODEL_STORE_NAME);
    let store = registry.join(MODEL_STORE_NAME);
    fs::write(&outside_file, b"foreign").unwrap();
    symlink(&outside_file, &store).unwrap();
    assert!(matches!(
        root.read_legacy_model_registry(&profile, MAX),
        Err(StoreError::Denied(_))
    ));
    fs::remove_file(&store).unwrap();
    fs::hard_link(&outside_file, &store).unwrap();
    assert!(matches!(
        root.read_legacy_model_registry(&profile, MAX),
        Err(StoreError::Denied(_))
    ));
    assert_eq!(fs::read(outside_file).unwrap(), b"foreign");
}

#[test]
fn model_registry_transaction_uses_only_the_original_model_lock_and_store() {
    let (_temp, root, profile, provider) = fixture();
    let registry = root.path().join(MODEL_PROFILE_PATH);
    let other = registry.with_file_name("other");
    fs::create_dir_all(&provider).unwrap();
    fs::write(provider.join(STORE_NAME), b"provider").unwrap();
    fs::create_dir_all(&other).unwrap();
    fs::write(other.join(MODEL_STORE_NAME), b"other profile").unwrap();

    let transaction = root.lock_legacy_model_registry(&profile).unwrap();
    let lock_path = registry.join(MODEL_LOCK_PATH);
    let lock: serde_json::Value = serde_json::from_slice(&fs::read(&lock_path).unwrap()).unwrap();
    assert_eq!(lock.as_object().unwrap().len(), 4);
    assert_ne!(lock["owner"].as_str().unwrap(), "");
    assert_eq!(lock["pid"], std::process::id());
    assert!(lock["acquired_at"].as_f64().unwrap() > 0.0);
    assert!(lock["stale_after_seconds"].is_null());
    assert_eq!(
        fs::metadata(&lock_path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_busy(root.lock_legacy_model_registry(&profile));
    assert_eq!(transaction.read(MAX).unwrap(), None);
    transaction
        .publish(None, b"model", MAX, || {
            assert!(lock_path.is_file());
            assert!(!registry.join(MODEL_STORE_NAME).exists());
            assert!(!provider.join("locks").exists());
            Ok::<_, ()>(())
        })
        .unwrap();

    let store = registry.join(MODEL_STORE_NAME);
    assert_eq!(fs::read(&store).unwrap(), b"model");
    assert_eq!(
        fs::metadata(&store).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(fs::read_dir(&registry).unwrap().count(), 2);
    assert_eq!(fs::read(provider.join(STORE_NAME)).unwrap(), b"provider");
    assert_eq!(
        fs::read(other.join(MODEL_STORE_NAME)).unwrap(),
        b"other profile"
    );
    drop(transaction);
    assert!(!lock_path.exists());
    assert!(!registry.join(LOCK_PATH).exists());

    let legacy = br#"{"acquired_at":0.0,"owner":"pid:999","pid":999,"stale_after_seconds":0}"#;
    fs::write(&lock_path, legacy).unwrap();
    assert_busy(root.lock_legacy_model_registry(&profile));
    assert_eq!(fs::read(&lock_path).unwrap(), legacy);
    assert_eq!(fs::read(store).unwrap(), b"model");
}

#[test]
fn model_registry_transaction_checks_prior_and_confirms_its_own_uncertain_replacement() {
    let (_temp, root, profile, _) = fixture();
    let registry = root.path().join(MODEL_PROFILE_PATH);
    let store = registry.join(MODEL_STORE_NAME);
    let transaction = root.lock_legacy_model_registry(&profile).unwrap();
    fs::write(&store, b"prior").unwrap();
    assert!(matches!(transaction.read(4), Err(StoreError::Quota)));
    assert!(matches!(
        transaction.publish(Some(b"stale"), b"next", MAX, || -> Result<(), ()> {
            panic!("stale data must fail before the authority check")
        }),
        Err(LegacyRegistryPublishError::BeforePublish(
            StoreError::Conflict(_)
        ))
    ));
    assert_eq!(fs::read(&store).unwrap(), b"prior");
    assert_eq!(fs::read_dir(&registry).unwrap().count(), 2);

    let result = transaction.inner.publish(
        Some(b"prior"),
        b"next",
        MAX,
        || Ok::<_, ()>(()),
        |_| Err(std::io::Error::other("injected directory sync failure")),
    );
    assert!(matches!(
        result,
        Err(LegacyRegistryPublishError::Uncertain(_))
    ));
    assert!(transaction.confirm_exact(b"next", MAX).unwrap());
    assert!(!transaction.confirm_exact(b"next", 3).unwrap());
    assert!(!transaction.confirm_exact(b"different", MAX).unwrap());
    fs::remove_file(&store).unwrap();
    fs::write(&store, b"next").unwrap();
    assert!(!transaction.confirm_exact(b"next", MAX).unwrap());
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
