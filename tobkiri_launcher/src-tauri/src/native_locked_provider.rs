//! Verify legacy Registry routes against the accepted v4 Bundle.

use super::super::{BundleEntryKind, VerifiedBundleLock, MAX_CANONICAL_JSON_BYTES};
use super::NativeApplicationSources;
use anyhow::{bail, ensure, Context, Result};
use std::{
    ffi::OsStr,
    os::unix::ffi::OsStrExt,
    path::{Path, PathBuf},
    sync::Arc,
};
use tobkiri_core::{
    read_relative_with, AllocationBudget, AllocationError, CanonError, CoreBuffer, CoreValue,
    Digest,
};
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
use tobkiri_harness_native::OriginalSavedLocalChatChoice;
use tobkiri_harness_native::{
    OriginalConversationAppendChoice, OriginalConversationConfirmationChoice,
    OriginalConversationReadSelection, OriginalModelResolveRole, OriginalModelResolveSelection,
    OriginalModelSaveSelection, OriginalModelSettingsSelection, OriginalProviderPrepareSelection,
    OriginalRoutePin, OriginalSavedReceiptRole, OriginalSavedReceiptSelection,
    OriginalSavedTurnSettlementChoice, OriginalSavedTurnStartChoice,
};

/// The exact Bundle entries to reread, captured while the resolver source is borrowed.
pub(crate) struct LockedRegistryBundle<'pin> {
    root: CoreBuffer,
    provider_prepare: Option<[LockedRegistryRoute<'pin>; 2]>,
    model_list: Option<LockedRegistryRoute<'pin>>,
    model_save: Option<[LockedRegistryRoute<'pin>; 2]>,
    model_resolve: [Option<LockedRegistryRoute<'pin>>; 4],
    conversation_read: [Option<LockedRegistryRoute<'pin>>; 3],
    saved_receipt: [Option<LockedRegistryRoute<'pin>>; 2],
    budget: Arc<dyn AllocationBudget>,
}

/// An original Shell's signed Turn owner routes, independent of Conversation grants.
pub(crate) struct LockedTurnSettlementBundle<'pin> {
    root: CoreBuffer,
    routes: [Option<LockedRegistryRoute<'pin>>; 2],
    budget: Arc<dyn AllocationBudget>,
}

/// Signed Bundle files for the exact ordinary Saved Turn and Lifecycle grants.
pub(crate) struct LockedTurnStartBundle<'pin> {
    application_root: CoreBuffer,
    bundle_root: CoreBuffer,
    bundle_lock_digest: Digest,
    routes: [LockedRegistryRoute<'pin>; 2],
    budget: Arc<dyn AllocationBudget>,
}

/// Owned evidence of the installed source whose signed bytes proved the start role.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedTurnStartBundle {
    application_root: PathBuf,
    bundle_root: PathBuf,
    bundle_lock_digest: Digest,
    pack: (String, Digest),
    catalog: (String, Digest),
}

/// Original signed Bundle documents for the independent completion routes.
pub(crate) struct LockedConversationConfirmationBundle<'pin> {
    root: CoreBuffer,
    routes: [Option<[LockedRegistryRoute<'pin>; 3]>; 2],
    budget: Arc<dyn AllocationBudget>,
}

/// Locked Bundle documents for ordinary saved orchestration and its append grant when selected.
pub(crate) struct LockedSavedOrchestrationBundle<'pin> {
    root: CoreBuffer,
    routes: [LockedRegistryRoute<'pin>; 2],
    message_manage: Option<LockedRegistryRoute<'pin>>,
    budget: Arc<dyn AllocationBudget>,
}

/// The installed documents for the independent Generate and Stream grants.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
pub(crate) struct LockedSavedLocalChatBundle<'pin> {
    source: VerifiedSavedLocalChatBundle,
    routes: [Option<[LockedRegistryRoute<'pin>; 5]>; 2],
    budget: Arc<dyn AllocationBudget>,
}

/// The exact installed source from which Launcher verified the saved local roles.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedSavedLocalChatBundle {
    application_root: PathBuf,
    bundle_root: PathBuf,
    bundle_lock_digest: Digest,
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl VerifiedSavedLocalChatBundle {
    /// Require the same resolver-owned roots and lock that selected the verified files.
    pub(crate) fn require_source(&self, sources: &NativeApplicationSources) -> Result<()> {
        ensure!(
            self.application_root == sources.application_root
                && self.bundle_root == sources.bundle_root
                && self.bundle_lock_digest.as_str() == sources.bundle_lock.digest,
            "signed local chat source differs from the admitted launch"
        );
        Ok(())
    }
}

impl NativeApplicationSources {
    /// Capture the Bundle files already pinned by the original signed resolver.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn saved_local_chat_bundle<'pin>(
        &self,
        selections: [Option<&'pin OriginalSavedLocalChatChoice<'_>>; 2],
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedSavedLocalChatBundle<'pin>> {
        let mut routes = [None, None];
        for (locked, selection) in routes.iter_mut().zip(selections) {
            let Some(selection) = selection else {
                continue;
            };
            let pins = selection.pins();
            ensure!(
                pins[0].caller_function_id() == self.authority.shell_provider_id
                    && pins
                        .windows(2)
                        .all(|pair| { pair[0].function_id() == pair[1].caller_function_id() }),
                "old local chat callers differ from the selected saved chain"
            );
            let [turn, orchestration, gateway, adapter, registry] = pins;
            let from_lock = |pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget);
            *locked = Some([
                from_lock(turn)?,
                from_lock(orchestration)?,
                from_lock(gateway)?,
                from_lock(adapter)?,
                from_lock(registry)?,
            ]);
        }
        ensure!(
            routes.iter().any(Option::is_some),
            "old local chat has no selected role"
        );
        Ok(LockedSavedLocalChatBundle {
            source: VerifiedSavedLocalChatBundle {
                application_root: self.application_root.clone(),
                bundle_root: self.bundle_root.clone(),
                bundle_lock_digest: Digest::parse(&self.bundle_lock.digest)?,
            },
            routes,
            budget: Arc::clone(budget),
        })
    }

    /// Capture the originally signed Turn Pack and catalog for both start grants.
    pub(crate) fn turn_start_bundle<'pin>(
        &self,
        selection: &'pin OriginalSavedTurnStartChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedTurnStartBundle<'pin>> {
        let [turn, lifecycle] = selection.pins();
        ensure!(
            turn.caller_function_id() == self.authority.shell_provider_id
                && lifecycle.caller_function_id() == turn.function_id(),
            "old Turn start callers differ from the selected Shell and Saved Turn"
        );
        Ok(LockedTurnStartBundle {
            application_root: CoreBuffer::copy_from(
                self.application_root.as_os_str().as_bytes(),
                budget,
            )?,
            bundle_root: CoreBuffer::copy_from(self.bundle_root.as_os_str().as_bytes(), budget)?,
            bundle_lock_digest: Digest::parse(&self.bundle_lock.digest)?,
            routes: [
                LockedRegistryRoute::from_lock(&self.bundle_lock, turn, budget)?,
                LockedRegistryRoute::from_lock(&self.bundle_lock, lifecycle, budget)?,
            ],
            budget: Arc::clone(budget),
        })
    }

    /// Capture the originally signed Turn Pack and catalog for each settlement grant.
    pub(crate) fn turn_settlement_bundle<'pin>(
        &self,
        selections: [Option<&'pin OriginalSavedTurnSettlementChoice<'_>>; 2],
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedTurnSettlementBundle<'pin>> {
        let mut routes = [None, None];
        for (route, selection) in routes.iter_mut().zip(selections) {
            if let Some(selection) = selection {
                let pin = selection.pin();
                ensure!(
                    pin.caller_function_id() == self.authority.shell_provider_id,
                    "old Turn settlement caller differs from the selected Shell"
                );
                *route = Some(LockedRegistryRoute::from_lock(
                    &self.bundle_lock,
                    pin,
                    budget,
                )?);
            }
        }
        Ok(LockedTurnSettlementBundle {
            root: CoreBuffer::copy_from(self.bundle_root.as_os_str().as_bytes(), budget)?,
            routes,
            budget: Arc::clone(budget),
        })
    }

    /// Capture locked Bundle documents for each granted completion chain.
    pub(crate) fn conversation_confirmation_bundle<'pin>(
        &self,
        selections: [Option<&'pin OriginalConversationConfirmationChoice<'_>>; 2],
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedConversationConfirmationBundle<'pin>> {
        let mut routes = [None, None];
        for (route, selection) in routes.iter_mut().zip(selections) {
            if let Some(selection) = selection {
                let turn_caller = selection.turn_caller();
                ensure!(
                    turn_caller.caller_function_id() == self.authority.shell_provider_id,
                    "old Conversation confirmation caller differs from the selected Shell"
                );
                *route = Some([
                    LockedRegistryRoute::from_lock(&self.bundle_lock, turn_caller, budget)?,
                    LockedRegistryRoute::from_lock(
                        &self.bundle_lock,
                        selection.completion(),
                        budget,
                    )?,
                    LockedRegistryRoute::from_lock(
                        &self.bundle_lock,
                        selection.turn_get(),
                        budget,
                    )?,
                ]);
            }
        }
        Ok(LockedConversationConfirmationBundle {
            root: CoreBuffer::copy_from(self.bundle_root.as_os_str().as_bytes(), budget)?,
            routes,
            budget: Arc::clone(budget),
        })
    }

    /// Capture the locked Turn, Saved Orchestration, and Message Manage documents.
    pub(crate) fn conversation_append_bundle<'pin>(
        &self,
        selection: &'pin OriginalConversationAppendChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedSavedOrchestrationBundle<'pin>> {
        let [turn, orchestration, manage] = selection.pins();
        self.saved_orchestration_bundle([turn, orchestration], Some(manage), budget)
    }

    /// Capture locked documents from the selected ordinary saved orchestration pins.
    /// A saved append also supplies its independently selected Message Manage pin.
    pub(crate) fn saved_orchestration_bundle<'pin>(
        &self,
        [turn, orchestration]: [&'pin OriginalRoutePin; 2],
        message_manage: Option<&'pin OriginalRoutePin>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedSavedOrchestrationBundle<'pin>> {
        ensure!(
            turn.caller_function_id() == self.authority.shell_provider_id,
            "old Saved Turn caller differs from the selected Shell"
        );
        let routes = [
            LockedRegistryRoute::from_lock(&self.bundle_lock, turn, budget)?,
            LockedRegistryRoute::from_lock(&self.bundle_lock, orchestration, budget)?,
        ];
        let message_manage = message_manage
            .map(|pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget))
            .transpose()?;
        Ok(LockedSavedOrchestrationBundle {
            root: CoreBuffer::copy_from(self.bundle_root.as_os_str().as_bytes(), budget)?,
            routes,
            message_manage,
            budget: Arc::clone(budget),
        })
    }

    /// Capture Bundle-relative file identities from the original verified lock.
    /// File reads happen after the Core source borrow has ended.
    pub(crate) fn registry_bundle<'pin>(
        &self,
        provider: Option<&'pin OriginalProviderPrepareSelection<'_>>,
        model: Option<&'pin OriginalModelSettingsSelection<'_>>,
        model_save: Option<&'pin OriginalModelSaveSelection<'_>>,
        model_resolve: Option<&'pin OriginalModelResolveSelection<'_>>,
        (conversation_read, saved_receipt): (
            Option<&'pin OriginalConversationReadSelection<'_>>,
            Option<&'pin OriginalSavedReceiptSelection<'_>>,
        ),
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedRegistryBundle<'pin>> {
        let provider_prepare = provider
            .map(|selection| {
                ensure!(
                    selection.coordinator().caller_function_id()
                        == self.authority.shell_provider_id,
                    "old Provider prepare coordinator differs from the selected Shell"
                );
                Ok::<_, anyhow::Error>([
                    LockedRegistryRoute::from_lock(
                        &self.bundle_lock,
                        selection.coordinator(),
                        budget,
                    )?,
                    LockedRegistryRoute::from_lock(
                        &self.bundle_lock,
                        selection.provider(),
                        budget,
                    )?,
                ])
            })
            .transpose()?;
        let model_list = model
            .map(|selection| {
                ensure!(
                    selection.model_list().caller_function_id() == self.authority.shell_provider_id,
                    "old Model list caller differs from the selected Shell"
                );
                LockedRegistryRoute::from_lock(&self.bundle_lock, selection.model_list(), budget)
            })
            .transpose()?;
        let model_save = model_save
            .map(|selection| {
                let model_manage = selection.model_manage();
                let provider_read = selection.provider_read();
                ensure!(
                    model_manage.caller_function_id() == self.authority.shell_provider_id,
                    "old Model manage caller differs from the selected Shell"
                );
                ensure!(
                    provider_read.caller_function_id() == model_manage.function_id(),
                    "old Model save Provider read caller differs from Model manage"
                );
                Ok::<_, anyhow::Error>([
                    LockedRegistryRoute::from_lock(&self.bundle_lock, model_manage, budget)?,
                    LockedRegistryRoute::from_lock(&self.bundle_lock, provider_read, budget)?,
                ])
            })
            .transpose()?;
        let mut model_resolve_routes = [None, None, None, None];
        if let Some(selection) = model_resolve {
            use OriginalModelResolveRole::{Generate, QuoteGenerate, QuoteStream, Stream};
            for (locked, role) in
                model_resolve_routes
                    .iter_mut()
                    .zip([QuoteGenerate, QuoteStream, Generate, Stream])
            {
                *locked = selection
                    .pin(role)
                    .map(|pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget))
                    .transpose()?;
            }
        }
        let mut conversation_routes = [None, None, None];
        if let Some(selection) = conversation_read {
            let shell = selection.shell();
            if let Some(shell) = shell {
                ensure!(
                    shell.caller_function_id() == self.authority.shell_provider_id,
                    "old Conversation read caller differs from the selected Shell"
                );
            }
            for (locked, pin) in conversation_routes.iter_mut().zip([
                shell,
                selection.saved_turn(),
                selection.saved_orchestration(),
            ]) {
                *locked = pin
                    .map(|pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget))
                    .transpose()?;
            }
        }
        let mut receipt_routes = [None, None];
        if let Some(selection) = saved_receipt {
            for (locked, role) in receipt_routes.iter_mut().zip(OriginalSavedReceiptRole::ALL) {
                *locked = selection
                    .pin(role)
                    .map(|pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget))
                    .transpose()?;
            }
        }
        Ok(LockedRegistryBundle {
            root: CoreBuffer::copy_from(self.bundle_root.as_os_str().as_bytes(), budget)?,
            provider_prepare,
            model_list,
            model_save,
            model_resolve: model_resolve_routes,
            conversation_read: conversation_routes,
            saved_receipt: receipt_routes,
            budget: Arc::clone(budget),
        })
    }
}

