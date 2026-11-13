//! Independent pinned-original comparison of the direct native definitions prefix.
//! Original constructor/list ran through full recovery; this driver calls the
//! native definitions interval under a real authenticated journal guard. The
//! selected root is a controlled CI fixture, not an AppHandle, Guest factory,
//! full catalog, migration, original exception reference or Authority grant.
use super::*;
use anyhow::{Context, ensure};
use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};
use std::{
    collections::BTreeSet,
    fs::File,
    os::{fd::AsRawFd, unix::fs::MetadataExt},
    sync::atomic::{AtomicBool, Ordering},
};
use tobkiri_core::RuntimeOwner;
use tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES;

const CASES: [&str; 13] = [
    "index-absent",
    "legacy-file",
    "existing-profile",
    "legacy-bootstrap-missing",
    "invalid-json",
    "invalid-utf8",
    "duplicate-key",
    "wrong-digest",
    "wrong-schema",
    "generation-bool",
    "duplicate-profile-id",
    "index-symlink",
    "index-hardlink",
];
const ORIGINAL: &str = "1cf025f1eeab31a3daa2861b1f26a025c6954b6e";

pub(super) struct SelectedFixture {
    pub(super) root: PathBuf,
    directory: File,
    initially_absent: bool,
    process: u32,
    live: AtomicBool,
}
impl SelectedFixture {
    pub(super) fn bind(root: PathBuf) -> anyhow::Result<Self> {
        let initially_absent = match fs::symlink_metadata(&root) {
            Ok(named) => {
                ensure!(named.is_dir(), "selected fixture root must be a directory");
                false
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => true,
            Err(error) => return Err(error.into()),
        };
        let held = if initially_absent {
            root.parent()
                .context("absent fixture needs an existing parent")?
        } else {
            &root
        };
        ensure!(
            held.canonicalize()? == held,
            "fixture binding is not canonical"
        );
        let directory = File::open(held)?;
        let source = Self {
            root,
            directory,
            initially_absent,
            process: std::process::id(),
            live: AtomicBool::new(true),
        };
        source.check_original_owner()?;
        Ok(source)
    }

    pub(super) fn retire(&self) {
        self.live.store(false, Ordering::Release);
    }
}
impl OriginalAuthorityOwner for SelectedFixture {
    fn with_original_user_data<R>(&self, use_root: impl FnOnce(&Path) -> R) -> Result<R> {
        self.check_original_owner()?;
        Ok(use_root(&self.root))
    }
    fn check_original_owner(&self) -> Result<()> {
        if !self.live.load(Ordering::Acquire) || self.process != std::process::id() {
            return Err(OriginalAuthorityError::unavailable());
        }
        let held = self
            .directory
            .metadata()
            .map_err(|_| OriginalAuthorityError::unavailable())?;
        let named_path = if self.initially_absent {
            let absent = fs::symlink_metadata(&self.root)
                .is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound);
            if !absent {
                return Err(OriginalAuthorityError::unavailable());
            }
            self.root.parent().expect("bound fixture parent")
        } else {
            &self.root
        };
        let named =
            fs::symlink_metadata(named_path).map_err(|_| OriginalAuthorityError::unavailable())?;
        if !named.is_dir() || (held.dev(), held.ino()) != (named.dev(), named.ino()) {
            return Err(OriginalAuthorityError::unavailable());
        }
        Ok(())
    }
}
impl crate::BootstrapReviewOwner for SelectedFixture {
    fn with_original_review_user_data<R>(&self, use_root: impl FnOnce(&Path) -> R) -> Result<R> {
        self.with_original_user_data(use_root)
    }
}

pub(super) fn lock_free(root: &Path, name: &str) -> anyhow::Result<Option<bool>> {
    let path = root.join("profiles").join(name);
    if !path.try_exists()? {
        return Ok(None);
    }
    let file = File::options().read(true).write(true).open(path)?;
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
        ensure!(unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) } == 0);
        return Ok(Some(true));
    }
    let errno = std::io::Error::last_os_error().raw_os_error();
    ensure!(errno == Some(libc::EWOULDBLOCK) || errno == Some(libc::EAGAIN));
    Ok(Some(false))
}

