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
use tobkiri_harness_native::{
    OriginalAiRoutingSelection, OriginalApplicationPresentationChoice,
    OriginalConversationAppendChoice, OriginalConversationCompleteChoice, OriginalFailoverChoice,
    OriginalRequestPrepareChoice, OriginalSavedGatewayChoice, OriginalSavedLocalChatChoice,
    OriginalSideChatSavedReceiptChoice, OriginalStreamResultSelection, OriginalToolIntentsChoice,
    OriginalUsageCostChoice, CONVERSATION_COMPLETE_APPLICATION_ID,
};
use tobkiri_harness_native::{
    OriginalConversationConfirmationChoice, OriginalConversationReadSelection,
    OriginalModelResolveRole, OriginalModelResolveSelection, OriginalModelSaveSelection,
    OriginalModelSettingsSelection, OriginalProviderPrepareSelection, OriginalRoutePin,
    OriginalSavedReceiptRole, OriginalSavedReceiptSelection, OriginalSavedTurnSettlementChoice,
    OriginalSavedTurnStartChoice,
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

/// Signed Bundle files for the exact Turn settlement chains.
pub(crate) struct LockedTurnSettlementBundle<'pin> {
    application_root: CoreBuffer,
    bundle_root: CoreBuffer,
    bundle_lock_digest: Digest,
    routes: [Option<LockedTurnSettlementRoute<'pin>>; 4],
    budget: Arc<dyn AllocationBudget>,
}

struct LockedTurnSettlementRoute<'pin> {
    side_chat: Option<LockedRegistryRoute<'pin>>,
    turn: LockedRegistryRoute<'pin>,
}

/// Signed Bundle files for one exact Saved Turn start chain.
pub(crate) struct LockedTurnStartBundle<'pin> {
    application_root: CoreBuffer,
    bundle_root: CoreBuffer,
    bundle_lock_digest: Digest,
    side_chat: Option<LockedRegistryRoute<'pin>>,
    routes: [LockedRegistryRoute<'pin>; 2],
    budget: Arc<dyn AllocationBudget>,
}

/// Owned evidence of the installed source whose signed bytes proved the Turn entry point.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedTurnEntryBundle {
    application_root: PathBuf,
    bundle_root: PathBuf,
    bundle_lock_digest: Digest,
    side_chat: Option<VerifiedTurnEntryDocuments>,
    turn: VerifiedTurnEntryDocuments,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedTurnEntryDocuments {
    pack: (String, Digest),
    catalog: (String, Digest),
}