impl LockedRegistryBundle<'_> {
    /// Reread and confirm the locked Pack and executable catalog for each pin.
    pub(crate) fn verify(&self, mut checkpoint: impl FnMut() -> Result<()>) -> Result<()> {
        let root = Path::new(OsStr::from_bytes(self.root.as_bytes()));
        if let Some([coordinator, provider]) = &self.provider_prepare {
            coordinator.verify(root, &self.budget, &mut checkpoint, None)?;
            provider.verify(root, &self.budget, &mut checkpoint, None)?;
        }
        if let Some(model_list) = &self.model_list {
            model_list.verify(root, &self.budget, &mut checkpoint, None)?;
        }
        if let Some([model_manage, provider_read]) = &self.model_save {
            model_manage.verify(root, &self.budget, &mut checkpoint, None)?;
            provider_read.verify(root, &self.budget, &mut checkpoint, None)?;
        }
        let mut model_resolve = self.model_resolve.iter().flatten();
        if let Some(first) = model_resolve.next() {
            // Every role targets the same locked Model Pack. Check each signed pin
            // against that Pack and catalog, including the role's own operation.
            let pack = first.pack.read(root, &self.budget, &mut checkpoint)?;
            let catalog = first.catalog.read(root, &self.budget, &mut checkpoint)?;
            for route in std::iter::once(first).chain(model_resolve) {
                checkpoint()?;
                route.verify_documents(&pack, &catalog, Some("read"))?;
            }
        }
        verify_privileged_routes(
            self.conversation_read
                .iter()
                .chain(self.saved_receipt.iter())
                .flatten(),
            root,
            &self.budget,
            &mut checkpoint,
        )?;
        checkpoint()
    }
}