/// Independent observer, matching the original fixture's finite filesystem
/// projection. Key bytes/digests are deliberately excluded from this output.
pub(super) fn filesystem(root: &Path) -> anyhow::Result<Value> {
    if !root.try_exists()? {
        return Ok(json!({"exists":false,"files":[]}));
    }
    let mut pending = vec![root.to_path_buf()];
    let mut rows = Vec::new();
    while let Some(path) = pending.pop() {
        ensure!(rows.len() < 128, "fixture observation file bound");
        let meta = fs::symlink_metadata(&path)?;
        let kind = if meta.is_dir() {
            "directory"
        } else if meta.is_file() {
            "file"
        } else if meta.file_type().is_symlink() {
            "symlink"
        } else {
            "other"
        };
        let relative = if path == root {
            ".".to_owned()
        } else {
            path.strip_prefix(root)?
                .to_str()
                .context("UTF-8 fixture path")?
                .to_owned()
        };
        let mut row = json!({"path":relative,"kind":kind,"mode":meta.mode() & 0o7777});
        if kind == "symlink" {
            row["target"] = json!(
                fs::read_link(&path)?
                    .to_str()
                    .context("UTF-8 fixture target")?
            );
        } else if kind == "file" {
            ensure!(meta.len() <= 4 * 1024 * 1024);
            row["bytes"] = json!(meta.len());
            if !matches!(
                path.file_name().and_then(|v| v.to_str()),
                Some(".publication-key" | "original-key")
            ) {
                row["sha256"] = json!(format!("{:x}", Sha256::digest(fs::read(&path)?)));
            }
        } else if kind == "directory" {
            let mut children = fs::read_dir(&path)?
                .map(|entry| entry.map(|v| v.path()))
                .collect::<std::io::Result<Vec<_>>>()?;
            ensure!(children.len() <= 128);
            children.sort();
            pending.extend(children.into_iter().rev());
        }
        rows.push(row);
    }
    Ok(json!({"exists":true,"files":rows}))
}

#[derive(PartialEq, Eq)]
struct KeyIdentity {
    device: u64,
    inode: u64,
    links: u64,
    mode: u32,
    length: u64,
    digest: [u8; 32],
}
fn key_identity(root: &Path) -> anyhow::Result<KeyIdentity> {
    let path = root.join("profiles/.publication-key");
    let meta = fs::symlink_metadata(&path)?;
    ensure!(meta.is_file() && meta.len() <= 4096);
    // The closed fixture observer retains no raw signing key or logged hash.
    let bytes = zeroize::Zeroizing::new(fs::read(path)?);
    Ok(KeyIdentity {
        device: meta.dev(),
        inode: meta.ino(),
        links: meta.nlink(),
        mode: meta.mode(),
        length: meta.len(),
        digest: Sha256::digest(bytes.as_slice()).into(),
    })
}

#[derive(PartialEq, Eq)]
struct IndexIdentity {
    device: u64,
    inode: u64,
    links: u64,
    mode: u32,
    length: u64,
}

