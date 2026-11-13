//! The fixed Host registry scopes shared by recovery and Setup lookup.
use super::*;
use tobkiri_canon::AllocationBudget;

#[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
#[path = "original_setup_candidate_reference_ci.rs"]
mod candidate_reference_ci;
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

pub(crate) enum DefinitionRecovery {
    ImportLegacy,
    SuccessorBuilder(SuccessorCandidate),
    RepairLegacyDisplayNames,
}

/// Funded DATA for the unbound successor builder on the same review.
/// A future builder must execute inside the definition lock.
pub(crate) struct SuccessorCandidate {
    #[cfg(unix)]
    _state: definitions_model::State,
    #[cfg(unix)]
    _item: usize,
    #[cfg(unix)]
    _profile_id: usize,
    #[cfg(unix)]
    _item_origin: definitions_model::SuccessorItemOrigin,
    #[cfg(unix)]
    _source_path: definitions_model::SuccessorSourcePath,
}

#[cfg(all(test, unix))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DefinitionPrefix {
    ImportLegacyNotBound,
    MigrateLegacySuccessorsNotBound,
}

/// Borrow Read's existing invocation, close ledger and prepaid diagnostics.
/// The caller drops owned errors before Read; no new account/source is owned.
fn review_operation<'a, S: crate::BootstrapReviewOwner>(
    read: &'a PublicationRead,
    owner: &'a S,
    cancellation: &'a CoreCancellation,
) -> Result<Operation<'a, S>> {
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
    Ok(op)
}