impl LockedTurnSettlementBundle<'_> {
    /// Verify the original Turn Pack and both independently granted operations.
    pub(crate) fn verify(&self, mut checkpoint: impl FnMut() -> Result<()>) -> Result<()> {
        let root = Path::new(OsStr::from_bytes(self.root.as_bytes()));
        verify_privileged_routes(
            self.routes.iter().flatten(),
            root,
            &self.budget,
            &mut checkpoint,
        )?;
        checkpoint()
    }
}

impl LockedTurnStartBundle<'_> {
    /// Verify both Python variants and their privileged operations before detaching evidence.
    pub(crate) fn verify(
        &self,
        mut checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<VerifiedTurnStartBundle> {
        let bundle_root = Path::new(OsStr::from_bytes(self.bundle_root.as_bytes()));
        verify_privileged_routes(
            self.routes.iter(),
            bundle_root,
            &self.budget,
            &mut checkpoint,
        )?;
        checkpoint()?;
        let [turn, _] = &self.routes;
        Ok(VerifiedTurnStartBundle {
            application_root: Path::new(OsStr::from_bytes(self.application_root.as_bytes())).into(),
            bundle_root: bundle_root.into(),
            bundle_lock_digest: self.bundle_lock_digest.clone(),
            pack: (
                turn.pack.path.as_str()?.to_owned(),
                turn.pack.digest.clone(),
            ),
            catalog: (
                turn.catalog.path.as_str()?.to_owned(),
                turn.catalog.digest.clone(),
            ),
        })
    }
}

