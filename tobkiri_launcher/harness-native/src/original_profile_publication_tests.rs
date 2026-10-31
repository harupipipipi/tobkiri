//! Fresh CI comparison of the real native publication prefix, not full Setup.
//! The original application's denied factory is a finite fixture binding here;
//! this does not prove that the product factory or original GUI is connected.
use super::*;
use crate::BootstrapReviewOwner;
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs::File,
    path::PathBuf,
    sync::atomic::{AtomicBool, Ordering},
};
use tobkiri_core::RuntimeOwner;

struct Source {
    root: PathBuf,
    // The selected parent is real even when the original user-data root does
    // not exist. Opening this descriptor never initializes that root.
    _selected_parent: File,
    live: AtomicBool,
}
impl OriginalAuthorityOwner for Source {
    fn with_original_user_data<R>(&self, use_root: impl FnOnce(&Path) -> R) -> Result<R> {
        self.check_original_owner()?;
        Ok(use_root(&self.root))
    }
    fn check_original_owner(&self) -> Result<()> {
        if self.live.load(Ordering::Acquire) {
            Ok(())
        } else {
            Err(OriginalAuthorityError::unavailable())
        }
    }
}
impl BootstrapReviewOwner for Source {
    fn with_original_review_user_data<R>(&self, use_root: impl FnOnce(&Path) -> R) -> Result<R> {
        self.check_original_owner()?;
        Ok(use_root(&self.root))
    }
}

fn bind_error(value: &mut Value, root: &str, denial_class: &str) {
    match value {
        Value::String(text) => {
            *text = text.replace(root, "$USER_DATA");
        }
        Value::Array(values) => {
            for value in values {
                bind_error(value, root, denial_class);
            }
        }
        Value::Object(values) => {
            if values.get("type").and_then(Value::as_str) == Some(crate::bootstrap_review::DENIED) {
                values.insert("type".into(), Value::String(denial_class.into()));
            }
            // The source-defined persistence exception subclasses OSError with
            // no errno or filename. This projects that fixed source type; it
            // does not turn a native-only condition into a source exception.
            if values.get("type").and_then(Value::as_str)
                == Some("tobkiri_protocol.secure_persistence.SecurePersistenceError")
            {
                values.entry("errno").or_insert(Value::Null);
                values.entry("filename").or_insert(Value::Null);
            }
            for value in values.values_mut() {
                bind_error(value, root, denial_class);
            }
        }
        _ => {}
    }
}

#[cfg(unix)]
fn verify_os_lock_released(root: &Path) -> anyhow::Result<()> {
    use std::os::fd::AsRawFd;
    let path = root.join("profiles/.bootstrap-publication.lock");
    if !path.is_file() {
        return Ok(());
    }
    // Open only the existing fixture lock. A separate descriptor must obtain
    // the real nonblocking OS lock after the product guard has closed; a
    // logical close counter or unrelated Core-owner reopen cannot prove this.
    let file = File::options().read(true).write(true).open(path)?;
    let locked = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    anyhow::ensure!(
        locked == 0,
        "actual publication OS lock survived operation close"
    );
    let unlocked = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) };
    anyhow::ensure!(unlocked == 0, "fixture OS lock could not be released");
    Ok(())
}

