//! The fixed Host registry snapshot shared by recovery's list and Setup lookup.
use super::*;
use tobkiri_canon::AllocationBudget;

#[cfg(all(test, unix))]
#[path = "original_setup_candidate_selection_tests.rs"]
mod candidate_selection_tests;
#[cfg(unix)]
#[path = "original_profile_definitions_model.rs"]
mod definitions_model;
#[cfg(all(test, unix))]
#[path = "original_profile_definitions_reference_ci.rs"]
mod reference_ci;
#[cfg(all(test, unix))]
#[path = "original_profile_definitions_tests.rs"]
mod tests;

const INDEX: &str = "index.json";
const INTEGRITY: &str =
    "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreIntegrityError";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DefinitionPrefix {
    ImportLegacyNotBound,
    MigrateLegacySuccessorsNotBound,
}

/// Borrow Read's existing invocation, close ledger and prepaid diagnostics.
/// The caller drops owned errors before Read; no new account/source is owned.
pub(crate) fn run<S: crate::BootstrapReviewOwner>(
    read: &PublicationRead,
    owner: &S,
    cancellation: &CoreCancellation,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<DefinitionPrefix> {
    let op = Operation {
        owner,
        invocation: &read.invocation,
        cancellation,
        deadline: read
            .invocation
            .deadline()
            .map_err(|_| OriginalAuthorityError::unavailable())?,
        pid: std::process::id(),
        cleanup: read.cleanup.clone(),
        #[cfg(test)]
        fixture: None,
    };
    op.check(OriginalAuthorityStage::Source)?;
    owner
        .with_original_review_user_data(|root| prefix(&op, root, budget))?
        .map_err(recovery_error)
}

fn integrity(message: impl Into<String>) -> OriginalAuthorityError {
    raw(INTEGRITY, message)
}
fn unreadable(error: OriginalAuthorityError) -> OriginalAuthorityError {
    if error.class == RAW_IO
        || matches!(
            error.class,
            "builtins.ValueError"
                | "tobkiri_protocol.errors.CanonicalizationError"
                | "tobkiri_protocol.secure_persistence.SecurePersistenceError"
        )
    {
        caused(integrity("Profile definition store is unreadable"), error)
    } else {
        error
    }
}
fn missing(error: &OriginalAuthorityError) -> bool {
    error.class == "builtins.FileNotFoundError"
        || (error.class == RAW_IO
            && matches!(&error.cause, Some(OriginalAuthorityCause::Io(io)) if io.kind == std::io::ErrorKind::NotFound))
}

#[cfg(unix)]
fn prefix<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    requested: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<DefinitionPrefix> {
    let store = Store::new(op, requested)?;
    let mut state = store.snapshot(op, budget)?;
    let listed = definitions_model::list(op, &mut state, budget)?;
    let empty = listed.is_empty();
    drop(listed);
    drop(state);
    // Original short circuit: a nonempty list does not inspect this path.
    if empty {
        op.check(OriginalAuthorityStage::Source)?;
        if store
            .user_data
            .join("settings/startup_profiles.json")
            .is_file()
        {
            return Ok(DefinitionPrefix::ImportLegacyNotBound);
        }
    }
    Ok(DefinitionPrefix::MigrateLegacySuccessorsNotBound)
}

#[cfg(unix)]
pub(super) struct RegisteredProfile {
    _state: definitions_model::State,
    _profile: definitions_model::StoredProfile,
}

#[cfg(unix)]
pub(super) fn get_profile<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    profile_id: &Text,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<RegisteredProfile>> {
    let store = Store::new(op, root)?;
    definitions_model::canonical_id(profile_id, op, budget)?;
    let mut state = store.snapshot(op, budget)?;
    Ok(
        definitions_model::get(op, &mut state, profile_id, budget)?.map(|profile| {
            RegisteredProfile {
                _state: state,
                _profile: profile,
            }
        }),
    )
}

#[cfg(unix)]
struct Store {
    user_data: PathBuf,
    directory: Directory,
    _paths: NativeSetupReservation,
}

#[cfg(unix)]
impl Store {
    fn new<S: OriginalAuthorityOwner>(op: &Operation<'_, S>, requested: &Path) -> Result<Self> {
        let paths = op.reserve(
            OriginalAuthorityStage::Path,
            requested
                .as_os_str()
                .len()
                .saturating_mul(16)
                .saturating_add(16384),
        )?;
        if requested.is_symlink() {
            return Err(integrity("Profile store root must not be a symlink"));
        }
        let requested = std::path::absolute(requested).map_err(|error| raw_io(error, None))?;
        let (user_data, profiles) = if requested.file_name().is_some_and(|name| name == "profiles")
        {
            (
                requested
                    .parent()
                    .expect("absolute profiles parent")
                    .to_path_buf(),
                requested,
            )
        } else {
            let profiles = requested.join("profiles");
            (requested, profiles)
        };
        if profiles.is_symlink() {
            return Err(integrity("Profile store directory must not be a symlink"));
        }
        // The constructor's original mkdir/chmod effects precede the snapshot.
        let directory = Directory::capture(op, &profiles, true)?;
        Ok(Self {
            user_data,
            directory,
            _paths: paths,
        })
    }

    fn snapshot<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<definitions_model::State> {
        let directory = &self.directory;
        let mut lock =
            PublicationLock::acquire_named(op, directory, publication_io::LockName::Definition)?;
        let state = (|| {
            let read = (|| {
                if !directory.exists(op, INDEX)? {
                    return Ok(None);
                }
                directory.read(op, INDEX).map(Some)
            })();
            let bytes = match read {
                Err(error) if missing(&error) => None,
                other => other.map_err(unreadable)?,
            };
            definitions_model::load(op, bytes.as_ref().map(|b| b.value.as_slice()), budget)
        })();
        // Both original consumers convert rows after the lock's post-yield
        // validation and physical unlock/close.
        finish_snapshot(op, directory, &mut lock, state)
    }
}

#[cfg(unix)]
fn finish_snapshot<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    directory: &Directory,
    lock: &mut publication_io::PublicationLock,
    state: Result<definitions_model::State>,
) -> Result<definitions_model::State> {
    let state = match state {
        Ok(state) => directory
            .validate_file(
                op,
                publication_io::LockName::Definition.text(),
                lock.file.as_ref().expect("definition lock"),
            )
            .map(|()| state),
        Err(error) => Err(error),
    };
    finish_scope(state, lock.close())
}

#[cfg(not(unix))]
fn prefix<S: crate::BootstrapReviewOwner>(
    _: &Operation<'_, S>,
    _: &Path,
    _: &Arc<dyn AllocationBudget>,
) -> Result<DefinitionPrefix> {
    Err(OriginalAuthorityError::unsupported(
        OriginalAuthorityStage::Source,
        "Windows Profile definition persistence/ACL",
    ))
}