impl VerifiedTurnStartBundle {
    /// Bind the current signed resolution to the exact installed source used for admission.
    pub(crate) fn require_source(&self, sources: &NativeApplicationSources) -> Result<()> {
        let lock = &sources.bundle_lock;
        let (pack_path, pack_digest) = &self.pack;
        let (catalog_path, catalog_digest) = &self.catalog;
        ensure!(
            self.application_root == sources.application_root
                && self.bundle_root == sources.bundle_root
                && self.bundle_lock_digest.as_str() == lock.digest
                && sources.authority.shell_provider_id == "shell.tauri.default"
                && lock.pack_paths.get("rumi_turn_runtime_pack") == Some(pack_path)
                && lock.authority_roles.get(pack_path) == Some(&BundleEntryKind::Pack)
                && lock.authority_digests.get(pack_path).map(String::as_str)
                    == Some(pack_digest.as_str())
                && lock.sidecar_digests.get(catalog_path).map(String::as_str)
                    == Some(catalog_digest.as_str()),
            "signed Turn start source differs from the admitted launch"
        );
        Ok(())
    }

    /// Recheck the same locked bytes that established both privileged operation identities.
    pub(crate) fn revalidate(
        &self,
        budget: &Arc<dyn AllocationBudget>,
        mut checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<()> {
        let documents = [
            ("bundle.lock.json", &self.bundle_lock_digest),
            (self.pack.0.as_str(), &self.pack.1),
            (self.catalog.0.as_str(), &self.catalog.1),
        ];
        for (path, digest) in documents {
            LockedDocument::new(path, digest.as_str(), budget)?.read_bytes(
                &self.bundle_root,
                budget,
                &mut checkpoint,
            )?;
        }
        checkpoint()
    }

    #[cfg(all(test, target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn fixture() -> Self {
        Self {
            application_root: PathBuf::new(),
            bundle_root: PathBuf::new(),
            bundle_lock_digest: Digest::hash(b"test Bundle lock"),
            pack: (String::new(), Digest::hash(b"test Turn Pack")),
            catalog: (String::new(), Digest::hash(b"test Turn catalog")),
        }
    }
}

