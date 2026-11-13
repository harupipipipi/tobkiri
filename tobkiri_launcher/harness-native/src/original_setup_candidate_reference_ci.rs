//! Direct Host selector observations on separately seeded original fixtures.
//! The actual catalog, getter, denial factory, and Authority are outside this
//! interval. The native test consumes inputs only; Python compares outcomes.
use super::{
    reference_ci::{SelectedFixture, filesystem, lock_free, original_error},
    *,
};
use anyhow::{Context, ensure};
use serde_json::{Value, json};
use std::{collections::BTreeSet, fs::File};
use tobkiri_core::RuntimeOwner;
use tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES;

const ORIGINAL: &str = "1cf025f1eeab31a3daa2861b1f26a025c6954b6e";
const CASES: [&str; 7] = [
    "no-pointer",
    "other-profile",
    "unregistered-profile",
    "dangling-pointer",
    "missing-snapshot",
    "other-profile-snapshot-mismatch",
    "registered-profile",
];

fn recovery_name(prefix: Prefix) -> &'static str {
    match prefix {
        Prefix::NoJournal => "no-journal",
        Prefix::NoIntent => "no-intent",
        Prefix::RecoveredNone => "recovered-none",
        Prefix::BootstrapProfileIdNotBound => "held-publication",
    }
}

fn selection_observation(selection: &Result<CandidateSelection>) -> Value {
    match selection {
        Ok(CandidateSelection::BundledCatalog) => json!({"outcome":"bundled-catalog"}),
        Ok(CandidateSelection::Denied(message)) => {
            json!({"outcome":"denial-request","message":message})
        }
        Ok(CandidateSelection::ActiveAuthority) => {
            json!({"outcome":"integration-boundary","stage":"active-authority-entry"})
        }
        Err(error) => original_error(error),
    }
}

fn observe_case(
    source: SelectedFixture,
    owner_path: &Path,
    case: &str,
    additions: bool,
    profile_id: &str,
) -> anyhow::Result<(Value, bool)> {
    let before = filesystem(&source.root)?;
    let owner = RuntimeOwner::open(owner_path)?;
    let invocation = owner.begin_native_setup()?;
    let deadline = invocation.deadline()?;
    let engine = tobkiri_wasm::WasmEngine::new_with_control(invocation.control())?;
    let store = engine.native_setup_store(invocation.clone(), ())?;
    let cancellation = store.cancellation().clone();
    let budget = store.allocation_owners().graph().clone();
    let mut read = PublicationRead::new(&invocation)?;
    let initial_fuel = store.fuel_remaining()?;
    let mut discard_calls = Vec::new();
    let mut recoveries = Vec::new();
    let mut entry_error = None;
    let mut selection = None;
    let mut final_fuel = None;
    let mut same_deadline = store.original_deadline() == deadline;
    let execution: anyhow::Result<()> = (|| {
        for discard in [true, false] {
            discard_calls.push(discard);
            match read.enter_recovery(&source, &cancellation, discard) {
                Ok(prefix) => recoveries.push(recovery_name(prefix)),
                Err(error) => {
                    entry_error = Some(original_error(&error));
                    anyhow::bail!("native recovery stopped before candidate selection");
                }
            }
        }
        ensure!(
            read.check_continuation_thread().is_err(),
            "journal guard survived recovery"
        );
        if let Err(error) = read.acquire_candidate_root(&source, &cancellation) {
            entry_error = Some(original_error(&error));
            anyhow::bail!("native candidate root capture failed");
        }
        selection =
            Some(read.candidate_selection(&source, &cancellation, profile_id, additions, &budget));
        final_fuel = Some(store.fuel_remaining()?);
        same_deadline &=
            store.original_deadline() == deadline && invocation.deadline()? == deadline;
        Ok(())
    })();
    // Retain the actual candidate and raw diagnostics through Store retirement.
    // There is no child NativeDataIo because this driver only enters Host code.
    let observed = selection.as_ref().map(selection_observation);
    store.retire();
    drop(store);
    let close = read.close_continuation();
    let close_succeeded = close.is_ok();
    let close_error = close.as_ref().err().map(ToString::to_string);
    let receipt = read.receipt();
    let uncertain = read.uncertain_cleanup();
    let retained_cleanup = uncertain.is_some();
    if !close_succeeded || !receipt.verified() || retained_cleanup {
        // Keep the bounded failure row and skip the physical reopen when the
        // real close receipt is uncertain.
        return Ok((
            json!({
                "case": case, "include_source_additions": additions, "profile_id": profile_id,
                "selection": observed, "entry_error": entry_error,
                "execution_error": execution.as_ref().err().map(ToString::to_string),
                "close_succeeded": close_succeeded, "close_error": close_error,
                "close_receipt": {"open_files":receipt.open_files,"open_connections":receipt.open_connections,"uncertain":receipt.uncertain},
                "retained_cleanup": retained_cleanup,
            }),
            false,
        ));
    }
    drop((
        selection,
        close,
        uncertain,
        read,
        budget,
        cancellation,
        engine,
    ));
    tobkiri_wasm::reap_allocation_owners()?;
    let capacity = invocation.capacity_snapshot()?;
    let after = filesystem(&source.root)?;
    let definition_lock = lock_free(&source.root, ".profile-definition.lock")?;
    let publication_lock = lock_free(&source.root, ".bootstrap-publication.lock")?;
    let active_lock = lock_free(&source.root, ".active-profile.lock")?;
    source.retire();
    drop((source, invocation, owner));
    let reopened = RuntimeOwner::open(owner_path);
    let owner_reopened = reopened.is_ok();
    let reopen_error = reopened.as_ref().err().map(ToString::to_string);
    drop(reopened);

    let cleanup_ok = capacity.charged_bytes == 0
        && capacity.reservations == 0
        && capacity.limit_bytes == NATIVE_SETUP_MEMORY_BYTES
        && capacity.peak_charged_bytes <= NATIVE_SETUP_MEMORY_BYTES
        && owner_reopened;
    Ok((
        json!({
            "case": case, "include_source_additions": additions, "profile_id": profile_id,
            "selection": observed, "entry_error": entry_error,
            "execution_error": execution.as_ref().err().map(ToString::to_string),
            "discard_superseded_calls": discard_calls, "recovery_results": recoveries,
            "user_data_before": before, "user_data_after": after,
            "definition_lock_free_after": definition_lock, "publication_lock_free_after": publication_lock,
            "active_lock_free_after": active_lock,
            "close_succeeded": close_succeeded, "close_error": close_error,
            "close_receipt": {"open_files":receipt.open_files,"open_connections":receipt.open_connections,"uncertain":receipt.uncertain},
            "retained_cleanup": retained_cleanup,
            "capacity": {"charged_bytes":capacity.charged_bytes,"reservations":capacity.reservations,
                "peak_bytes":capacity.peak_charged_bytes,"limit_bytes":capacity.limit_bytes},
            "same_original_deadline": same_deadline, "initial_fuel": initial_fuel, "final_fuel": final_fuel,
            "physical_owner_reopened": owner_reopened, "owner_reopen_error": reopen_error,
        }),
        cleanup_ok,
    ))
}

