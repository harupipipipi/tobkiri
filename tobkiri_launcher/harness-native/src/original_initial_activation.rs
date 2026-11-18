//! Initial publication after an explicit approval and independent Bundle reread.
use super::*;
use crate::BootstrapActivationCandidate;
use crate::original_authority::catalog::CatalogIo;
use std::sync::Mutex;
use workspace_active::{ActiveSnapshot, initial_records};

#[cfg(test)]
#[path = "original_initial_activation_tests.rs"]
mod tests;

pub(crate) struct InitialPublication<'a> {
    pub(crate) candidate: &'a BootstrapActivationCandidate,
    pub(crate) resolved: &'a CoreValue,
    pub(crate) source_profile: &'a CoreValue,
    pub(crate) loader: &'a Mutex<Option<CatalogIo>>,
    pub(crate) schemas: BoundProfileSchemas,
}

impl PublicationRead {
    pub(crate) async fn activate_initial<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        cancellation: &CoreCancellation,
        budget: &Arc<dyn AllocationBudget>,
        initial: InitialPublication<'_>,
    ) -> Result<()> {
        let payload =
            self.prepare_initial_publication(owner, cancellation, initial.candidate, budget)?;
        let confirmed = self
            .confirmed
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let scope = &confirmed.scope;
        let root = scope
            .directory
            .profiles_user_data()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let op = self.candidate_operation(owner, cancellation)?;
        let candidate = payload
            .get_key("candidate")
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let profile = candidate
            .get_key("profile_id")
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let profile_id = profile
            .as_str()
            .filter(|value| value.len() <= 128 && model::canonical_ascii_id(value))
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let activation = candidate
            .get_key("activation_id")
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let activation_id = activation
            .as_str()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        drop(candidate);
        let predecessor = initial
            .candidate
            .confirmation()
            .get_key("definition_predecessor")
            .filter(|value| !matches!(value.inspect(), tobkiri_canon::CoreRef::Null));

        publication_begin::persist_initial_intent(&op, scope, payload, budget)?;
        if predecessor.is_none() {
            scope.validate(&op)?;
            definitions::register_initial_definition(&op, root, initial.source_profile, budget)?;
        }
        let _paths = op.reserve(
            OriginalAuthorityStage::Path,
            root.as_os_str()
                .len()
                .saturating_add(profile_id.len())
                .saturating_add(128),
        )?;
        let path = root.join("workspaces").join(profile_id);
        scope.validate(&op)?;
        Directory::create_workspace(&op, &path)?;

        with_registered_store_async(&op, root, async |store| {
            let workspace = RecoveryWorkspace::open(&op, &path, LockName::workspace(profile_id))?;
            let activation = InitialWorkspace {
                op: &op,
                scope,
                store,
                workspace,
                budget,
                profile_id,
                initial,
            };
            activation.recover().await?;
            if let Some(predecessor) = predecessor {
                activation.workspace.republish(&op, |state, _| {
                    scope.validate(&op)?;
                    if state.exists(&op, "active.json")? {
                        return Err(raw(
                            recovery_workspace::APP_DENIED,
                            "inactive bootstrap predecessor has an active workspace",
                        ));
                    }
                    definitions::register_initial_inactive_definition(
                        &op,
                        root,
                        activation.initial.source_profile,
                        profile_id,
                        &predecessor,
                        budget,
                    )
                })?;
            }
            let created_at = created_at(&op, budget)?;
            activation.activate(activation_id, &created_at).await?;
            let active = activation.load_active_snapshot().await?;
            publish_active::publish(&op, root, scope, budget, &active)
        })
        .await?;
        self.verify_confirmed(owner, cancellation)
    }
}

struct InitialWorkspace<'a, 'op, 'host, S> {
    op: &'op Operation<'host, S>,
    scope: &'a RecoveryScope,
    store: &'a Store<'op, 'host, S>,
    workspace: RecoveryWorkspace,
    budget: &'a Arc<dyn AllocationBudget>,
    profile_id: &'a str,
    initial: InitialPublication<'a>,
}