impl LockedConversationConfirmationBundle<'_> {
    /// Verify each original variant and its catalog's effect class.
    pub(crate) fn verify(&self, mut checkpoint: impl FnMut() -> Result<()>) -> Result<()> {
        let root = Path::new(OsStr::from_bytes(self.root.as_bytes()));
        // Turn Resource is catalogued as privileged at the operation level.
        // The captured Completion client restricts its use to GET.
        verify_privileged_routes(
            self.routes
                .iter()
                .flatten()
                .flat_map(|[turn, _, get]| [turn, get]),
            root,
            &self.budget,
            &mut checkpoint,
        )?;
        verify_privileged_routes(
            self.routes
                .iter()
                .flatten()
                .map(|[_, completion, _]| completion),
            root,
            &self.budget,
            &mut checkpoint,
        )?;
        checkpoint()
    }
}

impl LockedSavedOrchestrationBundle<'_> {
    /// Verify the original privileged Turn, pure Orchestration, and selected append operations.
    pub(crate) fn verify(&self, mut checkpoint: impl FnMut() -> Result<()>) -> Result<()> {
        let root = Path::new(OsStr::from_bytes(self.root.as_bytes()));
        let [turn, orchestration] = &self.routes;
        turn.verify(root, &self.budget, &mut checkpoint, Some("privileged"))?;
        orchestration.verify(root, &self.budget, &mut checkpoint, Some("pure"))?;
        if let Some(manage) = &self.message_manage {
            manage.verify(root, &self.budget, &mut checkpoint, Some("privileged"))?;
        }
        checkpoint()
    }
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl LockedSavedLocalChatBundle<'_> {
    /// Verify each role against the same bytes for each of the five selected Packs.
    pub(crate) fn verify(
        self,
        mut checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<VerifiedSavedLocalChatBundle> {
        let effects = [
            "privileged",
            "pure",
            "privileged",
            "privileged",
            "privileged",
        ];
        for (index, effect) in effects.into_iter().enumerate() {
            verify_locked_routes(
                self.routes.iter().flatten().map(|routes| &routes[index]),
                &self.source.bundle_root,
                &self.budget,
                &mut checkpoint,
                effect,
            )?;
        }
        checkpoint()?;
        Ok(self.source)
    }
}