fn write_observations(input: &Path, rows: &[Value]) -> anyhow::Result<()> {
    let output = serde_json::to_vec_pretty(&json!({
        "format":"io.tobkiri.original-setup-candidate-selection-native-observations.v1",
        "original_head":ORIGINAL,"case_count":rows.len(),"cases":rows,
        "scope":"direct Host selector with one real Store; no native catalog, getter, Guest factory, Authority, or AppHandle",
    }))?;
    ensure!(output.len() <= 2 * 1024 * 1024);
    fs::write(input.with_file_name("native-observations.json"), output)?;
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires fourteen freshly seeded pinned Setup candidate inputs"]
async fn original_setup_candidate_reference_driver() -> anyhow::Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_CI").as_deref() == Ok("1")
    );
    let input = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_SETUP_CANDIDATE_INPUT")
            .context("fresh original Setup candidate input required")?,
    );
    ensure!(input.is_absolute() && input.canonicalize()? == input);
    let mut bytes = Vec::new();
    File::open(&input)?
        .take(16 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= 16 * 1024);
    let manifest: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        manifest.as_object().is_some_and(|object| object.len() == 3)
            && manifest["format"]
                == "io.tobkiri.original-setup-candidate-selection-native-input.v1"
            && manifest["original_head"] == ORIGINAL
    );
    let cases = manifest["cases"]
        .as_array()
        .context("candidate cases required")?;
    ensure!(cases.len() == CASES.len() * 2);
    let mut seen = BTreeSet::new();
    let root = input
        .parent()
        .context("native fixture directory required")?;
    let storage = tempfile::tempdir()?;
    let physical_storage = storage.path().canonicalize()?;
    let mut observations = Vec::new();
    for (index, case) in cases.iter().enumerate() {
        let name = case["case"]
            .as_str()
            .context("candidate case name required")?;
        let additions = case["include_source_additions"]
            .as_bool()
            .context("candidate additions flag required")?;
        let profile_id = case["profile_id"]
            .as_str()
            .context("candidate Profile ID required")?;
        ensure!(
            case.as_object().is_some_and(|object| object.len() == 3)
                && CASES.contains(&name)
                && seen.insert((name, additions))
                && profile_id == "defaults"
        );
        let selected = root
            .join(format!("{name}-{}", u8::from(additions)))
            .join("native/user-data");
        let source = SelectedFixture::bind(selected)?;
        let owner_path = physical_storage.join(format!("candidate-owner-{index}"));
        eprintln!("CI_CANDIDATE_START case={name} additions={additions}");
        let (row, cleanup_ok) = observe_case(source, &owner_path, name, additions, profile_id)?;
        observations.push(row);
        write_observations(&input, &observations)?;
        ensure!(
            cleanup_ok,
            "native candidate cleanup is unverified for {name} additions={additions}"
        );
    }
    Ok(())
}