/// Borrow Read's existing invocation across the two definition-lock scopes.
pub(crate) fn recover<S: crate::BootstrapReviewOwner>(
    read: &PublicationRead,
    owner: &S,
    cancellation: &CoreCancellation,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<DefinitionRecovery> {
    let op = review_operation(read, owner, cancellation)?;
    owner
        .with_original_review_user_data(|root| recover_at(&op, root, budget))?
        .map_err(recovery_error)
}

// Retain the original constructor/list comparison's stopping point.
#[cfg(all(test, unix))]
fn run<S: crate::BootstrapReviewOwner>(
    read: &PublicationRead,
    owner: &S,
    cancellation: &CoreCancellation,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<DefinitionPrefix> {
    let op = review_operation(read, owner, cancellation)?;
    owner
        .with_original_review_user_data(|root| {
            Ok(match first_list(&op, root, budget)? {
                None => DefinitionPrefix::ImportLegacyNotBound,
                Some(_) => DefinitionPrefix::MigrateLegacySuccessorsNotBound,
            })
        })?
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
fn recover_at<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    requested: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<DefinitionRecovery> {
    match first_list(op, requested, budget)? {
        Some(store) => store.select_successor(op, budget),
        None => Ok(DefinitionRecovery::ImportLegacy),
    }
}

#[cfg(unix)]
fn first_list<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    requested: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<Store>> {
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
            return Ok(None);
        }
    }
    Ok(Some(store))
}

#[cfg(unix)]
pub(super) struct RegisteredProfile {
    state: definitions_model::State,
    profile: definitions_model::StoredProfile,
}

#[cfg(unix)]
pub(super) fn get_profile<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    profile_id: &Text,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<RegisteredProfile>> {
    let store = Store::new(op, root)?;
    store.get_profile(op, profile_id, budget)
}

/// The actual registry and pointer reads retained by the fixed Setup call.
pub(crate) struct BootstrapPredecessorInputs {
    #[cfg(unix)]
    _snapshot: definitions_model::State,
    #[cfg(unix)]
    registered: Option<RegisteredProfile>,
    #[cfg(unix)]
    pointer: Option<active_profile::LoadedPointer>,
}

impl BootstrapPredecessorInputs {
    pub(crate) fn needs_source_digest(&self, profile_id: &str) -> bool {
        #[cfg(unix)]
        {
            self.registered.is_some()
                && self
                    .pointer
                    .as_ref()
                    .is_some_and(|p| p.profile_id().is(profile_id))
        }
        #[cfg(not(unix))]
        {
            let _ = profile_id;
            false
        }
    }

    pub(crate) fn matches_source_digest(&self, digest: &Digest) -> bool {
        #[cfg(unix)]
        {
            self.registered.as_ref().is_some_and(|entry| {
                entry
                    .state
                    .doc
                    .string_is(entry.profile.references[1], digest.as_str())
            })
        }
        #[cfg(not(unix))]
        {
            let _ = digest;
            false
        }
    }
}

#[cfg(unix)]
pub(super) fn bootstrap_predecessor_inputs<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    profile_id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<BootstrapPredecessorInputs> {
    let store = Store::new(op, root)?;
    let snapshot = store.snapshot(op, budget)?;
    let _id = op.reserve(
        OriginalAuthorityStage::Source,
        profile_id
            .len()
            .saturating_mul(std::mem::size_of::<u32>())
            .max(1),
    )?;
    let registered = store.get_profile(op, &Text::from_str(profile_id), budget)?;
    let pointer = active_profile::load(op, root)?;
    Ok(BootstrapPredecessorInputs {
        _snapshot: snapshot,
        registered,
        pointer,
    })
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
        let mut lock = self.lock(op)?;
        let state = self.read_state(op, budget);
        // Both original consumers convert rows after the lock's post-yield
        // validation and physical unlock/close.
        finish_snapshot(op, &self.directory, &mut lock, state)
    }

    fn get_profile<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        profile_id: &Text,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<RegisteredProfile>> {
        definitions_model::canonical_id(profile_id, op, budget)?;
        let mut state = self.snapshot(op, budget)?;
        Ok(definitions_model::get(op, &mut state, profile_id, budget)?
            .map(|profile| RegisteredProfile { state, profile }))
    }

    fn lock<S: OriginalAuthorityOwner>(&self, op: &Operation<'_, S>) -> Result<PublicationLock> {
        PublicationLock::acquire_named(op, &self.directory, publication_io::LockName::Definition)
    }

    fn read_state<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<definitions_model::State> {
        let read = (|| {
            if !self.directory.exists(op, INDEX)? {
                return Ok(None);
            }
            self.directory.read(op, INDEX).map(Some)
        })();
        let bytes = match read {
            Err(error) if missing(&error) => None,
            other => other.map_err(unreadable)?,
        };
        definitions_model::load(op, bytes.as_ref().map(|b| b.value.as_slice()), budget)
    }

    fn select_successor<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<DefinitionRecovery> {
        use definitions_model::SuccessorSelection;
        let mut lock = self.lock(op)?;
        let selected = (|| {
            let state = self.read_state(op, budget)?;
            let selected = definitions_model::select_successor(op, &state, budget)?;
            Ok((state, selected))
        })();
        match selected {
            Ok((_, SuccessorSelection::Exhausted)) => finish_snapshot(
                op,
                &self.directory,
                &mut lock,
                Ok(DefinitionRecovery::RepairLegacyDisplayNames),
            ),
            Ok((
                state,
                SuccessorSelection::BuilderEntry {
                    item,
                    profile_id,
                    item_origin,
                    source_path,
                },
            )) => {
                // Like an exception at the helper entry, the boundary closes
                // the physical lock without its normal post-yield validation.
                let candidate = SuccessorCandidate {
                    _state: state,
                    _item: item,
                    _profile_id: profile_id,
                    _item_origin: item_origin,
                    _source_path: source_path,
                };
                finish_scope(
                    Ok(DefinitionRecovery::SuccessorBuilder(candidate)),
                    lock.close(),
                )
            }
            Err(error) => finish_scope(Err(error), lock.close()),
        }
    }
}

#[cfg(unix)]
fn finish_snapshot<S: OriginalAuthorityOwner, T>(
    op: &Operation<'_, S>,
    directory: &Directory,
    lock: &mut publication_io::PublicationLock,
    state: Result<T>,
) -> Result<T> {
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
fn recover_at<S: crate::BootstrapReviewOwner>(
    _: &Operation<'_, S>,
    _: &Path,
    _: &Arc<dyn AllocationBudget>,
) -> Result<DefinitionRecovery> {
    Err(OriginalAuthorityError::unsupported(
        OriginalAuthorityStage::Source,
        "Windows Profile definition persistence/ACL",
    ))
}