fn verify_privileged_routes<'a>(
    routes: impl Iterator<Item = &'a LockedRegistryRoute<'a>>,
    root: &Path,
    budget: &Arc<dyn AllocationBudget>,
    checkpoint: &mut impl FnMut() -> Result<()>,
) -> Result<()> {
    verify_locked_routes(routes, root, budget, checkpoint, "privileged")
}

fn verify_locked_routes<'a>(
    mut routes: impl Iterator<Item = &'a LockedRegistryRoute<'a>>,
    root: &Path,
    budget: &Arc<dyn AllocationBudget>,
    checkpoint: &mut impl FnMut() -> Result<()>,
    effect: &str,
) -> Result<()> {
    if let Some(first) = routes.next() {
        let pack = first.pack.read(root, budget, checkpoint)?;
        let catalog = first.catalog.read(root, budget, checkpoint)?;
        for route in std::iter::once(first).chain(routes) {
            checkpoint()?;
            ensure!(
                route.pack.digest == first.pack.digest
                    && route.catalog.digest == first.catalog.digest,
                "legacy Registry routes select different Bundle documents"
            );
            route.verify_documents(&pack, &catalog, Some(effect))?;
        }
    }
    Ok(())
}

struct LockedRegistryRoute<'pin> {
    pin: &'pin OriginalRoutePin,
    pack: LockedDocument,
    catalog: LockedDocument,
}

impl<'pin> LockedRegistryRoute<'pin> {
    fn from_lock(
        lock: &VerifiedBundleLock,
        pin: &'pin OriginalRoutePin,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self> {
        let pack_id = pin.pack_id();
        let path = lock
            .pack_paths
            .get(pack_id)
            .filter(|path| lock.authority_roles.get(*path) == Some(&BundleEntryKind::Pack))
            .context("legacy Registry Pack is absent from the accepted Bundle authority")?;
        let pack_digest = lock
            .authority_digests
            .get(path)
            .context("legacy Registry Pack is missing its accepted Bundle digest")?;
        let (catalog_path, catalog_digest) = lock
            .sidecar_digests
            .iter()
            .find(|(path, _)| {
                // The Bundle verifier already enforced each catalog's path and Pack identity.
                path.strip_prefix("packs/")
                    .and_then(|name| name.strip_suffix(".executables.v4.json"))
                    == Some(pack_id)
            })
            .context("legacy Registry executable catalog is absent from the accepted Bundle")?;
        Ok(Self {
            pin,
            pack: LockedDocument::new(path, pack_digest, budget)?,
            catalog: LockedDocument::new(catalog_path, catalog_digest, budget)?,
        })
    }

    fn verify(
        &self,
        root: &Path,
        budget: &Arc<dyn AllocationBudget>,
        checkpoint: &mut impl FnMut() -> Result<()>,
        effect_class: Option<&str>,
    ) -> Result<()> {
        let pack = self.pack.read(root, budget, checkpoint)?;
        let catalog = self.catalog.read(root, budget, checkpoint)?;
        self.verify_documents(&pack, &catalog, effect_class)
    }