fn index_identity(root: &Path) -> anyhow::Result<Option<IndexIdentity>> {
    match fs::symlink_metadata(root.join("profiles/index.json")) {
        Ok(meta) => Ok(Some(IndexIdentity {
            device: meta.dev(),
            inode: meta.ino(),
            links: meta.nlink(),
            mode: meta.mode(),
            length: meta.len(),
        })),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

pub(super) fn original_error(error: &OriginalAuthorityError) -> Value {
    use crate::original_authority::tests;
    if let Some((stage, reason)) = tests::native_issue(error, 0) {
        return json!({"outcome":"native-integration-failure",
            "stage":tests::stage_name(stage),"reason":reason});
    }
    let mut graph = tests::graph(error, &mut 0);
    fn os_fields(value: &mut Value) {
        match value {
            Value::Object(fields) => {
                // Fixed synthetic OSErrors have null fields. Keep actual
                // errno/filename when the native carrier has a real stat error.
                if matches!(
                    fields.get("type").and_then(Value::as_str),
                    Some(
                        "tobkiri_protocol.secure_persistence.SecurePersistenceError"
                            | "builtins.FileNotFoundError"
                    )
                ) {
                    fields.entry("errno").or_insert(Value::Null);
                    fields.entry("filename").or_insert(Value::Null);
                }
                for field in fields.values_mut() {
                    os_fields(field);
                }
            }
            Value::Array(items) => {
                for item in items {
                    os_fields(item);
                }
            }
            _ => {}
        }
    }
    os_fields(&mut graph);
    json!({"outcome":"error","error":graph})
}

fn observation(outcome: &Result<DefinitionPrefix>) -> Value {
    match outcome {
        Ok(DefinitionPrefix::ImportLegacyNotBound) => {
            json!({"outcome":"integration-boundary","stage":"import_legacy_collection"})
        }
        Ok(DefinitionPrefix::MigrateLegacySuccessorsNotBound) => {
            json!({"outcome":"integration-boundary","stage":"migrate_legacy_successors"})
        }
        Err(error) => original_error(error),
    }
}

fn write_observations(manifest: &Path, observations: &[Value]) -> anyhow::Result<()> {
    let output = serde_json::to_vec_pretty(&json!({
        "format":"io.tobkiri.original-profile-definition-prefix-native-observations.v1",
        "original_head":ORIGINAL,"case_count":observations.len(),"cases":observations,
        "native_comparison":true,"qualification":false,"production_enabled":false,
        "scope":"controlled selected-root mechanics; direct native constructor/list versus original full-recovery interval; no actual AppHandle/Wasm catalog/Guest factory/migration/Authority",
        "error_projection":"typed OriginalAuthorityError; raw registry subclass preserved; outer catch request and actual Guest exception reference unqualified",
        "native_data_io_join":"not bound in this synchronous mechanics driver"
    }))?;
    ensure!(output.len() <= 2 * 1024 * 1024);
    fs::write(manifest.with_file_name("native-observations.json"), output)?;
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires thirteen fresh independently pinned original definitions fixtures"]
async fn original_profile_definitions_reference_driver() -> anyhow::Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_CI").as_deref() == Ok("1")
    );
    let manifest = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_PROFILE_DEFINITIONS_FIXTURES")
            .context("fresh original definitions summary required")?,
    );
    ensure!(manifest.is_absolute() && manifest.canonicalize()? == manifest);
    ensure!(fs::metadata(&manifest)?.len() <= 2 * 1024 * 1024);
    let bytes = fs::read(&manifest)?;
    ensure!(bytes.len() <= 2 * 1024 * 1024);
    let fixtures: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        fixtures["format"] == "io.tobkiri.original-profile-definition-prefix-reference.v1"
            && fixtures["original_head"] == ORIGINAL
            && fixtures["case_count"] == 13
            && fixtures["native_comparison"] == false
            && fixtures["qualification"] == false
            && fixtures["production_enabled"] == false
    );
    let cases = fixtures["cases"]
        .as_array()
        .context("closed original cases required")?;
    let names: BTreeSet<_> = cases.iter().map(|v| v["name"].as_str()).collect();
    ensure!(
        cases.len() == CASES.len()
            && names == CASES.iter().copied().map(Some).collect::<BTreeSet<_>>()
    );
    let storage = tempfile::tempdir()?;
    let storage = storage.path().canonicalize()?;
    let fixtures_root = manifest.parent().context("fixture directory required")?;
    let mut observations = Vec::new();
    let mut all_match = true;
    for (index, case) in cases.iter().enumerate() {
        let name = case["name"].as_str().context("closed case name required")?;
        let input_path = fixtures_root.join(name).join("native/input.json");
        ensure!(
            case["native_input"].as_str()
                == Some(input_path.to_str().context("UTF-8 fixture input")?)
        );
        ensure!(fs::metadata(&input_path)?.len() <= 16 * 1024);
        let input_bytes = fs::read(&input_path)?;
        ensure!(input_bytes.len() <= 16 * 1024);
        let input: Value = serde_json::from_slice(&input_bytes)?;
        let root = input_path
            .parent()
            .context("native fixture directory required")?
            .join("user-data");
        ensure!(
            input.as_object().is_some_and(|v| v.len() == 4)
                && input["case"] == name
                && input["resource_fixture_only"] == true
                && input["user_data"].as_str()
                    == Some(root.to_str().context("UTF-8 fixture root")?)
                && root.canonicalize()? == root
        );
        // The input's runtime is provenance DATA; no code/path is selected or
        // loaded from it by this direct, already-compiled native interval.
        ensure!(
            input["runtime"]
                .as_str()
                .is_some_and(|v| Path::new(v).is_absolute())
        );
        let source = SelectedFixture::bind(root)?;
        let before = filesystem(&source.root)?;
        ensure!(
            before == case["user_data_before"],
            "fresh native fixture differs: {name}"
        );
        let key_before = key_identity(&source.root)?;
        let index_before = index_identity(&source.root)?;
        let owner_path = storage.join(format!("owner-{index}"));
        let owner = RuntimeOwner::open(&owner_path)?;
        let invocation = owner.begin_native_setup()?;
        let deadline = invocation.deadline()?;
        let engine = tobkiri_wasm::WasmEngine::new_with_control(invocation.control())?;
        // Exactly one Store supplies the real graph budget for this operation.
        // No Guest/component is executed, no work meter or deadline is reset.
        let store = engine.native_setup_store(invocation.clone(), ())?;
        let cancellation = store.cancellation().clone();
        let graph_budget = store.allocation_owners().graph().clone();
        let mut read = PublicationRead::new(&invocation)?;
        let mut actual = None;
        let mut publication_error = None;
        let mut observed = None;
        let mut definition_free_during = None;
        let mut journal_free_during = None;
        let execution: anyhow::Result<()> = (|| {
            ensure!(
                store.original_deadline() == deadline && store.fuel_remaining()? == 2_000_000_000
            );
            match read.enter_recovery(&source, &cancellation, true) {
                Ok(super::super::Prefix::BootstrapProfileIdNotBound) => {}
                Ok(_) => anyhow::bail!("fixture did not enter held publication recovery"),
                Err(error) => {
                    publication_error = Some(error);
                    anyhow::bail!("publication failed before the definitions interval");
                }
            }
            ensure!(lock_free(&source.root, ".bootstrap-publication.lock")? == Some(false));
            eprintln!("CI_DEFINITIONS_CASE_START index={index} name={name}");
            actual = Some(run(&read, &source, &cancellation, &graph_budget));
            observed = actual.as_ref().map(observation);
            definition_free_during = lock_free(&source.root, ".profile-definition.lock")?;
            journal_free_during = lock_free(&source.root, ".bootstrap-publication.lock")?;
            ensure!(definition_free_during == Some(true));
            ensure!(journal_free_during == Some(false));
            // An owned registry RuntimeError subclass is still the raw source
            // type. No string match is promoted to a denied-factory request.
            if let Some(Err(error)) = &actual {
                ensure!(error.class != crate::bootstrap_review::DENIED);
            }
            ensure!(invocation.deadline()? == deadline && store.original_deadline() == deadline);
            ensure!(store.fuel_remaining()? == 2_000_000_000);
            Ok(())
        })();
        let before_finish = invocation.capacity_snapshot()?;
        // This driver has no native data-IO binding or child to Join. Retire
        // the actual Store first, while the separate Read still owns its guard.
        store.retire();
        drop(store);
        let after_store = invocation.capacity_snapshot()?;
        let journal_held_after_store =
            lock_free(&source.root, ".bootstrap-publication.lock")? == Some(false);
        // Read's prepaid diagnostic reservation outlives all owned errors.
        let entry_error = publication_error.as_ref().map(original_error);
        drop(actual);
        drop(publication_error);
        let closed = read.close_continuation();
        let close_ok = closed.is_ok();
        let close_error = closed.as_ref().err().map(ToString::to_string);
        let receipt = read.receipt();
        let uncertain = read.uncertain_cleanup();
        drop(closed);
        if !close_ok || !receipt.verified() || uncertain.is_some() {
            observations.push(json!({"name":name,"observation":observed,
                "publication_entry_error":entry_error,
                "execution_error":execution.as_ref().err().map(ToString::to_string),
                "close_succeeded":close_ok,"close_error":close_error,
                "close_receipt":{"open_files":receipt.open_files,"open_connections":receipt.open_connections,"uncertain":receipt.uncertain},
                "cleanup_verified":false,"retained_cleanup":uncertain.is_some(),
                "completed_join":false,"physical_owner_reopened":false,"matched":false}));
            write_observations(&manifest, &observations)?;
            anyhow::bail!("definitions close remains unverified for {name}");
        }
        let definition_free = lock_free(&source.root, ".profile-definition.lock")?;
        let journal_free = lock_free(&source.root, ".bootstrap-publication.lock")?;
        let after = filesystem(&source.root)?;
        let key_unchanged = key_identity(&source.root)? == key_before;
        let index_unchanged = index_identity(&source.root)? == index_before;
        drop(read);
        drop(uncertain);
        drop(graph_budget);
        drop(cancellation);
        drop(engine);
        let after_data = invocation.capacity_snapshot()?;
        let reaped = tobkiri_wasm::reap_allocation_owners()?;
        let after_reap = invocation.capacity_snapshot()?;
        let compared = observed
            .as_ref()
            .is_some_and(|value| *value == case["expected"]);
        let files_match = after == case["user_data_after"];
        let post_locks_match = json!(definition_free) == case["definition_lock_free_after"]
            && json!(journal_free) == case["journal_lock_free_after"];
        let reached_match = if case["expected"]["outcome"] == "integration-boundary" {
            case["reached"].as_array().is_some_and(|v| {
                v.len() == 1
                    && v[0]["method"] == case["expected"]["stage"]
                    && v[0]["definition_lock_free"] == true
                    && v[0]["journal_lock_free"] == false
            })
        } else {
            case["reached"].as_array().is_some_and(Vec::is_empty)
        };
        let cleanup_ok = close_ok
            && receipt.verified()
            && journal_free == Some(true)
            && definition_free == Some(true)
            && journal_held_after_store;
        let capacity_ok = after_reap.charged_bytes == 0
            && after_reap.reservations == 0
            && after_reap.limit_bytes == NATIVE_SETUP_MEMORY_BYTES
            && after_reap.peak_charged_bytes <= NATIVE_SETUP_MEMORY_BYTES;
        let mut matched = execution.is_ok()
            && compared
            && files_match
            && key_unchanged
            && index_unchanged
            && post_locks_match
            && reached_match
            && cleanup_ok
            && capacity_ok;
        source.retire();
        drop(source);
        drop(invocation);
        drop(owner);
        // Real exclusive physical-owner reopen, not a logical released flag.
        let reopened = RuntimeOwner::open(&owner_path);
        let owner_reopened = reopened.is_ok();
        let reopen_error = reopened.as_ref().err().map(ToString::to_string);
        drop(reopened);
        matched &= owner_reopened;
        all_match &= matched;
        observations.push(json!({"name":name,"observation":observed,
            "publication_entry_error":entry_error,"user_data_after":after,
            "expected_matched":compared,"filesystem_matched":files_match,
            "key_bytes_and_identity_unchanged":key_unchanged,"original_boundary_lock_contract":reached_match,
            "index_bytes_and_identity_unchanged":files_match && index_unchanged,
            "original_after_close_locks_matched":post_locks_match,
            "execution_matched_interval":execution.is_ok(),
            "execution_error":execution.as_ref().err().map(ToString::to_string),
            "definition_lock_free_before_finish":definition_free_during,
            "journal_lock_free_before_finish":journal_free_during,
            "journal_lock_held_after_store_retirement":journal_held_after_store,
            "journal_lock_free_after_close":journal_free,"definition_lock_free_after_close":definition_free,
            "close_succeeded":close_ok,"close_error":close_error,
            "close_receipt":{"open_files":receipt.open_files,"open_connections":receipt.open_connections,"uncertain":receipt.uncertain},
            "capacity":{"before_finish":{"bytes":before_finish.charged_bytes,"reservations":before_finish.reservations},
                "after_store_retirement":{"bytes":after_store.charged_bytes,"reservations":after_store.reservations},
                "after_data_release":{"bytes":after_data.charged_bytes,"reservations":after_data.reservations},
                "reaped":reaped,"after_reap":{"bytes":after_reap.charged_bytes,"reservations":after_reap.reservations},
                "peak_bytes":after_reap.peak_charged_bytes,"limit_bytes":after_reap.limit_bytes},
            "single_store":true,"budget_resets":0,"host_work_limit":2_000_000_000_u64,
            "same_original_30s_deadline":execution.is_ok(),"guest_fuel_used":0,
            "physical_owner_reopened":owner_reopened,"owner_reopen_error":reopen_error,"matched":matched}));
        write_observations(&manifest, &observations)?;
        // Do not continue or turn an uncertain close into a completed Join.
        ensure!(
            cleanup_ok && capacity_ok && owner_reopened,
            "definitions cleanup/capacity unverified for {name}"
        );
        eprintln!(
            "CI_DEFINITIONS_CASE_CLOSED index={index} matched={matched} capacity=0 reservations=0"
        );
    }
    ensure!(
        all_match,
        "definitions mismatch; inspect native-observations.json"
    );
    Ok(())
}
