//! The fixed Host registry scopes shared by recovery and Setup lookup.
use super::*;
use tobkiri_canon::AllocationBudget;
#[cfg(unix)]
use tobkiri_canon::CoreValue;

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
#[path = "original_inactive_registration_ci.rs"]
mod inactive_registration_ci;
#[cfg(unix)]
#[path = "original_profile_definitions_projection_io.rs"]
mod projection_io;
#[cfg(all(test, unix))]
#[path = "original_publication_begin_ci.rs"]
mod publication_begin_ci;
#[cfg(all(test, unix))]
#[path = "original_profile_definitions_reference_ci.rs"]
mod reference_ci;
#[cfg(all(test, unix))]
#[path = "original_profile_definitions_tests.rs"]
pub(super) mod tests;

const INDEX: &str = "index.json";
const INTEGRITY: &str =
    "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreIntegrityError";
#[cfg(unix)]
const STORE_ERROR: &str = "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreError";

pub(crate) enum DefinitionRecovery {
    ImportLegacy,
    SuccessorBuilder(SuccessorCandidate),
    RepairLegacyDisplayNames,
    #[cfg(unix)]
    BootstrapTemplate(BootstrapTemplateStore),
    #[cfg(unix)]
    ProfileDefinitions(CoreValue),
}

/// The on-disk registry and original invocation retained during catalog lookup.
#[cfg(unix)]
pub(crate) struct BootstrapTemplateStore {
    store: Store,
    invocation: NativeSetupInvocation,
    cleanup: Arc<io::CleanupState>,
    cancellation: CoreCancellation,
    budget: Arc<dyn AllocationBudget>,
    pid: u32,
}

#[cfg(unix)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum RegistryBootstrapState {
    Empty,
    TemplateAvailable,
    NotRequired,
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