    fn verify_documents(
        &self,
        pack: &CoreValue,
        catalog: &CoreValue,
        effect_class: Option<&str>,
    ) -> Result<()> {
        let pin = self.pin;
        let pack_identity = pack
            .get_key("pack")
            .context("missing locked Pack identity")?;
        let integrity = pack
            .get_key("integrity")
            .context("missing locked Pack integrity")?;
        let source_identity = integrity.get_key("source_identity");
        // The original Bundle verifier checked the catalog's closed schema and digest,
        // and only allowed a materialization alias on source-bound projections.
        let canonical_catalog_digest = catalog
            .get_key("materialization_catalog_digest")
            .filter(|value| value.as_str().is_some())
            .or_else(|| catalog.get_key("catalog_digest"));
        ensure!(
            text_matches(&pack_identity, "id", pin.pack_id())
                && text_matches(&pack_identity, "artifact_digest", pin.artifact_digest())
                && text_matches(catalog, "pack_id", pin.pack_id())
                && source_identity
                    .as_ref()
                    .and_then(CoreValue::as_str)
                    .is_some_and(|source| { text_matches(catalog, "source_identity", source) })
                && canonical_catalog_digest
                    .as_ref()
                    .and_then(CoreValue::as_str)
                    == Some(pin.executable_catalog_digest()),
            "legacy Registry Pack or canonical catalog identity differs from the selected pin"
        );
        let sidecar = unique_row(pack, "artifacts", "path", "executables.v4.json")?;
        ensure!(
            text_matches(&sidecar, "kind", "sidecar")
                && text_matches(&sidecar, "digest", self.catalog.digest.as_str()),
            "legacy Registry Pack does not commit the selected locked catalog bytes"
        );

        let function = unique_row(pack, "functions", "id", pin.function_id())?;
        let provider = unique_row(pack, "provider_catalog", "provider_id", pin.function_id())?;
        let operation = unique_row(
            pack,
            "operation_catalog",
            "operation_id",
            pin.operation_id(),
        )?;
        let contract = unique_row(pack, "contracts", "contract_id", pin.contract_id())?;
        let variant = unique_row(catalog, "variants", "variant_id", pin.variant_id())?;
        let variant_operation =
            unique_row(&variant, "operations", "operation_id", pin.operation_id())?;
        if let Some(effect_class) = effect_class {
            ensure!(
                text_matches(&variant_operation, "effect_class", effect_class),
                "legacy Registry executable operation effect differs from the selected route"
            );
        }
        let implementation_path = variant.get_key("implementation_path");
        let implementation = unique_row(
            pack,
            "artifacts",
            "path",
            implementation_path
                .as_ref()
                .and_then(CoreValue::as_str)
                .context("missing locked executable path")?,
        )?;

        let matches = |row: &CoreValue, fields: &[(&str, &str)]| {
            fields
                .iter()
                .all(|(key, expected)| text_matches(row, key, expected))
        };
        let operation_id = pin.operation_id();
        let contract_id = pin.contract_id();
        let revision = pin.contract_revision_digest();
        let implementation_digest = pin.function_implementation_digest();
        ensure!(
            matches(
                &function,
                &[
                    ("implementation_digest", implementation_digest),
                    ("contract_revision_digest", revision),
                    ("isolation", pin.domain_kind()),
                ]
            ) && matches(
                &provider,
                &[
                    ("owner", pin.pack_id()),
                    ("contract_reference", contract_id)
                ]
            ) && matches(
                &operation,
                &[
                    ("owner", pin.pack_id()),
                    ("contract_reference", contract_id),
                    ("provider_id", pin.function_id()),
                ]
            ) && matches(
                &variant,
                &[
                    ("function_id", pin.function_id()),
                    ("implementation_digest", implementation_digest),
                    ("platform", pin.platform()),
                    ("architecture", pin.architecture()),
                    ("runtime_abi", pin.runtime_abi()),
                    ("backend", pin.backend()),
                    ("execution_kind", pin.execution_kind()),
                ]
            ) && matches(
                &implementation,
                &[("kind", "executable"), ("digest", implementation_digest)]
            ) && matches(
                &variant_operation,
                &[("contract_id", contract_id), ("revision_digest", revision)]
            ) && text_matches(&contract, "revision_digest", revision)
                && [&function, &provider, &contract].into_iter().all(|row| {
                    row.get_key("operations").and_then(|value| {
                        Some(
                            value
                                .array()?
                                .iter()
                                .filter(|item| item.as_str() == Some(operation_id))
                                .count(),
                        )
                    }) == Some(1)
                }),
            "legacy Registry Pack or variant does not match the selected function and operation"
        );
        Ok(())
    }
}

struct LockedDocument {
    path: CoreBuffer,
    digest: Digest,
}

impl LockedDocument {
    fn new(path: &str, digest: &str, budget: &Arc<dyn AllocationBudget>) -> Result<Self> {
        Ok(Self {
            path: CoreBuffer::copy_from(path.as_bytes(), budget)?,
            digest: Digest::parse(digest)?,
        })
    }

