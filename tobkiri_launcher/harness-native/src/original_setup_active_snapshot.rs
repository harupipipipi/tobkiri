//! Load the registered Setup predecessor on the original native owner.
use super::*;
use crate::BootstrapProfileSchemas;
use crate::original_authority::catalog::CatalogIo;
use crate::original_authority::io::with_existing_store_async;
use recovery_workspace::APP_DENIED;
use std::sync::Mutex;
use workspace_active::{
    ActiveReviewRecords, ActiveSnapshot, ActiveSnapshotRead, ProfileCheck, SnapshotProjection,
};

#[path = "original_setup_active_catalog_policy.rs"]
mod active_catalog_policy;
#[path = "original_setup_optional_packs.rs"]
mod optional_packs;
pub(crate) use active_catalog_policy::{
    RegisteredCatalogSelection, RegisteredDefinitionCandidate, RegisteredSourceUpdate,
};

#[derive(Debug)]
pub(crate) enum RegisteredSnapshotReached {
    CatalogPolicy(RegisteredCatalogPolicy),
    LegacySuccessor,
    SnapshotSchemas,
}

#[derive(Debug)]
pub(crate) struct RegisteredCatalogPolicy {
    pub(crate) registered_profile: CoreValue,
    pub(crate) active_profile: CoreValue,
    pub(crate) active_plan: Option<CoreValue>,
    pub(crate) verified_definition_digest: CoreValue,
}

pub(crate) struct PredecessorActivation<'a> {
    pub(crate) inputs: &'a definitions::BootstrapPredecessorInputs,
    pub(crate) source_digest: &'a Digest,
    pub(crate) profile_id: &'a str,
    pub(crate) loader: &'a Mutex<Option<CatalogIo>>,
    pub(crate) schemas: Option<BoundProfileSchemas>,
}

pub(crate) enum PredecessorActivationReached {
    Binding(Option<CoreValue>),
    SnapshotSchemas,
    LegacySuccessor,
}

const RECONFIRMATION_CLASS: &str =
    "ecosystem.defaultspack.domain.runtime_v4.service.ProfileReconfirmationRequired";
const RECONFIRMATION_MESSAGE: &str = "active Profile Shell artifact identity was superseded by the verified packaged release; explicit reconfirmation is required";

/// Authenticated, current v5 record data for the original owner. This proof is
/// neither a compiled native Profile nor permission to dispatch a Pack.
pub struct OriginalActiveProfile {
    identity: CoreValue,
    profile: CoreValue,
    lock: CoreValue,
    plan: CoreValue,
}

impl OriginalActiveProfile {
    pub fn identity(&self) -> &CoreValue {
        &self.identity
    }

    pub fn records(&self) -> (&CoreValue, &CoreValue, &CoreValue) {
        (&self.profile, &self.lock, &self.plan)
    }
}

/// Inspect a current activation on the existing funded invocation. The caller
/// retains the actual selected owner and checks the cleanup receipt before
/// publishing any result, including if the future is dropped.
pub struct OriginalActivationInspection(PublicationRead);

impl OriginalActivationInspection {
    pub fn new(
        invocation: &NativeSetupInvocation,
    ) -> std::result::Result<Self, OriginalAuthorityAdmission> {
        PublicationRead::new(invocation).map(Self)
    }

    pub fn close_receipt(&self) -> OriginalAuthorityCloseReceipt {
        self.0.receipt()
    }