impl<S: crate::BootstrapReviewOwner> InitialWorkspace<'_, '_, '_, S> {
    async fn recover(&self) -> Result<()> {
        loop {
            let step = self.recovered_lock(async |_, _| Ok(())).await?;
            if self.resume(step)?.is_some() {
                return Ok(());
            }
        }
    }

    async fn activate(&self, activation_id: &str, created_at: &CoreValue) -> Result<()> {
        let resolved = self.initial.resolved;
        let schemas = self.initial.schemas;
        initial_records::validate(
            self.op,
            self.budget,
            schemas,
            resolved,
            self.profile_id,
            activation_id,
        )?;
        let profile = resolved
            .get_key("profile")
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        loop {
            let identity = self.verify_artifact(&profile)?;
            let step = self
                .recovered_lock(async |state, workspace_digest| {
                    legacy_activation::activate(
                        self.op,
                        self.store,
                        state,
                        workspace_digest,
                        self.budget,
                        legacy_activation::Input {
                            profile_id: self.profile_id,
                            activation_id,
                            resolved,
                            created_at,
                            schemas,
                        },
                        |profile| self.require_identity(profile, &identity),
                    )
                })
                .await?;
            if self.resume(step)?.is_some() {
                return Ok(());
            }
        }
    }

    async fn load_active_snapshot(&self) -> Result<ActiveSnapshot> {
        loop {
            let Some(first) = self.resume(self.read_active().await?)? else {
                continue;
            };
            let verified = self.with_catalog(|io| {
                io.verify_initial_active_artifact(
                    self.op,
                    &first.artifact_profile,
                    self.profile_id,
                    self.initial.source_profile,
                )
            })?;
            if verified == ActiveArtifactVerification::SuccessorNeedsReconfirmation {
                return Err(raw(
                    "ecosystem.defaultspack.domain.runtime_v4.service.ProfileReconfirmationRequired",
                    "active Profile Shell artifact identity was superseded by the verified packaged release; explicit reconfirmation is required",
                ));
            }
            let Some(current) = self.resume(self.read_active().await?)? else {
                continue;
            };
            first.require_same_activation(&current)?;
            return Ok(first);
        }
    }

    async fn read_active(&self) -> Result<ControlFlow<PendingRepublish, ActiveSnapshot>> {
        self.recovered_lock(async |state, workspace_digest| {
            // With the current records just committed above, legacy migration
            // cannot supply this fresh publication's candidate.
            let snapshot = workspace_active::read(
                self.op,
                state,
                workspace_digest,
                self.budget,
                Some(self.initial.schemas),
                self.store,
                (self.profile_id, async |_| Ok(None)),
            )
            .await?;
            snapshot.current.ok_or_else(|| {
                raw(
                    recovery_workspace::APP_DENIED,
                    "Profile publication candidate changed",
                )
            })
        })
        .await
    }

    async fn recovered_lock<T>(
        &self,
        run: impl AsyncFnOnce(&Directory, &Digest) -> Result<T>,
    ) -> Result<ControlFlow<PendingRepublish, T>> {
        self.workspace
            .recover_async(self.op, async |state, workspace_digest, journal| {
                self.scope.validate(self.op)?;
                if let Some(pending) = pending_reservation::recover_pending_locked(
                    self.op,
                    self.store,
                    state,
                    journal,
                    self.profile_id,
                    self.budget,
                )? {
                    return Ok(ControlFlow::Break(pending));
                }
                Ok(ControlFlow::Continue(run(state, workspace_digest).await?))
            })
            .await
    }

    fn resume<T>(&self, step: ControlFlow<PendingRepublish, T>) -> Result<Option<T>> {
        match step {
            ControlFlow::Continue(value) => Ok(Some(value)),
            ControlFlow::Break(pending) => {
                let identity = self.verify_artifact(&pending.profile)?;
                pending.publish(
                    self.op,
                    self.store,
                    &self.workspace,
                    self.profile_id,
                    self.budget,
                    |profile| self.require_identity(profile, &identity),
                )?;
                Ok(None)
            }
        }
    }

    fn verify_artifact(&self, profile: &CoreValue) -> Result<ArtifactTreeIdentity> {
        self.with_catalog(|io| {
            io.verify_initial_artifact(
                self.op,
                profile,
                self.profile_id,
                self.initial.source_profile,
            )
        })
    }

    fn require_identity(&self, profile: &CoreValue, expected: &ArtifactTreeIdentity) -> Result<()> {
        self.scope.validate(self.op)?;
        self.with_catalog(|io| io.require_exact_artifact_identity(self.op, profile, expected))
    }

    fn with_catalog<T>(&self, run: impl FnOnce(&CatalogIo) -> Result<T>) -> Result<T> {
        let catalog = self
            .initial
            .loader
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        run(catalog
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?)
    }
}

fn created_at<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    use std::time::{SystemTime, UNIX_EPOCH};
    let stage = OriginalAuthorityStage::Source;
    op.check(stage)?;
    let _formatted = op.reserve(stage, 128)?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| OriginalAuthorityError::unavailable())?;
    let seconds: libc::time_t = now
        .as_secs()
        .try_into()
        .map_err(|_| OriginalAuthorityError::unavailable())?;
    let mut stamp = std::mem::MaybeUninit::<libc::tm>::uninit();
    if unsafe { libc::gmtime_r(&seconds, stamp.as_mut_ptr()) }.is_null() {
        return Err(OriginalAuthorityError::unavailable());
    }
    let stamp = unsafe { stamp.assume_init() };
    let year = stamp
        .tm_year
        .checked_add(1900)
        .filter(|year| (1..=9999).contains(year))
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let mut text = format!(
        "{year:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
        stamp.tm_mon + 1,
        stamp.tm_mday,
        stamp.tm_hour,
        stamp.tm_min,
        stamp.tm_sec
    );
    let micros = now.subsec_micros();
    if micros != 0 {
        use std::fmt::Write;
        write!(text, ".{micros:06}").expect("format into String");
    }
    text.push('Z');
    CoreValue::string(&text, budget).map_err(|_| OriginalAuthorityError::capacity(stage))
}