    fn read(
        &self,
        root: &Path,
        budget: &Arc<dyn AllocationBudget>,
        checkpoint: &mut impl FnMut() -> Result<()>,
    ) -> Result<CoreValue> {
        let path = self.path.as_str()?;
        let bytes = self.read_bytes(root, budget, checkpoint)?;
        CoreValue::parse_guest(bytes.as_bytes(), budget)
            .with_context(|| format!("could not decode locked Bundle entry {path}"))
    }

    fn read_bytes(
        &self,
        root: &Path,
        budget: &Arc<dyn AllocationBudget>,
        checkpoint: &mut impl FnMut() -> Result<()>,
    ) -> Result<CoreBuffer> {
        checkpoint()?;
        let path = self.path.as_str()?;
        let mut failure = None;
        let bytes = read_relative_with(
            root,
            path,
            MAX_CANONICAL_JSON_BYTES as u64,
            |reader, size| {
                let bytes = CoreBuffer::write_exact(size as usize, budget, |target| {
                    for chunk in target.chunks_mut(8192) {
                        if let Err(error) =
                            checkpoint().and_then(|()| reader.read_exact(chunk).map_err(Into::into))
                        {
                            failure = Some(error);
                            return Err(AllocationError::Invalid);
                        }
                    }
                    Ok(())
                })
                .map_err(|_| CanonError::Limit("legacy Registry Bundle input"))?;
                Ok(bytes)
            },
        );
        if let Some(error) = failure {
            return Err(error);
        }
        let bytes = bytes.with_context(|| format!("could not read locked Bundle entry {path}"))?;
        checkpoint()?;
        ensure!(
            Digest::hash(bytes.as_bytes()) == self.digest,
            "locked Bundle digest changed: {path}"
        );
        Ok(bytes)
    }
}

fn text_matches(value: &CoreValue, key: &str, expected: &str) -> bool {
    value.get_key(key).as_ref().and_then(CoreValue::as_str) == Some(expected)
}

fn unique_row(value: &CoreValue, list: &str, key: &str, id: &str) -> Result<CoreValue> {
    let rows = value
        .get_key(list)
        .with_context(|| format!("missing locked {list}"))?;
    let rows = rows
        .array()
        .with_context(|| format!("invalid locked {list}"))?;
    let mut rows = rows.iter().filter(|row| text_matches(row, key, id));
    let row = rows
        .next()
        .with_context(|| format!("missing selected row in locked {list}"))?;
    if rows.next().is_some() {
        bail!("ambiguous selected row in locked {list}");
    }
    Ok(row)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::symlink, time::SystemTime};
    use tobkiri_core::AllocationRequest;

    struct TestBudget;

    impl AllocationBudget for TestBudget {
        fn reserve(&self, _: AllocationRequest) -> Result<(), AllocationError> {
            Ok(())
        }

        fn release(&self, _: u64) {}
    }

    #[test]
    fn locked_document_rejects_replacement_and_preserves_checkpoint_failure() {
        let unique = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "tobkiri-locked-provider-{}-{unique}",
            std::process::id()
        ));
        fs::create_dir(&root).unwrap();
        let path = root.join("selected.json");
        let original = br#"{"pin":"original"}"#;
        fs::write(&path, original).unwrap();
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let document =
            LockedDocument::new("selected.json", Digest::hash(original).as_str(), &budget).unwrap();

        let value = document.read(&root, &budget, &mut || Ok(())).unwrap();
        assert!(text_matches(&value, "pin", "original"));
        let mut checks = 0;
        let cancelled = document.read(&root, &budget, &mut || {
            checks += 1;
            ensure!(checks < 2, "inspection cancelled");
            Ok(())
        });
        assert_eq!(cancelled.unwrap_err().to_string(), "inspection cancelled");

        fs::write(&path, br#"{"pin":"modified"}"#).unwrap();
        assert!(document
            .read(&root, &budget, &mut || Ok(()))
            .unwrap_err()
            .to_string()
            .contains("digest changed"));
        let other = root.join("other.json");
        fs::write(&other, original).unwrap();
        fs::remove_file(&path).unwrap();
        symlink(&other, &path).unwrap();
        let symlink = document.read(&root, &budget, &mut || Ok(()));
        fs::remove_dir_all(&root).unwrap();
        assert!(symlink.is_err());
    }
}