    pub async fn read<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        cancellation: &CoreCancellation,
        budget: &Arc<dyn AllocationBudget>,
        schemas: &'static BootstrapProfileSchemas,
        component_digest: &Digest,
    ) -> Result<OriginalActiveProfile> {
        let unavailable = || raw(APP_DENIED, "current v5 Profile activation is unavailable");
        let schemas = schemas.bind(component_digest).map_err(|_| unavailable())?;
        let op =
            PublicationRead::operation(&self.0.invocation, &self.0.cleanup, owner, cancellation)?;
        op.check(OriginalAuthorityStage::Source)?;
        let root = owner.with_original_review_user_data(|root| {
            let capacity = op.reserve(
                OriginalAuthorityStage::Path,
                root.as_os_str().len().saturating_add(128),
            )?;
            Ok::<_, OriginalAuthorityError>(Funded {
                value: root.to_path_buf(),
                _capacity: capacity,
            })
        })??;
        let before =
            active_profile::load_existing_verified(&op, &root, budget)?.ok_or_else(unavailable)?;
        let Node::String(snapshot_digest) = before
            .doc
            .node(before.pointer.field("activation_snapshot_digest"))
        else {
            return Err(unavailable());
        };
        let _identity_capacity = op.reserve(OriginalAuthorityStage::Source, 1024)?;
        let profile_id = before.profile_id().utf8()?;
        let workspace = workspace_path(&op, &root, &profile_id)?;
        let workspace =
            RecoveryWorkspace::open_existing(&op, &workspace, LockName::workspace(&profile_id))?;
        let current = with_existing_store_async(&op, &root, async |store| {
            workspace
                .inspect_current(&op, async |state, digest| {
                    let mut current = workspace_active::read_with_projection(
                        &op,
                        state,
                        digest,
                        budget,
                        Some(schemas),
                        store,
                        (
                            &profile_id,
                            SnapshotProjection::CurrentActivation(snapshot_digest),
                            async |_| Ok(None),
                        ),
                    )
                    .await?
                    .current
                    .ok_or_else(unavailable)?;
                    let after = active_profile::load_existing_verified(&op, &root, budget)?
                        .ok_or_else(unavailable)?;
                    let catalog_matches =
                        match before.doc.node(before.pointer.field("catalog_revision")) {
                            Node::String(expected) => current
                                .catalog_revision
                                .as_str()
                                .is_some_and(|value| expected.is(value)),
                            _ => false,
                        };
                    let records = current.review_records.take().ok_or_else(unavailable)?;
                    let identity = current.into_candidate();
                    if !before
                        .pointer
                        .matches(&before.doc, &after.pointer, &after.doc)
                        || !matches_host_pointer(&identity, &before)
                        || !catalog_matches
                        || identity
                            .get_key("profile_id")
                            .is_none_or(|value| value.as_str() != Some(profile_id.as_str()))
                    {
                        return Err(raw(
                            APP_DENIED,
                            "active Profile changed during Authority inspection",
                        ));
                    }
                    Ok(OriginalActiveProfile {
                        identity,
                        profile: records.profile,
                        lock: records.lock.ok_or_else(unavailable)?,
                        plan: records.plan,
                    })
                })
                .await
        })
        .await?;
        op.check(OriginalAuthorityStage::Source)?;
        if !self.0.cleanup.verified() {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Close,
                "existing activation inspection cleanup is uncertain",
            ));
        }
        Ok(current)
    }
}