/// Original signed Bundle documents for the independent completion routes.
pub(crate) struct LockedConversationConfirmationBundle<'pin> {
    root: CoreBuffer,
    routes: [Option<[LockedRegistryRoute<'pin>; 3]>; 2],
    budget: Arc<dyn AllocationBudget>,
}

/// Locked Bundle documents for one Side Chat confirmation caller.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
pub(crate) struct LockedSideChatConfirmationBundle<'pin> {
    source: VerifiedOrdinarySavedBundle,
    routes: [LockedRegistryRoute<'pin>; 4],
    budget: Arc<dyn AllocationBudget>,
}

/// Locked Bundle documents for ordinary saved orchestration.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
pub(crate) struct LockedSavedOrchestrationBundle<'pin> {
    root: CoreBuffer,
    routes: [LockedRegistryRoute<'pin>; 2],
    budget: Arc<dyn AllocationBudget>,
}

/// Locked Bundle documents for the ordinary or Side Chat saved append route.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
pub(crate) struct LockedConversationAppendBundle<'pin> {
    source: VerifiedOrdinarySavedBundle,
    side_chat: Option<LockedRegistryRoute<'pin>>,
    routes: [LockedRegistryRoute<'pin>; 3],
    budget: Arc<dyn AllocationBudget>,
}

/// Locked Bundle documents for one Side Chat saved receipt caller.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
pub(crate) struct LockedSideChatSavedReceiptBundle<'pin> {
    source: VerifiedOrdinarySavedBundle,
    routes: [LockedRegistryRoute<'pin>; 3],
    budget: Arc<dyn AllocationBudget>,
}

/// The installed documents for the independent Generate and Stream grants.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
pub(crate) struct LockedSavedLocalChatBundle<'pin> {
    source: VerifiedOrdinarySavedBundle,
    routes: [Option<[LockedRegistryRoute<'pin>; 5]>; 2],
    budget: Arc<dyn AllocationBudget>,
}

/// Signed Bundle documents for an ordinary Gateway call and its selected AI operation.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
pub(crate) struct LockedAiRoutingBundle<'pin, const ROLES: usize, const PINS: usize = 4> {
    source: VerifiedOrdinarySavedBundle,
    routes: [Option<[LockedRegistryRoute<'pin>; PINS]>; ROLES],
    budget: Arc<dyn AllocationBudget>,
}

/// The signed Pack and executable catalog for both presentation grants.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
pub(crate) struct LockedApplicationPresentationBundle<'pin> {
    source: VerifiedOrdinarySavedBundle,
    routes: [LockedRegistryRoute<'pin>; 2],
    budget: Arc<dyn AllocationBudget>,
}

/// The signed Shell, Application, Conversation, and Gateway documents.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
pub(crate) struct LockedConversationCompleteBundle<'pin> {
    source: VerifiedOrdinarySavedBundle,
    routes: [LockedRegistryRoute<'pin>; 2],
    shell_pack: LockedDocument,
    application_pack: LockedDocument,
    choice: &'pin OriginalConversationCompleteChoice<'pin>,
    budget: Arc<dyn AllocationBudget>,
}

/// The exact installed source from which Launcher verified a signed saved route.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedOrdinarySavedBundle {
    application_root: PathBuf,
    bundle_root: PathBuf,
    bundle_lock_digest: Digest,
    documents: Vec<(String, Digest)>,
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl VerifiedOrdinarySavedBundle {
    fn new(sources: &NativeApplicationSources) -> Result<Self> {
        Ok(Self {
            application_root: sources.application_root.clone(),
            bundle_root: sources.bundle_root.clone(),
            bundle_lock_digest: Digest::parse(&sources.bundle_lock.digest)?,
            documents: Vec::new(),
        })
    }

    #[cfg(test)]
    pub(crate) fn fixture() -> Self {
        Self {
            application_root: PathBuf::new(),
            bundle_root: PathBuf::new(),
            bundle_lock_digest: Digest::hash(b"test"),
            documents: Vec::new(),
        }
    }

    /// Require the same resolver-owned roots and lock that selected the verified files.
    pub(crate) fn require_source(&self, sources: &NativeApplicationSources) -> Result<()> {
        ensure!(
            self.application_root == sources.application_root
                && self.bundle_root == sources.bundle_root
                && self.bundle_lock_digest.as_str() == sources.bundle_lock.digest,
            "signed saved source differs from the admitted launch"
        );
        Ok(())
    }

    /// Prove the same bytes are still installed before dispensing a dispatch permit.
    pub(crate) fn revalidate(
        &self,
        budget: &Arc<dyn AllocationBudget>,
        mut checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<()> {
        let documents = std::iter::once(("bundle.lock.json", &self.bundle_lock_digest)).chain(
            self.documents
                .iter()
                .map(|(path, digest)| (path.as_str(), digest)),
        );
        for (path, digest) in documents {
            LockedDocument::new(path, digest.as_str(), budget)?.read_bytes(
                &self.bundle_root,
                budget,
                &mut checkpoint,
            )?;
        }
        checkpoint()
    }
}

impl NativeApplicationSources {
    /// Capture the installed documents behind the selected presentation caller.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn application_presentation_bundle<'pin>(
        &self,
        choice: &'pin OriginalApplicationPresentationChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<LockedApplicationPresentationBundle<'pin>>> {
        let [caller, presentation] = choice.pins();
        if !self.owns_runtime_pack(presentation.pack_id()) {
            return Ok(None);
        }
        ensure!(
            caller.caller_function_id() == self.authority.shell_provider_id
                && self.authority.shell_provider_id == "shell.tauri.default"
                && presentation.caller_function_id() == caller.function_id(),
            "application presentation callers differ from the selected Shell and caller chain"
        );
        let from_lock = |pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget);
        Ok(Some(LockedApplicationPresentationBundle {
            source: VerifiedOrdinarySavedBundle::new(self)?,
            routes: [from_lock(caller)?, from_lock(presentation)?],
            budget: Arc::clone(budget),
        }))
    }

    /// Capture the original owned Conversation operation and its Gateway grant.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn conversation_complete_bundle<'pin>(
        &self,
        choice: &'pin OriginalConversationCompleteChoice<'pin>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<LockedConversationCompleteBundle<'pin>>> {
        let [conversation, gateway] = choice.pins();
        if !self.owns_runtime_pack(conversation.pack_id())
            || self.authority.application_id != CONVERSATION_COMPLETE_APPLICATION_ID
        {
            return Ok(None);
        }
        let shell_id = self.authority.shell_provider_id.as_str();
        ensure!(
            shell_id == "shell.tauri.default"
                && conversation.caller_function_id() == shell_id
                && gateway.caller_function_id() == conversation.function_id()
                && self.authority.launch.provider_id == CONVERSATION_COMPLETE_APPLICATION_ID
                && self.authority.launch.entrypoint_digest == choice.application_artifacts()[1],
            "Conversation Complete differs from the selected Shell and Application launch"
        );
        let lock = &self.bundle_lock;
        Ok(Some(LockedConversationCompleteBundle {
            source: VerifiedOrdinarySavedBundle::new(self)?,
            routes: [
                LockedRegistryRoute::from_lock(lock, conversation, budget)?,
                LockedRegistryRoute::from_lock(lock, gateway, budget)?,
            ],
            shell_pack: LockedDocument::pack_from_lock(lock, shell_id, budget)?,
            application_pack: LockedDocument::pack_from_lock(
                lock,
                CONVERSATION_COMPLETE_APPLICATION_ID,
                budget,
            )?,
            choice,
            budget: Arc::clone(budget),
        }))
    }

    /// Capture the original resolver's signed documents for every selected Routing role.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn ai_routing_bundle<'pin>(
        &self,
        selection: &'pin OriginalAiRoutingSelection<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedAiRoutingBundle<'pin, 4>> {
        self.ordinary_ai_bundle(
            OriginalModelResolveRole::ALL.map(|role| selection.pins(role)),
            budget,
        )
    }

    /// Capture the normalizer's signed documents independently of the Routing grants.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn stream_result_bundle<'pin>(
        &self,
        selection: &'pin OriginalStreamResultSelection<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedAiRoutingBundle<'pin, 1>> {
        self.ordinary_ai_bundle([Some(selection.pins())], budget)
    }

    /// Capture one usage-cost chain using the original signed Bundle document pins.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn usage_cost_bundle<'pin>(
        &self,
        choice: &'pin OriginalUsageCostChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedAiRoutingBundle<'pin, 1>> {
        self.ordinary_ai_bundle([Some(choice.pins())], budget)
    }

    /// Capture one tool-intent normalization chain using the original signed Bundle document pins.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn tool_intents_bundle<'pin>(
        &self,
        choice: &'pin OriginalToolIntentsChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedAiRoutingBundle<'pin, 1>> {
        self.ordinary_ai_bundle([Some(choice.pins())], budget)
    }

    /// Capture the signed Bundle documents behind one Gateway request-preparation grant.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn request_prepare_bundle<'pin>(
        &self,
        choice: &'pin OriginalRequestPrepareChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedAiRoutingBundle<'pin, 1>> {
        self.ordinary_ai_bundle([Some(choice.pins())], budget)
    }

    /// Capture the signed Bundle documents behind the original Generate failover grant.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn failover_bundle<'pin>(
        &self,
        choice: &'pin OriginalFailoverChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedAiRoutingBundle<'pin, 1>> {
        self.ordinary_ai_bundle([Some(choice.pins())], budget)
    }

    /// Capture the signed Bundle documents through the original Gateway itself.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn saved_gateway_bundle<'pin>(
        &self,
        choice: &'pin OriginalSavedGatewayChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedAiRoutingBundle<'pin, 1, 3>> {
        let pins = choice.pins();
        self.require_ordinary_ai_callers(pins)?;
        let [turn, orchestration, gateway] = pins;
        let from_lock = |pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget);
        Ok(LockedAiRoutingBundle {
            source: VerifiedOrdinarySavedBundle::new(self)?,
            routes: [Some([
                from_lock(turn)?,
                from_lock(orchestration)?,
                from_lock(gateway)?,
            ])],
            budget: Arc::clone(budget),
        })
    }

    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    fn ordinary_ai_bundle<'pin, const ROLES: usize>(
        &self,
        selections: [Option<&'pin [OriginalRoutePin; 4]>; ROLES],
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedAiRoutingBundle<'pin, ROLES>> {
        let mut routes = std::array::from_fn(|_| None);
        for (locked, selection) in routes.iter_mut().zip(selections) {
            let Some(pins) = selection else {
                continue;
            };
            self.require_ordinary_ai_callers(pins)?;
            let [turn, orchestration, gateway, child] = pins;
            let from_lock = |pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget);
            *locked = Some([
                from_lock(turn)?,
                from_lock(orchestration)?,
                from_lock(gateway)?,
                from_lock(child)?,
            ]);
        }
        Ok(LockedAiRoutingBundle {
            source: VerifiedOrdinarySavedBundle::new(self)?,
            routes,
            budget: Arc::clone(budget),
        })
    }

    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    fn require_ordinary_ai_callers(&self, pins: &[OriginalRoutePin]) -> Result<()> {
        ensure!(
            pins.first()
                .is_some_and(|pin| pin.caller_function_id() == self.authority.shell_provider_id)
                && pins
                    .windows(2)
                    .all(|pair| pair[0].function_id() == pair[1].caller_function_id()),
            "old AI operation callers differ from the selected saved chain"
        );
        Ok(())
    }

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
            source: VerifiedOrdinarySavedBundle::new(self)?,
            routes,
            budget: Arc::clone(budget),
        })
    }

    /// Capture the signed Bundle documents for the selected start caller's grants.
    pub(crate) fn turn_start_bundle<'pin>(
        &self,
        selection: &'pin OriginalSavedTurnStartChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedTurnStartBundle<'pin>> {
        let [turn, lifecycle] = selection.pins();
        let side_chat = selection.side_chat();
        let first = side_chat.unwrap_or(turn);
        ensure!(
            first.caller_function_id() == self.authority.shell_provider_id
                && side_chat.is_none_or(|side| side.function_id() == turn.caller_function_id())
                && lifecycle.caller_function_id() == turn.function_id(),
            "old Turn start callers differ from the selected Shell and caller chain"
        );
        let from_lock = |pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget);
        Ok(LockedTurnStartBundle {
            application_root: CoreBuffer::copy_from(
                self.application_root.as_os_str().as_bytes(),
                budget,
            )?,
            bundle_root: CoreBuffer::copy_from(self.bundle_root.as_os_str().as_bytes(), budget)?,
            bundle_lock_digest: Digest::parse(&self.bundle_lock.digest)?,
            side_chat: side_chat.map(from_lock).transpose()?,
            routes: [from_lock(turn)?, from_lock(lifecycle)?],
            budget: Arc::clone(budget),
        })
    }

    /// Capture the signed Bundle documents for each settlement caller's chain.
    pub(crate) fn turn_settlement_bundle<'pin>(
        &self,
        selections: [Option<&'pin OriginalSavedTurnSettlementChoice<'_>>; 4],
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedTurnSettlementBundle<'pin>> {
        let mut routes = [None, None, None, None];
        let from_lock = |pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget);
        for (route, selection) in routes.iter_mut().zip(selections) {
            if let Some(selection) = selection {
                let turn = selection.pin();
                let side_chat = selection.side_chat();
                let first = side_chat.unwrap_or(turn);
                ensure!(
                    first.caller_function_id() == self.authority.shell_provider_id
                        && side_chat
                            .is_none_or(|side| side.function_id() == turn.caller_function_id()),
                    "old Turn settlement callers differ from the selected Shell and caller chain"
                );
                *route = Some(LockedTurnSettlementRoute {
                    side_chat: side_chat.map(from_lock).transpose()?,
                    turn: from_lock(turn)?,
                });
            }
        }
        Ok(LockedTurnSettlementBundle {
            application_root: CoreBuffer::copy_from(
                self.application_root.as_os_str().as_bytes(),
                budget,
            )?,
            bundle_root: CoreBuffer::copy_from(self.bundle_root.as_os_str().as_bytes(), budget)?,
            bundle_lock_digest: Digest::parse(&self.bundle_lock.digest)?,
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
                    selection.side_chat().is_none()
                        && turn_caller.caller_function_id() == self.authority.shell_provider_id,
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

    /// Capture the four installed routes behind one Side Chat confirmation chain.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn side_chat_confirmation_bundle<'pin>(
        &self,
        selection: &'pin OriginalConversationConfirmationChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedSideChatConfirmationBundle<'pin>> {
        let side_chat = selection
            .side_chat()
            .context("Side Chat confirmation has no signed Side Chat entry")?;
        let pins = [
            side_chat,
            selection.turn_caller(),
            selection.completion(),
            selection.turn_get(),
        ];
        ensure!(
            side_chat.caller_function_id() == self.authority.shell_provider_id
                && pins
                    .windows(2)
                    .all(|pair| pair[0].function_id() == pair[1].caller_function_id()),
            "Side Chat confirmation callers differ from the selected Shell and caller chain"
        );
        let [side_chat, turn, completion, get] = pins;
        let from_lock = |pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget);
        Ok(LockedSideChatConfirmationBundle {
            source: VerifiedOrdinarySavedBundle::new(self)?,
            routes: [
                from_lock(side_chat)?,
                from_lock(turn)?,
                from_lock(completion)?,
                from_lock(get)?,
            ],
            budget: Arc::clone(budget),
        })
    }

    /// Capture every locked Pack and catalog through the signed append route.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn conversation_append_bundle<'pin>(
        &self,
        selection: &'pin OriginalConversationAppendChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedConversationAppendBundle<'pin>> {
        let side_chat = selection.side_chat();
        let pins = selection.pins();
        let [turn, orchestration, manage] = pins;
        let first = side_chat.unwrap_or(turn);
        ensure!(
            first.caller_function_id() == self.authority.shell_provider_id
                && side_chat.is_none_or(|side| side.function_id() == turn.caller_function_id())
                && pins
                    .windows(2)
                    .all(|pair| pair[0].function_id() == pair[1].caller_function_id()),
            "saved append callers differ from the selected Shell and caller chain"
        );
        let from_lock = |pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget);
        Ok(LockedConversationAppendBundle {
            source: VerifiedOrdinarySavedBundle::new(self)?,
            side_chat: side_chat.map(from_lock).transpose()?,
            routes: [
                from_lock(turn)?,
                from_lock(orchestration)?,
                from_lock(manage)?,
            ],
            budget: Arc::clone(budget),
        })
    }

    /// Capture the signed documents behind this exact Side Chat receipt chain.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn side_chat_saved_receipt_bundle<'pin>(
        &self,
        selection: &'pin OriginalSideChatSavedReceiptChoice<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedSideChatSavedReceiptBundle<'pin>> {
        let pins = selection.pins();
        let [side_chat, turn, receipt] = pins;
        ensure!(
            side_chat.caller_function_id() == self.authority.shell_provider_id
                && pins
                    .windows(2)
                    .all(|pair| pair[0].function_id() == pair[1].caller_function_id()),
            "Side Chat saved receipt callers differ from the selected Shell and caller chain"
        );
        let from_lock = |pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget);
        Ok(LockedSideChatSavedReceiptBundle {
            source: VerifiedOrdinarySavedBundle::new(self)?,
            routes: [from_lock(side_chat)?, from_lock(turn)?, from_lock(receipt)?],
            budget: Arc::clone(budget),
        })
    }

    /// Capture locked documents from the selected ordinary saved orchestration pins.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn saved_orchestration_bundle<'pin>(
        &self,
        [turn, orchestration]: [&'pin OriginalRoutePin; 2],
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
        Ok(LockedSavedOrchestrationBundle {
            root: CoreBuffer::copy_from(self.bundle_root.as_os_str().as_bytes(), budget)?,
            routes,
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
    /// Verify each operation and retain the exact documents for each admitted caller.
    pub(crate) fn verify(
        &self,
        mut checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<[Option<VerifiedTurnEntryBundle>; 4]> {
        let bundle_root = Path::new(OsStr::from_bytes(self.bundle_root.as_bytes()));
        verify_privileged_routes(
            self.routes
                .iter()
                .flatten()
                .filter_map(|route| route.side_chat.as_ref()),
            bundle_root,
            &self.budget,
            &mut checkpoint,
        )?;
        verify_privileged_routes(
            self.routes.iter().flatten().map(|route| &route.turn),
            bundle_root,
            &self.budget,
            &mut checkpoint,
        )?;
        checkpoint()?;
        let mut verified = [None, None, None, None];
        for (result, route) in verified.iter_mut().zip(&self.routes) {
            if let Some(route) = route {
                *result = Some(VerifiedTurnEntryBundle::from_routes(
                    &self.application_root,
                    bundle_root,
                    &self.bundle_lock_digest,
                    route.side_chat.as_ref(),
                    &route.turn,
                )?);
            }
        }
        Ok(verified)
    }
}

impl LockedTurnStartBundle<'_> {
    /// Verify every original variant and privileged operation for this start caller.
    pub(crate) fn verify(
        &self,
        mut checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<VerifiedTurnEntryBundle> {
        let bundle_root = Path::new(OsStr::from_bytes(self.bundle_root.as_bytes()));
        if let Some(side_chat) = &self.side_chat {
            side_chat.verify(
                bundle_root,
                &self.budget,
                &mut checkpoint,
                Some("privileged"),
            )?;
        }
        verify_privileged_routes(
            self.routes.iter(),
            bundle_root,
            &self.budget,
            &mut checkpoint,
        )?;
        checkpoint()?;
        let [turn, _] = &self.routes;
        VerifiedTurnEntryBundle::from_routes(
            &self.application_root,
            bundle_root,
            &self.bundle_lock_digest,
            self.side_chat.as_ref(),
            turn,
        )
    }
}

impl VerifiedTurnEntryDocuments {
    fn from_route(route: &LockedRegistryRoute<'_>) -> Result<Self> {
        Ok(Self {
            pack: (
                route.pack.path.as_str()?.to_owned(),
                route.pack.digest.clone(),
            ),
            catalog: (
                route.catalog.path.as_str()?.to_owned(),
                route.catalog.digest.clone(),
            ),
        })
    }

    fn matches_lock(&self, lock: &VerifiedBundleLock, pack_id: &str) -> bool {
        let (pack_path, pack_digest) = &self.pack;
        let (catalog_path, catalog_digest) = &self.catalog;
        lock.pack_paths.get(pack_id) == Some(pack_path)
            && lock.authority_roles.get(pack_path) == Some(&BundleEntryKind::Pack)
            && lock.authority_digests.get(pack_path).map(String::as_str)
                == Some(pack_digest.as_str())
            && lock.sidecar_digests.get(catalog_path).map(String::as_str)
                == Some(catalog_digest.as_str())
    }
}

impl VerifiedTurnEntryBundle {
    fn from_routes(
        application_root: &CoreBuffer,
        bundle_root: &Path,
        bundle_lock_digest: &Digest,
        side_chat: Option<&LockedRegistryRoute<'_>>,
        turn: &LockedRegistryRoute<'_>,
    ) -> Result<Self> {
        Ok(Self {
            application_root: Path::new(OsStr::from_bytes(application_root.as_bytes())).into(),
            bundle_root: bundle_root.into(),
            bundle_lock_digest: bundle_lock_digest.clone(),
            side_chat: side_chat
                .map(VerifiedTurnEntryDocuments::from_route)
                .transpose()?,
            turn: VerifiedTurnEntryDocuments::from_route(turn)?,
        })
    }

    /// Bind the current signed resolution to the exact installed source used for admission.
    pub(crate) fn require_source(&self, sources: &NativeApplicationSources) -> Result<()> {
        let lock = &sources.bundle_lock;
        ensure!(
            self.application_root == sources.application_root
                && self.bundle_root == sources.bundle_root
                && self.bundle_lock_digest.as_str() == lock.digest
                && sources.authority.shell_provider_id == "shell.tauri.default"
                && self.turn.matches_lock(lock, "rumi_turn_runtime_pack")
                && self
                    .side_chat
                    .as_ref()
                    .is_none_or(|documents| documents.matches_lock(lock, "tobkiri_side_chat_pack")),
            "signed Turn entry source differs from the admitted launch"
        );
        Ok(())
    }

    /// Recheck the locked bytes for every Pack in the admitted caller chain.
    pub(crate) fn revalidate(
        &self,
        budget: &Arc<dyn AllocationBudget>,
        mut checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<()> {
        let documents = std::iter::once(("bundle.lock.json", &self.bundle_lock_digest)).chain(
            self.side_chat
                .iter()
                .chain(std::iter::once(&self.turn))
                .flat_map(|documents| {
                    [
                        (documents.pack.0.as_str(), &documents.pack.1),
                        (documents.catalog.0.as_str(), &documents.catalog.1),
                    ]
                }),
        );
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
    pub(crate) fn fixture(via_side_chat: bool) -> Self {
        Self {
            application_root: PathBuf::new(),
            bundle_root: PathBuf::new(),
            bundle_lock_digest: Digest::hash(b"test Bundle lock"),
            side_chat: via_side_chat.then(|| VerifiedTurnEntryDocuments {
                pack: (String::new(), Digest::hash(b"test Side Chat Pack")),
                catalog: (String::new(), Digest::hash(b"test Side Chat catalog")),
            }),
            turn: VerifiedTurnEntryDocuments {
                pack: (String::new(), Digest::hash(b"test Turn Pack")),
                catalog: (String::new(), Digest::hash(b"test Turn catalog")),
            },
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

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl LockedSideChatConfirmationBundle<'_> {
    /// Verify and retain the installed Pack and catalog for each grant.
    pub(crate) fn verify(
        self,
        mut checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<VerifiedOrdinarySavedBundle> {
        self.source.revalidate(&self.budget, &mut checkpoint)?;
        self.source.verify_routes(
            &[Some(self.routes)],
            &self.budget,
            checkpoint,
            ["privileged"; 4],
        )
    }
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl LockedSavedOrchestrationBundle<'_> {
    /// Verify the original privileged Turn and pure Orchestration operations.
    pub(crate) fn verify(&self, mut checkpoint: impl FnMut() -> Result<()>) -> Result<()> {
        let root = Path::new(OsStr::from_bytes(self.root.as_bytes()));
        let [turn, orchestration] = &self.routes;
        turn.verify(root, &self.budget, &mut checkpoint, Some("privileged"))?;
        orchestration.verify(root, &self.budget, &mut checkpoint, Some("pure"))?;
        checkpoint()
    }
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl LockedConversationAppendBundle<'_> {
    /// Verify and retain the signed physical documents for the selected append caller.
    pub(crate) fn verify(
        self,
        checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<VerifiedOrdinarySavedBundle> {
        let [turn, orchestration, manage] = self.routes;
        if let Some(side_chat) = self.side_chat {
            self.source.verify_routes(
                &[Some([side_chat, turn, orchestration, manage])],
                &self.budget,
                checkpoint,
                ["privileged", "privileged", "pure", "privileged"],
            )
        } else {
            self.source.verify_routes(
                &[Some([turn, orchestration, manage])],
                &self.budget,
                checkpoint,
                ["privileged", "pure", "privileged"],
            )
        }
    }
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl LockedSideChatSavedReceiptBundle<'_> {
    /// Verify the grant chain and retain the physical source for subsequent reads.
    pub(crate) fn verify(
        self,
        checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<VerifiedOrdinarySavedBundle> {
        self.source.verify_routes(
            &[Some(self.routes)],
            &self.budget,
            checkpoint,
            ["privileged"; 3],
        )
    }
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl LockedSavedLocalChatBundle<'_> {
    /// Verify each role against the same bytes for each of the five selected Packs.
    pub(crate) fn verify(
        self,
        checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<VerifiedOrdinarySavedBundle> {
        let effects = [
            "privileged",
            "pure",
            "privileged",
            "privileged",
            "privileged",
        ];
        self.source
            .verify_routes(&self.routes, &self.budget, checkpoint, effects)
    }
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl<const ROLES: usize> LockedAiRoutingBundle<'_, ROLES> {
    /// Verify each role against the signed Turn, Orchestration, Gateway, and AI operation Packs.
    pub(crate) fn verify(
        self,
        checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<VerifiedOrdinarySavedBundle> {
        self.source.verify_routes(
            &self.routes,
            &self.budget,
            checkpoint,
            ["privileged", "pure", "privileged", "privileged"],
        )
    }
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl LockedAiRoutingBundle<'_, 1, 3> {
    /// Verify the signed Turn, Orchestration and Gateway Packs for this role.
    pub(crate) fn verify(
        self,
        checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<VerifiedOrdinarySavedBundle> {
        self.source.verify_routes(
            &self.routes,
            &self.budget,
            checkpoint,
            ["privileged", "pure", "privileged"],
        )
    }
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl LockedApplicationPresentationBundle<'_> {
    /// Prove the original caller's signed read and its effect-free nested operation.
    pub(crate) fn verify(
        self,
        checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<VerifiedOrdinarySavedBundle> {
        self.source.verify_routes(
            &[Some(self.routes)],
            &self.budget,
            checkpoint,
            ["privileged", "pure"],
        )
    }
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl LockedConversationCompleteBundle<'_> {
    /// Prove the original pure Conversation operation and its privileged Gateway child.
    pub(crate) fn verify(
        self,
        mut checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<VerifiedOrdinarySavedBundle> {
        self.source.revalidate(&self.budget, &mut checkpoint)?;
        let mut source = self.source.verify_routes(
            &[Some(self.routes)],
            &self.budget,
            &mut checkpoint,
            ["pure", "privileged"],
        )?;
        for (document, pack_id, kind, [artifact, executable]) in [
            (
                self.shell_pack,
                "shell.tauri.default",
                "shell",
                self.choice.shell_artifacts(),
            ),
            (
                self.application_pack,
                CONVERSATION_COMPLETE_APPLICATION_ID,
                "application",
                self.choice.application_artifacts(),
            ),
        ] {
            let pack = document.read(&source.bundle_root, &self.budget, &mut checkpoint)?;
            let identity = pack
                .get_key("pack")
                .context("missing locked launch Pack identity")?;
            let function = unique_row(&pack, "functions", "id", pack_id)?;
            ensure!(
                text_matches(&identity, "id", pack_id)
                    && text_matches(&identity, "kind", kind)
                    && text_matches(&identity, "artifact_digest", artifact)
                    && text_matches(&function, "implementation_digest", executable),
                "Conversation Complete differs from the signed Shell or Application Pack"
            );
            source
                .documents
                .push((document.path.as_str()?.to_owned(), document.digest));
        }
        checkpoint()?;
        Ok(source)
    }
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl VerifiedOrdinarySavedBundle {
    fn verify_routes<const N: usize>(
        mut self,
        routes: &[Option<[LockedRegistryRoute<'_>; N]>],
        budget: &Arc<dyn AllocationBudget>,
        mut checkpoint: impl FnMut() -> Result<()>,
        effects: [&str; N],
    ) -> Result<Self> {
        for (index, effect) in effects.into_iter().enumerate() {
            verify_locked_routes(
                routes.iter().flatten().map(|routes| &routes[index]),
                &self.bundle_root,
                budget,
                &mut checkpoint,
                effect,
            )?;
        }
        checkpoint()?;
        let first = routes
            .iter()
            .flatten()
            .next()
            .context("saved proof has no route")?;
        self.documents.reserve(2 * first.len());
        for route in first {
            for document in [&route.pack, &route.catalog] {
                self.documents
                    .push((document.path.as_str()?.to_owned(), document.digest.clone()));
            }
        }
        Ok(self)
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
            pack: LockedDocument::pack_from_lock(lock, pack_id, budget)?,
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
    fn pack_from_lock(
        lock: &VerifiedBundleLock,
        pack_id: &str,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self> {
        let path = lock
            .pack_paths
            .get(pack_id)
            .filter(|path| lock.authority_roles.get(*path) == Some(&BundleEntryKind::Pack))
            .context("legacy Registry Pack is absent from the accepted Bundle authority")?;
        let digest = lock
            .authority_digests
            .get(path)
            .context("legacy Registry Pack is missing its accepted Bundle digest")?;
        Self::new(path, digest, budget)
    }

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
    fn side_chat_turn_entry_revalidates_both_packs_and_the_bundle_lock() {
        struct Root(PathBuf);
        impl Drop for Root {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = Root(std::env::temp_dir().join(format!(
            "tobkiri-turn-start-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        )));
        fs::create_dir_all(root.0.join("packs")).unwrap();
        let root_path = root.0.canonicalize().unwrap();
        let bundle_lock = "bundle.lock.json";
        let turn_pack = "packs/rumi_turn_runtime_pack.json";
        let turn_catalog = "packs/rumi_turn_runtime_pack.executables.v4.json";
        let side_pack = "packs/tobkiri_side_chat_pack.json";
        let side_catalog = "packs/tobkiri_side_chat_pack.executables.v4.json";
        let paths = [
            bundle_lock,
            turn_pack,
            turn_catalog,
            side_pack,
            side_catalog,
        ];
        for path in paths {
            fs::write(root_path.join(path), path).unwrap();
        }
        let document = |path: &str| (path.to_owned(), Digest::hash(path.as_bytes()));
        let ordinary = VerifiedTurnEntryBundle {
            application_root: root_path.clone(),
            bundle_root: root_path.clone(),
            bundle_lock_digest: Digest::hash(bundle_lock.as_bytes()),
            side_chat: None,
            turn: VerifiedTurnEntryDocuments {
                pack: document(turn_pack),
                catalog: document(turn_catalog),
            },
        };
        let side_chat = VerifiedTurnEntryBundle {
            side_chat: Some(VerifiedTurnEntryDocuments {
                pack: document(side_pack),
                catalog: document(side_catalog),
            }),
            ..ordinary.clone()
        };
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        for path in paths {
            side_chat.revalidate(&budget, || Ok(())).unwrap();
            fs::write(root_path.join(path), b"changed").unwrap();
            let error = side_chat.revalidate(&budget, || Ok(())).unwrap_err();
            assert_eq!(
                error.to_string(),
                format!("locked Bundle digest changed: {path}")
            );
            assert_eq!(
                ordinary.revalidate(&budget, || Ok(())).is_ok(),
                [side_pack, side_catalog].contains(&path)
            );
            fs::write(root_path.join(path), path).unwrap();
        }
    }

    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    #[test]
    fn ordinary_gateway_bundles_revalidate_only_their_own_documents_and_bundle_lock() {
        struct Root(PathBuf);
        impl Drop for Root {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = Root(std::env::temp_dir().join(format!(
            "tobkiri-stream-bundle-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        )));
        fs::create_dir_all(root.0.join("packs")).unwrap();
        let bundle_lock = "bundle.lock.json";
        let normalizer = "packs/rumi_ai_stream_pack.executables.v4.json";
        let usage = "packs/rumi_ai_usage_pack.executables.v4.json";
        let routing = "packs/rumi_ai_routing_pack.executables.v4.json";
        for path in [bundle_lock, normalizer, usage, routing] {
            fs::write(root.0.join(path), path).unwrap();
        }
        let proof = |document: &str| VerifiedOrdinarySavedBundle {
            application_root: root.0.clone(),
            bundle_root: root.0.canonicalize().unwrap(),
            bundle_lock_digest: Digest::hash(bundle_lock.as_bytes()),
            documents: vec![(document.to_owned(), Digest::hash(document.as_bytes()))],
        };
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        fs::write(root.0.join(routing), b"changed").unwrap();

        for (selected, unrelated) in [(normalizer, usage), (usage, normalizer)] {
            let bundle = proof(selected);
            fs::write(root.0.join(unrelated), b"changed").unwrap();
            bundle.revalidate(&budget, || Ok(())).unwrap();
            fs::write(root.0.join(unrelated), unrelated).unwrap();
            for document in [selected, bundle_lock] {
                fs::write(root.0.join(document), b"changed").unwrap();
                let error = bundle.revalidate(&budget, || Ok(())).unwrap_err();
                assert_eq!(
                    error.to_string(),
                    format!("locked Bundle digest changed: {document}")
                );
                fs::write(root.0.join(document), document).unwrap();
            }
        }
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