/// Resume the same Profile registry after the bundled catalog subscript.
#[cfg(unix)]
pub(crate) fn complete_bootstrap_template<S: crate::BootstrapReviewOwner>(
    read: &PublicationRead,
    owner: &S,
    cancellation: &CoreCancellation,
    budget: &Arc<dyn AllocationBudget>,
    pending: BootstrapTemplateStore,
    template: &CoreValue,
) -> Result<CoreValue> {
    if !pending.invocation.same_invocation(&read.invocation)
        || !Arc::ptr_eq(&pending.cleanup, &read.cleanup)
        || !pending.cancellation.same_node(cancellation)
        || !Arc::ptr_eq(&pending.budget, budget)
        || pending.pid != std::process::id()
    {
        return Err(OriginalAuthorityError::unavailable());
    }
    let op = review_operation(read, owner, cancellation)?;
    (|| {
        pending
            .store
            .bootstrap_defaults(&op, template, budget, registry_time)?;
        pending.store.catalog_profiles(&op, budget)
    })()
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
    let Some(store) = first_list(op, requested, budget)? else {
        return Ok(DefinitionRecovery::ImportLegacy);
    };
    match store.select_successor(op, budget)? {
        DefinitionRecovery::RepairLegacyDisplayNames => {
            store.repair_display_names(op, budget, registry_time)?;
            store.migrate_retired_pack_projections(op, budget, registry_time, |retirement| {
                op.owner.with_original_catalog_source(|source| {
                    projection_io::inventory(op, source.runtime, retirement.directory, budget)
                })?
            })?;
            match store.bootstrap_state(op, budget)? {
                RegistryBootstrapState::Empty => Ok(DefinitionRecovery::BootstrapTemplate(
                    BootstrapTemplateStore {
                        store,
                        invocation: op.invocation.clone(),
                        cleanup: op.cleanup.clone(),
                        cancellation: op.cancellation.clone(),
                        budget: budget.clone(),
                        pid: op.pid,
                    },
                )),
                _ => Ok(DefinitionRecovery::ProfileDefinitions(
                    store.catalog_profiles(op, budget)?,
                )),
            }
        }
        next => Ok(next),
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

/// The current inactive definition from a separately locked registry snapshot.
pub(crate) struct InactiveBootstrapRegistry {
    #[cfg(unix)]
    definition: definitions_model::InactiveDefinition,
    #[cfg(unix)]
    snapshot: definitions_model::State,
    #[cfg(unix)]
    serialized_definition: Option<Funded<Vec<u8>>>,
}

#[cfg(unix)]
pub(super) struct InactiveHandoverDigests {
    pub(super) snapshot: Digest,
    pub(super) journal: Digest,
}

impl InactiveBootstrapRegistry {
    pub(crate) fn definition(&self) -> &[u8] {
        #[cfg(unix)]
        {
            self.serialized_definition
                .as_ref()
                .expect("review serialized definition")
                .as_slice()
        }
        #[cfg(not(unix))]
        unreachable!("inactive registry snapshot requires native persistence")
    }

    pub(crate) fn release_serialized_definition(&mut self) {
        #[cfg(unix)]
        drop(self.serialized_definition.take());
    }

    #[cfg(unix)]
    fn serialize_definition<S: OriginalAuthorityOwner>(
        &mut self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<()> {
        self.serialized_definition = Some(definitions_model::inactive_definition_bytes(
            op,
            &mut self.snapshot,
            &self.definition,
            budget,
        )?);
        Ok(())
    }

    #[cfg(unix)]
    pub(super) fn check_handover<S: OriginalAuthorityOwner>(
        &mut self,
        op: &Operation<'_, S>,
        journal: &Document,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Digest> {
        let digest =
            definitions_model::check_inactive_handover(op, &mut self.snapshot, journal, budget)?;
        definitions_model::require_inert_definition(
            op,
            &self.snapshot,
            self.definition.profile,
            budget,
        )?;
        Ok(digest)
    }

    #[cfg(unix)]
    pub(super) fn project_handover<S: OriginalAuthorityOwner>(
        &mut self,
        op: &Operation<'_, S>,
        digests: &InactiveHandoverDigests,
        profile_id: &str,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<tobkiri_canon::CoreValue> {
        definitions_model::project_inactive_predecessor(
            op,
            &mut self.snapshot,
            &self.definition,
            digests,
            profile_id,
            budget,
        )
    }
}

#[cfg(unix)]
pub(super) fn inactive_bootstrap_registry<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    profile_id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<InactiveBootstrapRegistry>> {
    let store = Store::new(op, root)?;
    let registry =
        inactive_registry_from_snapshot(op, store.snapshot(op, budget)?, profile_id, budget)?;
    registry
        .map(|mut registry| {
            registry.serialize_definition(op, budget)?;
            Ok(registry)
        })
        .transpose()
}

#[cfg(unix)]
fn inactive_registry_from_snapshot<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    snapshot: definitions_model::State,
    profile_id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<InactiveBootstrapRegistry>> {
    Ok(
        definitions_model::inactive_definition(op, &snapshot, profile_id, budget)?.map(
            |definition| InactiveBootstrapRegistry {
                definition,
                snapshot,
                serialized_definition: None,
            },
        ),
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
        let mut lock = self.lock(op)?;
        let state = self.read_state(op, budget);
        // Both original consumers convert rows after the lock's post-yield
        // validation and physical unlock/close.
        finish_snapshot(op, &self.directory, &mut lock, state)
    }

    fn bootstrap_state<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<RegistryBootstrapState> {
        let state = self.snapshot(op, budget)?;
        definitions_model::bootstrap_state(op, &state, budget)
    }

    fn catalog_profiles<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<CoreValue> {
        let mut state = self.snapshot(op, budget)?;
        definitions_model::catalog_profiles(op, &mut state, budget)
    }

    fn bootstrap_defaults<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        template: &CoreValue,
        budget: &Arc<dyn AllocationBudget>,
        clock: impl FnOnce() -> Result<i64>,
    ) -> Result<()> {
        let profile_id = definitions_model::bootstrap_template_id(op, template, budget)?;
        // get_profile(include_tombstone=True) owns its own snapshot. The
        // caller discards the record, so only presence is needed here.
        definitions_model::canonical_id(&profile_id, op, budget)?;
        let state = self.snapshot(op, budget)?;
        let existing = definitions_model::find_entry(op, &state, &profile_id, budget)?;
        drop(state);
        if existing.is_some() {
            return Ok(());
        }

        definitions_model::normalize_bootstrap_template(op, template, &profile_id, budget)?;
        let now = clock()?;
        if now < 0 {
            return Err(raw(STORE_ERROR, "clock returned a negative timestamp"));
        }
        let created = self.update_definitions(op, budget, |state| {
            definitions_model::prepare_bootstrap_template(
                op,
                state,
                template,
                &profile_id,
                now,
                budget,
            )
            .map(|output| Some((1, output)))
        });
        self.finish_bootstrap_create(op, budget, created)
    }

    fn finish_bootstrap_create<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        created: Result<usize>,
    ) -> Result<()> {
        match created {
            Err(conflict) if conflict.class == definitions_model::CONFLICT => {
                match self.bootstrap_state(op, budget) {
                    Ok(RegistryBootstrapState::Empty) => Err(conflict),
                    Ok(_) => Ok(()),
                    Err(mut error) => {
                        let mut context = &mut error.context;
                        while let Some(previous) = context {
                            context = &mut previous.context;
                        }
                        *context = Some(Box::new(conflict));
                        Err(error)
                    }
                }
            }
            result => result.map(|_| ()),
        }
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

    fn repair_display_names<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        clock: impl FnOnce() -> Result<i64>,
    ) -> Result<usize> {
        self.update_definitions(op, budget, |state| {
            definitions_model::prepare_display_name_repair(op, state, budget, clock)
        })
    }

    fn migrate_retired_pack_projections<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        clock: impl FnOnce() -> Result<i64>,
        inventory: impl FnMut(&projection_io::Retirement) -> Result<(Digest, usize)>,
    ) -> Result<usize> {
        self.update_definitions(op, budget, |state| {
            definitions_model::prepare_retired_pack_migration(op, state, budget, clock, inventory)
        })
    }

    fn update_definitions<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        prepare: impl FnOnce(
            &definitions_model::State,
        ) -> Result<Option<(usize, tobkiri_canon::CoreBuffer)>>,
    ) -> Result<usize> {
        let mut lock = self.lock(op)?;
        let result = (|| {
            let state = self.read_state(op, budget)?;
            let Some((count, output)) = prepare(&state)? else {
                return Ok(0);
            };
            drop(state);
            self.directory
                .replace_definition(op, output.as_bytes())
                .map_err(|error| {
                    if error.class == RAW_IO
                        || matches!(
                            error.class,
                            "builtins.FileNotFoundError"
                                | "builtins.ValueError"
                                | "tobkiri_protocol.secure_persistence.SecurePersistenceError"
                        )
                    {
                        caused(
                            raw(
                                STORE_ERROR,
                                "Profile definition store could not be committed",
                            ),
                            error,
                        )
                    } else {
                        error
                    }
                })?;
            Ok(count)
        })();
        finish_snapshot(op, &self.directory, &mut lock, result)
    }
}

#[cfg(unix)]
fn registry_time() -> Result<i64> {
    let seconds = match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(elapsed) => i64::try_from(elapsed.as_secs()).ok(),
        Err(error) => i64::try_from(error.duration().as_secs())
            .ok()
            .and_then(i64::checked_neg),
    };
    seconds.ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))
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
