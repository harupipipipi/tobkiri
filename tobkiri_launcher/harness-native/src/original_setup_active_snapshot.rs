//! Load the registered Setup predecessor on the original native owner.
use super::*;
use crate::original_authority::catalog::CatalogIo;
use std::sync::Mutex;
use workspace_active::{ActiveSnapshot, ActiveSnapshotRead, ProfileCheck, SnapshotProjection};

#[derive(Debug)]
pub(crate) enum RegisteredSnapshotReached {
    CatalogPolicy {
        registered_profile: CoreValue,
        active_profile: CoreValue,
        active_plan: Option<CoreValue>,
        verified_definition_digest: CoreValue,
    },
    LegacySuccessor,
    SnapshotSchemas,
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
            }
            .load_snapshot()
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
            Snapshot::Verified {
                identity,
                active_profile,
                active_plan,
            } => {
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
                Ok(RegisteredSnapshotReached::CatalogPolicy {
                    registered_profile: candidate.registered.review_profile(&op, budget)?,
                    active_profile,
                    active_plan,
                    verified_definition_digest,
                })
            }
            Snapshot::Boundary(reached) => Ok(reached),
        }
    }
}

enum Snapshot {
    Verified {
        identity: CoreValue,
        active_profile: CoreValue,
        active_plan: Option<CoreValue>,
    },
    Boundary(RegisteredSnapshotReached),
}

impl Snapshot {
    fn verified(mut active: ActiveSnapshot, verification: ActiveArtifactVerification) -> Self {
        let records = active
            .review_records
            .take()
            .expect("first registered Setup snapshot has review records");
        Self::Verified {
            identity: active.into_candidate(),
            active_profile: records.profile,
            // The original reconfirmation error carries the Profile and
            // identities. The old Plan participates only on the current path.
            active_plan: (verification == ActiveArtifactVerification::Current)
                .then_some(records.plan),
        }
    }
}

struct RegisteredActivation<'a, 'op, 'host, S> {
    op: &'op Operation<'host, S>,
    store: &'a Store<'op, 'host, S>,
    workspace: RecoveryWorkspace,
    budget: &'a Arc<dyn AllocationBudget>,
    loader: &'a Mutex<Option<CatalogIo>>,
    profile_id: &'a str,
    schemas: Option<BoundProfileSchemas>,
}

impl<S: crate::BootstrapReviewOwner> RegisteredActivation<'_, '_, '_, S> {
    async fn load_snapshot(&self) -> Result<Snapshot> {
        loop {
            let Some(first) =
                self.resume(self.read(SnapshotProjection::RegisteredReview).await?)?
            else {
                continue;
            };
            let Some(active) = first.current else {
                return Ok(Self::boundary(first));
            };
            // Hash outside the workspace lock. The store remains entered.
            let verified = self.with_catalog(|io| {
                io.verify_bundled_active_artifact(
                    self.op,
                    &active.artifact_profile,
                    self.profile_id,
                )
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
            ProfileCheck::NeedsLegacySuccessor => RegisteredSnapshotReached::LegacySuccessor,
            _ => RegisteredSnapshotReached::SnapshotSchemas,
        })
    }

    async fn read(
        &self,
        projection: SnapshotProjection,
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
                let identity = self.with_catalog(|io| {
                    io.verify_bundled_pending_artifact(self.op, &pending.profile, self.profile_id)
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