/// Never succeeds without this execution's fresh independent source fixtures.
#[test]
#[ignore = "requires fresh independent original publication fixtures"]
fn original_reference_driver() -> anyhow::Result<()> {
    let manifest = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_PUBLICATION_FIXTURES")
            .ok_or_else(|| anyhow::anyhow!("fresh original publication fixtures required"))?,
    );
    let bytes = std::fs::read(&manifest)?;
    anyhow::ensure!(bytes.len() <= 2 * 1024 * 1024);
    let fixtures: Value = serde_json::from_slice(&bytes)?;
    anyhow::ensure!(fixtures["format"] == "io.tobkiri.original-publication-prelude-fixtures.v1");
    anyhow::ensure!(fixtures["original_head"] == "1cf025f1eeab31a3daa2861b1f26a025c6954b6e");
    anyhow::ensure!(fixtures["python"] == "3.13.16");
    anyhow::ensure!(fixtures["reference_checks_passed"] == true);
    let denial_class = fixtures["application_denial_class"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("independent original factory binding required"))?;
    anyhow::ensure!(
        denial_class.len() <= 256 && denial_class.ends_with(".ProfileResolutionDenied")
    );
    let cases = fixtures["cases"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("original publication cases required"))?;
    anyhow::ensure!(cases.len() == 48);
    let unique = cases
        .iter()
        .map(|case| case["name"].as_str())
        .collect::<BTreeSet<_>>();
    anyhow::ensure!(unique.len() == 48 && !unique.contains(&None));
    let storage = tempfile::tempdir()?;
    let storage = storage.path().canonicalize()?;
    let mut observations = Vec::new();
    let mut all_match = true;
    for (index, case) in cases.iter().enumerate() {
        let root = PathBuf::from(
            case["user_data_dir"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("actual fixture source required"))?,
        );
        anyhow::ensure!(
            root.is_absolute() && root.file_name().is_some_and(|name| name == "native")
        );
        let source = Source {
            _selected_parent: File::open(
                root.parent()
                    .ok_or_else(|| anyhow::anyhow!("source parent required"))?,
            )?,
            root,
            live: AtomicBool::new(true),
        };
        let owner_path = storage.join(format!("owner-{index}"));
        let owner = RuntimeOwner::open(&owner_path)?;
        let invocation = owner.begin_native_setup()?;
        let cancellation = CoreCancellation::new_with_control(invocation.control())?;
        let mut read = PublicationRead::new(&invocation)?;
        if case["after_lock_remove_intent"].as_bool() == Some(true) {
            read.remove_intent_after_lock_for_fixture();
        }
        let mut actual = read.run(&source, &cancellation, true);
        if matches!(
            actual,
            Ok(PublicationPrefix::NoJournal | PublicationPrefix::NoIntent)
        ) {
            actual = read.run(&source, &cancellation, false);
        }
        let observed = match &actual {
            Ok(PublicationPrefix::NoJournal | PublicationPrefix::NoIntent) => {
                json!({"outcome":"integration-boundary", "stage":"catalog-not-bound"})
            }
            Ok(PublicationPrefix::VerifiedIntent) => {
                json!({"outcome":"integration-boundary", "stage":"verified-pointer-not-bound"})
            }
            Err(error) => match tests::native_issue(error, 0) {
                Some((stage, reason)) => json!({"outcome":"native-integration-failure",
                    "stage":tests::stage_name(stage), "reason":reason}),
                None => {
                    let mut graph = tests::graph(error, &mut 0);
                    bind_error(&mut graph, &source.root.to_string_lossy(), denial_class);
                    json!({"outcome":"error", "error":graph})
                }
            },
        };
        all_match &= observed == case["expected"];
        let mut row = observed;
        row.as_object_mut()
            .expect("finite observation")
            .insert("name".into(), case["name"].clone());
        observations.push(row);
        let output = serde_json::to_vec_pretty(&json!({
            "format":"io.tobkiri.original-publication-prelude-native-observations.v1",
            "native_comparison":true,"qualification":false,"production_enabled":false,
            "application_factory_binding":"finite fixture only; product factory unqualified",
            "cases":observations,
        }))?;
        anyhow::ensure!(output.len() <= 2 * 1024 * 1024);
        std::fs::write(manifest.with_file_name("native-observations.json"), output)?;
        anyhow::ensure!(
            read.receipt().verified(),
            "actual publication IO close was not verified"
        );
        anyhow::ensure!(
            read.uncertain_cleanup().is_none(),
            "uncertain publication IO was retained"
        );
        // Owned diagnostics must die before their prepaid Read reservations.
        drop(actual);
        drop(read);
        #[cfg(unix)]
        verify_os_lock_released(&source.root)?;
        let capacity = invocation.capacity_snapshot()?;
        anyhow::ensure!(
            capacity.charged_bytes == 0 && capacity.reservations == 0,
            "publication allocations survived the operation"
        );
        drop(source);
        drop(cancellation);
        drop(invocation);
        drop(owner);
        drop(RuntimeOwner::open(owner_path)?);
    }
    anyhow::ensure!(
        all_match,
        "publication mismatch; inspect native-observations.json"
    );
    Ok(())
}