impl PublicationRead {
    pub(crate) async fn review_registered_activation<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        budget: &Arc<dyn AllocationBudget>,
        loader: &Mutex<Option<CatalogIo>>,
        schemas: Option<BoundProfileSchemas>,
    ) -> Result<RegisteredSnapshotReached> {
        self.enter_registered_candidate()?;
        let (root, candidate) = self.registered_candidate();
        let op = self.candidate_operation(owner, cancellation)?;
        let receipt = self.receipt();
        let workspace = candidate.workspace.as_path();
        let profile_id = workspace
            .file_name()
            .and_then(|name| name.to_str())
            .expect("verified workspace Profile ID");
        let reached = with_registered_store_async(&op, root, async |store| {
            let workspace =
                RecoveryWorkspace::open(&op, workspace, LockName::workspace(profile_id))?;
            RegisteredActivation {
                op: &op,
                store,
                workspace,
                budget,
                loader,
                profile_id,
                schemas,
                source: ShellSource::Bundled,
            }
            .load_snapshot(SnapshotProjection::RegisteredReview)
            .await
        })
        .await?;
        if self.receipt() != receipt {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Close,
                "registered Setup Authority cleanup is uncertain",
            ));
        }
        op.check(OriginalAuthorityStage::Source)?;
        // The original compares the previously held Host pointer only after
        // the Authority context exits, even when Shell drift was verified.
        match reached {
            Snapshot::Verified(active) => {
                let records = active.records.expect("registered review records");
                let identity = active.identity;
                let verified_definition_digest = identity
                    .get_key("profile_definition_digest")
                    .filter(|digest| digest.as_str().is_some());
                let Some(verified_definition_digest) = verified_definition_digest
                    .filter(|_| matches_host_pointer(&identity, &candidate.pointer))
                else {
                    return Err(raw(
                        "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreConflict",
                        "bootstrap review does not match the verified active definition",
                    ));
                };
                Ok(RegisteredSnapshotReached::CatalogPolicy(
                    RegisteredCatalogPolicy {
                        registered_profile: candidate.registered.review_profile(&op, budget)?,
                        active_profile: records.profile,
                        // The old Plan participates only on the current path.
                        active_plan: (active.verification == ActiveArtifactVerification::Current)
                            .then_some(records.plan),
                        verified_definition_digest,
                    },
                ))
            }
            Snapshot::Boundary(SnapshotBoundary::LegacySuccessor) => {
                Ok(RegisteredSnapshotReached::LegacySuccessor)
            }
            Snapshot::Boundary(SnapshotBoundary::Schemas) => {
                Ok(RegisteredSnapshotReached::SnapshotSchemas)
            }
        }
    }

    pub(crate) async fn review_predecessor_activation<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        budget: &Arc<dyn AllocationBudget>,
        predecessor: PredecessorActivation<'_>,
    ) -> Result<PredecessorActivationReached> {
        let PredecessorActivation {
            inputs,
            source_digest,
            profile_id,
            loader,
            schemas,
        } = predecessor;
        if !inputs.needs_source_digest(profile_id) || !inputs.matches_source_digest(source_digest) {
            return Err(OriginalAuthorityError::unavailable());
        }
        let pointer = inputs
            .verified_pointer()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        self.enter_predecessor_activation(owner, cancellation)?;
        let root = self.predecessor_activation_root();
        let op = self.candidate_operation(owner, cancellation)?;
        let receipt = self.receipt();
        let workspace = workspace_path(&op, root, profile_id)?;
        let mut reconfirmation = None;
        let reached = with_registered_store_async(&op, root, async |store| {
            let workspace =
                RecoveryWorkspace::open(&op, &workspace, LockName::workspace(profile_id))?;
            let observed = RegisteredActivation {
                op: &op,
                store,
                workspace,
                budget,
                loader,
                profile_id,
                schemas,
                source: ShellSource::Resolved(source_digest),
            }
            .load_snapshot(SnapshotProjection::Identity)
            .await?;
            match observed {
                Snapshot::Verified(active)
                    if active.verification
                        == ActiveArtifactVerification::SuccessorNeedsReconfirmation =>
                {
                    reconfirmation = Some(active);
                    // This caller recognizes reconfirmation outside the Authority
                    // context. A failing close retains the original exception.
                    Err(raw(RECONFIRMATION_CLASS, RECONFIRMATION_MESSAGE))
                }
                other => Ok(other),
            }
        })
        .await;
        let reached = match reached {
            Ok(observed) => observed,
            Err(error) if error.class == RECONFIRMATION_CLASS && reconfirmation.is_some() => {
                Snapshot::Verified(reconfirmation.expect("actual verified reconfirmation"))
            }
            Err(error) => return Err(error),
        };
        if self.receipt() != receipt {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Close,
                "Pack pin predecessor Authority cleanup is uncertain",
            ));
        }
        op.check(OriginalAuthorityStage::Source)?;
        let identity = match reached {
            Snapshot::Verified(active) => active.identity,
            Snapshot::Boundary(SnapshotBoundary::Schemas) => {
                return Ok(PredecessorActivationReached::SnapshotSchemas);
            }
            Snapshot::Boundary(SnapshotBoundary::LegacySuccessor) => {
                return Ok(PredecessorActivationReached::LegacySuccessor);
            }
        };
        if !matches_host_pointer(&identity, pointer) {
            return Err(raw(
                "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreConflict",
                "Pack pin review does not match the verified active pointer",
            ));
        }
        let active_digest = identity
            .get_key("profile_definition_digest")
            .and_then(|digest| Digest::parse(digest.as_str()?).ok())
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if &active_digest == source_digest {
            return Ok(PredecessorActivationReached::Binding(None));
        }
        let loader = loader.lock().unwrap_or_else(|error| error.into_inner());
        let catalog = loader
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        Ok(PredecessorActivationReached::Binding(
            inputs.pack_pin_predecessor(&op, profile_id, &active_digest, catalog, budget)?,
        ))
    }
}

enum Snapshot {
    Verified(VerifiedSnapshot),
    Boundary(SnapshotBoundary),
}

struct VerifiedSnapshot {
    identity: CoreValue,
    records: Option<ActiveReviewRecords>,
    verification: ActiveArtifactVerification,
}

impl Snapshot {
    fn verified(mut active: ActiveSnapshot, verification: ActiveArtifactVerification) -> Self {
        let records = active.review_records.take();
        Self::Verified(VerifiedSnapshot {
            identity: active.into_candidate(),
            records,
            verification,
        })
    }
}

enum SnapshotBoundary {
    Schemas,
    LegacySuccessor,
}

enum ShellSource<'a> {
    Bundled,
    Resolved(&'a Digest),
}

struct RegisteredActivation<'a, 'op, 'host, S> {
    op: &'op Operation<'host, S>,
    store: &'a Store<'op, 'host, S>,
    workspace: RecoveryWorkspace,
    budget: &'a Arc<dyn AllocationBudget>,
    loader: &'a Mutex<Option<CatalogIo>>,
    profile_id: &'a str,
    schemas: Option<BoundProfileSchemas>,
    source: ShellSource<'a>,
}

impl<S: crate::BootstrapReviewOwner> RegisteredActivation<'_, '_, '_, S> {
    async fn load_snapshot(&self, projection: SnapshotProjection<'_>) -> Result<Snapshot> {
        loop {
            let Some(first) = self.resume(self.read(projection).await?)? else {
                continue;
            };
            let Some(active) = first.current else {
                return Ok(Self::boundary(first));
            };
            // Hash outside the workspace lock. The store remains entered.
            let verified = self.with_catalog(|io| match self.source {
                ShellSource::Bundled => io.verify_bundled_active_artifact(
                    self.op,
                    &active.artifact_profile,
                    self.profile_id,
                ),
                ShellSource::Resolved(digest) => {
                    let source = self.resolved_source(io, digest)?;
                    io.verify_initial_active_artifact(
                        self.op,
                        &active.artifact_profile,
                        self.profile_id,
                        &source,
                    )
                }
            })?;
            if verified == ActiveArtifactVerification::SuccessorNeedsReconfirmation {
                return Ok(Snapshot::verified(active, verified));
            }
            let Some(second) = self.resume(self.read(SnapshotProjection::Identity).await?)? else {
                continue;
            };
            let Some(current) = second.current else {
                return Ok(Self::boundary(second));
            };
            active.require_same_activation(&current)?;
            return Ok(Snapshot::verified(active, verified));
        }
    }

    fn boundary(read: ActiveSnapshotRead) -> Snapshot {
        Snapshot::Boundary(match read.prefix.check {
            ProfileCheck::NeedsLegacySuccessor => SnapshotBoundary::LegacySuccessor,
            _ => SnapshotBoundary::Schemas,
        })
    }

    async fn read(
        &self,
        projection: SnapshotProjection<'_>,
    ) -> Result<ControlFlow<PendingRepublish, ActiveSnapshotRead>> {
        self.workspace
            .recover_async(self.op, async |state, workspace_digest, journal| {
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
                let read = workspace_active::read_with_projection(
                    self.op,
                    state,
                    workspace_digest,
                    self.budget,
                    self.schemas,
                    self.store,
                    // Legacy resolution against this bundled catalog is a later
                    // binding. Never drive the journal recovery's replacement resolver.
                    (self.profile_id, projection, async |_| Ok(None)),
                )
                .await?;
                Ok(ControlFlow::Continue(read))
            })
            .await
    }

    fn resume(
        &self,
        read: ControlFlow<PendingRepublish, ActiveSnapshotRead>,
    ) -> Result<Option<ActiveSnapshotRead>> {
        match read {
            ControlFlow::Continue(read) => Ok(Some(read)),
            ControlFlow::Break(pending) => {
                let identity = self.with_catalog(|io| match self.source {
                    ShellSource::Bundled => io.verify_bundled_pending_artifact(
                        self.op,
                        &pending.profile,
                        self.profile_id,
                    ),
                    ShellSource::Resolved(digest) => {
                        let source = self.resolved_source(io, digest)?;
                        io.verify_initial_artifact(
                            self.op,
                            &pending.profile,
                            self.profile_id,
                            &source,
                        )
                    }
                })?;
                pending.publish(
                    self.op,
                    self.store,
                    &self.workspace,
                    self.profile_id,
                    self.budget,
                    |profile| {
                        self.with_catalog(|io| {
                            io.require_exact_artifact_identity(self.op, profile, &identity)
                        })
                    },
                )?;
                Ok(None)
            }
        }
    }

    fn resolved_source(&self, io: &CatalogIo, digest: &Digest) -> Result<CoreValue> {
        let source = io.resolved_review_profile(self.op, self.profile_id)?;
        let actual = source
            .digest_guest(self.budget)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
        if &actual != digest {
            return Err(OriginalAuthorityError::unavailable());
        }
        Ok(source)
    }

    fn with_catalog<T>(&self, run: impl FnOnce(&CatalogIo) -> Result<T>) -> Result<T> {
        let io = self
            .loader
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        run(io
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?)
    }
}

fn matches_host_pointer(identity: &CoreValue, pointer: &active_profile::LoadedPointer) -> bool {
    [
        "profile_revision",
        "activation_id",
        "plan_digest",
        "lock_digest",
    ]
    .into_iter()
    .all(
        |field| match pointer.doc.node(pointer.pointer.field(field)) {
            Node::String(expected) => identity
                .get_key(field)
                .is_some_and(|value| value.as_str().is_some_and(|value| expected.is(value))),
            _ => false,
        },
    )
}
