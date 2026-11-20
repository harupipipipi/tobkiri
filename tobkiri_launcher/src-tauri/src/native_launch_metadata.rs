//! Original Launch metadata, collected with the RuntimeOwner used during Setup.

use std::{
    io,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{atomic::Ordering, mpsc},
    time::{Duration, Instant},
};

use anyhow::{anyhow, bail, ensure, Context, Result};
use tauri::{AppHandle, Manager};
use tobkiri_core::{
    ids::{OpName, ShapeRef},
    pack::{RuntimeSpec, ShapeKind, VerifiedPack},
    Digest, NativeArtifactInspection, NativeSetupInvocation,
};
use tobkiri_gate::{ControlKind, CoreCancellation, PermissionSet};
use tobkiri_harness_native::{
    select_original_saved_turn_settlement, OriginalActivationInspection, OriginalActiveProfile,
    OriginalAiRoutingSelection, OriginalApplicationPresentationChoice,
    OriginalConversationAppendChoice, OriginalConversationCompleteChoice,
    OriginalConversationCompleteHealthChoice, OriginalConversationConfirmationChoice,
    OriginalConversationReadSelection, OriginalFailoverChoice, OriginalGatewayProviderHealthChoice,
    OriginalGatewayProviderSnapshotChoice, OriginalModelAccessProviderSnapshotSelection,
    OriginalModelResolveRole, OriginalModelResolveSelection, OriginalModelSaveSelection,
    OriginalModelSettingsSelection, OriginalProviderPrepareSelection, OriginalProviderSnapshotRole,
    OriginalQueryProviderSnapshotSelection, OriginalRequestPrepareChoice,
    OriginalReviewerHealthChoice, OriginalReviewerHealthRole, OriginalRoutePin,
    OriginalSavedGatewayChoice, OriginalSavedLocalChatChoice, OriginalSavedLocalChatRole,
    OriginalSavedOrchestrationChoice, OriginalSavedReceiptRole, OriginalSavedReceiptSelection,
    OriginalSavedTurnStartChoice, OriginalShellProviderSnapshotSelection,
    OriginalSideChatSavedReceiptChoice, OriginalStreamResultSelection, OriginalToolIntentsChoice,
    OriginalUsageCostChoice, OriginalWorkflowHealthCaller,
    OriginalWorkflowProviderSnapshotSelection, OriginalWorkflowSnapshotCaller,
    OriginalWorkflowSnapshotEntry,
};
use tobkiri_wasm::{
    verify_legacy_conversation_append_declaration,
    verify_legacy_conversation_confirmation_declaration,
    verify_legacy_conversation_linked_context_declaration,
    verify_legacy_conversation_list_declaration, verify_legacy_conversation_read_declaration,
    verify_legacy_conversation_receipt_declaration, verify_legacy_model_resolve_declaration,
    verify_legacy_model_save_declaration, verify_legacy_provider_health_declaration,
    verify_legacy_provider_model_access_declaration, verify_legacy_provider_snapshot_declaration,
};

use super::{
    authority, caught, selected_setup_component, JobTicket, NativeRuntimeState,
    OriginalBootstrapInputs, WorkerExit,
};
use crate::{
    defaultspack_authority::{
        require_current_launch_identity, NativeApplicationSources, NativeSourceRoot,
        SignedApplicationResolver, VerifiedOrdinarySavedBundle, VerifiedTurnEntryBundle,
    },
    dock_registration::{
        assemble_defaultspack_desktop_metadata, DefaultspackDesktopMetadata, LaunchMetadataUse,
    },
    host_contract::ExecutionProfileIdentity,
    native_declared_artifacts::collect_for_native_launch,
    preparation_budget, registry_pack_receipt, AppConfig, ShutdownState,
};

/// The original operation whose signed nested grant can read application presentation.
pub(crate) use tobkiri_harness_native::ApplicationPresentationRole;

/// The signed entry point authorizing this saved append.
pub(crate) use tobkiri_harness_native::ConversationAppendCaller;

/// The signed entry point authorizing this saved Turn start.
pub(crate) use tobkiri_harness_native::SavedTurnStartCaller;

/// The signed entry point authorizing this Turn settlement caller.
pub(crate) use tobkiri_harness_native::OriginalSavedTurnSettlementRole as SettlementCaller;

const REGISTRY_RECEIPT_SHA256: &str = env!("TOBKIRI_REGISTRY_PACK_RECEIPT_SHA256");
// Allow the bounded receipt decoder and streaming SHA-256 work to run only
// when their fixed working set fits the real Launch job's control ledger.
pub(super) const REGISTRY_VERIFICATION_BYTES: u64 = 256 * 1024;

/// Detached launch evidence for the verified legacy routes and their bundled Wasm Packs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedRegistryLaunch {
    pub(crate) identity: ExecutionProfileIdentity,
    pub(crate) security_epoch: u64,
    pub(crate) catalog_revision: String,
    pub(crate) provider_prepare: Option<VerifiedProviderPrepareSource>,
    provider_snapshot: Option<VerifiedProviderSnapshotSource>,
    pub(crate) model_settings: Option<VerifiedModelSettingsSource>,
    pub(crate) model_save: Option<VerifiedModelSaveSource>,
    model_resolve: Option<VerifiedModelResolveSource>,
    ai_routing: Option<VerifiedAiRoutingSource>,
    conversation: Option<VerifiedConversationSource>,
    turn: Option<VerifiedTurnSource>,
    orchestration: Option<VerifiedSavedOrchestrationSource>,
    local_chat: Option<VerifiedSavedLocalChatSource>,
    application_presentation: Option<VerifiedApplicationPresentationSource>,
    conversation_complete: Option<VerifiedConversationCompleteSource>,
}

/// The caller of the original conversation-owner read grant.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ConversationReadCaller {
    /// The selected Shell calling the owner directly.
    Shell,
    /// The original Saved Turn function calling the owner.
    SavedTurn,
    /// The original Saved Orchestration function calling the owner.
    SavedOrchestration,
}

impl VerifiedRegistryLaunch {
    /// Require the signed ordinary Conversation and Gateway grants and the verified DATA port.
    pub(crate) fn permits_conversation_complete(&self) -> bool {
        self.conversation_complete.is_some()
    }

    /// The admitted replacement manifest for the original Conversation Complete route.
    pub(crate) fn conversation_complete_manifest_sha256(&self) -> Option<&str> {
        self.conversation_complete
            .as_ref()
            .map(|source| source.manifest_sha256.as_str())
    }

    /// Require the caller's original nested presentation grant and verified read port.
    pub(crate) fn permits_application_presentation(
        &self,
        role: ApplicationPresentationRole,
    ) -> bool {
        self.application_presentation
            .as_ref()
            .and_then(|source| source.bundle(role))
            .is_some()
    }

    /// Advertise the sealed replacement only when every ordinary caller can use it.
    pub(crate) fn application_presentation_manifest_sha256(&self) -> Option<&str> {
        let source = self.application_presentation.as_ref()?;
        source
            .bundles
            .iter()
            .all(Option::is_some)
            .then_some(source.manifest_sha256.as_str())
    }

    /// Authorize the signed entry point and Provider operation verified at Launch.
    pub(crate) fn permits_saved_local_chat(&self, role: OriginalSavedLocalChatRole) -> bool {
        self.local_chat
            .as_ref()
            .and_then(|source| source.entry(role))
            .is_some()
    }

    /// Require the original caller's own Registry resource grant and the verified read port.
    pub(crate) fn permits_provider_snapshot(&self, role: OriginalProviderSnapshotRole) -> bool {
        self.provider_snapshot(role).is_some()
    }

    /// Whether any saved Gateway caller has a verified Registry resource grant.
    pub(crate) fn permits_gateway_provider_snapshot(&self) -> bool {
        self.raw_provider_snapshot()
            .is_some_and(|source| source.gateway.iter().any(Option::is_some))
    }

    /// Whether a Workflow entry has a verified Gateway or audio adapter Registry grant.
    pub(crate) fn permits_workflow_provider_snapshot(&self) -> bool {
        use OriginalWorkflowSnapshotCaller::{AudioSpeech, AudioTranscribe, Messages, Text};

        self.raw_provider_snapshot().is_some_and(|source| {
            [Text, Messages, AudioTranscribe, AudioSpeech]
                .into_iter()
                .any(|caller| source.workflow.permits_any_entry(caller))
        })
    }

    /// Whether a Workflow entry has a verified Text or Messages Generate adapter Registry grant.
    pub(crate) fn permits_workflow_adapter_provider_snapshot(&self) -> bool {
        use OriginalWorkflowSnapshotCaller::{MessagesAdapter, TextAdapter};

        self.raw_provider_snapshot().is_some_and(|source| {
            [TextAdapter, MessagesAdapter]
                .into_iter()
                .any(|caller| source.workflow.permits_any_entry(caller))
        })
    }

    /// Whether Shell Settings or Model Save has its own verified read grant.
    pub(crate) fn permits_projected_provider_snapshot(&self) -> bool {
        self.raw_provider_snapshot()
            .is_some_and(|source| source.shell_settings.is_some() || source.model_save.is_some())
    }

    /// Whether a query owner has its own verified projected Registry read grant.
    pub(crate) fn permits_query_provider_snapshot(&self) -> bool {
        self.raw_provider_snapshot().is_some_and(|source| {
            source.model_search.is_some() || source.command_datasource.is_some()
        })
    }

    /// Whether either original Model Access operation has a verified Registry read proof.
    pub(crate) fn permits_model_access_provider_snapshot(&self) -> bool {
        self.raw_provider_snapshot().is_some_and(|source| {
            source.model_access_read.is_some() || source.model_access_catalog.is_some()
        })
    }

    /// Require the Model Access operation's own signed grant and the verified projection port.
    pub(crate) fn permits_model_access_projection(
        &self,
        role: OriginalProviderSnapshotRole,
    ) -> bool {
        matches!(
            role,
            OriginalProviderSnapshotRole::ModelAccessRead
                | OriginalProviderSnapshotRole::ModelAccessCatalog
        ) && self
            .provider_snapshot(role)
            .is_some_and(|(_, source)| source.model_access_projection_declared)
    }

    fn provider_snapshot(
        &self,
        role: OriginalProviderSnapshotRole,
    ) -> Option<(
        &VerifiedOrdinarySavedBundle,
        &VerifiedProviderSnapshotSource,
    )> {
        let source = self.provider_snapshot.as_ref()?;
        let bundle = match role {
            OriginalProviderSnapshotRole::GatewayHealth { role, side_chat } => {
                source.gateway_health[usize::from(side_chat)][role.index()].as_ref()?
            }
            OriginalProviderSnapshotRole::WorkflowHealth { caller, entry } => source
                .workflow_health
                .bundle(caller.snapshot_caller(), entry)?,
            OriginalProviderSnapshotRole::ConversationCompleteHealth => {
                source.conversation_health.as_ref()?
            }
            OriginalProviderSnapshotRole::ReviewerHealth(role) => {
                source.reviewer_health[role.index()].as_ref()?
            }
            _ if !source.raw_snapshot_declared => return None,
            OriginalProviderSnapshotRole::Adapter(role) => {
                &self.local_chat.as_ref()?.entry(role)?.bundle
            }
            OriginalProviderSnapshotRole::Gateway { role, side_chat } => {
                let entry = source.gateway[usize::from(side_chat)].as_ref()?;
                if entry.side_chat.is_some() != side_chat
                    || !entry
                        .roles
                        .iter()
                        .flatten()
                        .any(|(granted, _)| *granted == role)
                {
                    return None;
                }
                &entry.bundle
            }
            OriginalProviderSnapshotRole::Workflow { caller, entry } => {
                source.workflow.bundle(caller, entry)?
            }
            OriginalProviderSnapshotRole::ShellSettings => source.shell_settings.as_ref()?,
            OriginalProviderSnapshotRole::ModelAccessRead => source.model_access_read.as_ref()?,
            OriginalProviderSnapshotRole::ModelAccessCatalog => {
                source.model_access_catalog.as_ref()?
            }
            OriginalProviderSnapshotRole::ModelSave => source.model_save.as_ref()?,
            OriginalProviderSnapshotRole::ModelSearch => source.model_search.as_ref()?,
            OriginalProviderSnapshotRole::CommandDatasource => {
                source.command_datasource.as_ref()?
            }
        };
        Some((bundle, source))
    }

    fn raw_provider_snapshot(&self) -> Option<&VerifiedProviderSnapshotSource> {
        self.provider_snapshot
            .as_ref()
            .filter(|source| source.raw_snapshot_declared)
    }

    /// Whether the verified Provider Pack exposes the raw Registry read port.
    pub(crate) fn permits_raw_provider_snapshot(&self) -> bool {
        self.raw_provider_snapshot().is_some()
    }

    /// Require a verified Health port and at least one signed Workflow Health path.
    pub(crate) fn permits_workflow_provider_health(&self) -> bool {
        self.provider_snapshot.as_ref().is_some_and(|source| {
            OriginalWorkflowHealthCaller::ALL.into_iter().any(|caller| {
                source
                    .workflow_health
                    .permits_any_entry(caller.snapshot_caller())
            })
        })
    }

    /// Require the original ordinary saved chain and verified pure Wasm computation.
    pub(crate) fn permits_saved_orchestration(&self) -> bool {
        self.saved_orchestration_manifest().is_some()
    }

    /// Exact replacement manifest digest verified for the original saved chain.
    pub(crate) fn saved_orchestration_manifest(&self) -> Option<&str> {
        self.orchestration
            .as_ref()
            .map(|source| source.manifest_sha256.as_str())
    }

    /// Require the Gateway role's own signed grant against the verified Model Pack.
    pub(crate) fn permits_model_resolve(&self, role: OriginalModelResolveRole) -> bool {
        self.model_resolve
            .as_ref()
            .is_some_and(|source| source.permits(role))
    }

    /// Require the Gateway role's own complete ordinary saved chain and verified Routing Pack.
    pub(crate) fn permits_ai_routing(&self, role: OriginalModelResolveRole) -> bool {
        self.ai_routing
            .as_ref()
            .is_some_and(|source| source.permits(role))
    }

    /// Require the signed Gateway Stream chain and verified stream-result port.
    pub(crate) fn permits_stream_result(&self) -> bool {
        self.ai_routing
            .as_ref()
            .is_some_and(|source| source.stream_result.is_some())
    }

    /// Require the signed ordinary Generate or Stream usage-cost grant for this Gateway role.
    pub(crate) fn permits_usage_cost(&self, role: OriginalModelResolveRole) -> bool {
        self.ai_routing
            .as_ref()
            .and_then(|source| source.usage_cost(role))
            .is_some()
    }

    /// Require the signed ordinary Generate or Stream tool-intent normalization grant.
    pub(crate) fn permits_tool_intents(&self, role: OriginalModelResolveRole) -> bool {
        self.ai_routing
            .as_ref()
            .and_then(|source| source.tool_intents(role))
            .is_some()
    }

    /// Require the exact signed request-preparation grant for this Gateway role.
    pub(crate) fn permits_request_prepare(&self, role: OriginalModelResolveRole) -> bool {
        self.ai_routing
            .as_ref()
            .and_then(|source| source.request_prepare(role))
            .is_some()
    }

    /// Require the original ordinary Generate chain and its signed failover grant.
    pub(crate) fn permits_failover(&self) -> bool {
        self.ai_routing
            .as_ref()
            .is_some_and(|source| source.failover.is_some())
    }

    /// Require the signed ordinary Generate or Stream Gateway and the verified result port.
    pub(crate) fn permits_gateway_result(&self, role: OriginalModelResolveRole) -> bool {
        self.ai_routing
            .as_ref()
            .and_then(|source| source.gateway_result(role))
            .is_some()
    }

    /// Require the exact saved Gateway role and the verified stateless policy port.
    pub(crate) fn permits_gateway_policy(&self, role: OriginalModelResolveRole) -> bool {
        self.ai_routing
            .as_ref()
            .and_then(|source| source.gateway_policy(role))
            .is_some()
    }

    /// Require the caller's own legacy read edge against the verified replacement.
    pub(crate) fn permits_conversation_read(&self, caller: ConversationReadCaller) -> bool {
        self.conversation
            .as_ref()
            .is_some_and(|source| source.permits_read(caller))
    }

    /// The exact original Shell read grant may use the separate list port when declared.
    pub(crate) fn permits_conversation_list(&self) -> bool {
        self.conversation
            .as_ref()
            .is_some_and(VerifiedConversationSource::permits_list)
    }

    /// Require the Saved Orchestration read edge and the verified private linked context port.
    pub(crate) fn permits_linked_conversation_context(&self) -> bool {
        self.conversation
            .as_ref()
            .is_some_and(VerifiedConversationSource::permits_linked_context)
    }

    /// Require the caller's original signed Turn grant and verified settlement Pack.
    pub(crate) fn permits_turn_settlement(&self, role: SettlementCaller) -> bool {
        self.turn
            .as_ref()
            .is_some_and(|source| source.permits_settlement(role))
    }

    /// Require the caller's complete signed confirmation chain and verified Turn read port.
    pub(crate) fn permits_turn_read(&self, role: OriginalSavedReceiptRole) -> bool {
        self.turn
            .as_ref()
            .is_some_and(|source| source.permits_read(role))
    }

    /// Require this caller's exact Saved Turn-to-Lifecycle chain and verified Turn start port.
    pub(crate) fn permits_turn_start_for(&self, caller: SavedTurnStartCaller) -> bool {
        self.turn
            .as_ref()
            .and_then(|source| source.start.bundle(caller))
            .is_some()
    }

    /// Require the ordinary signed Turn start chain and the verified Turn input port.
    pub(crate) fn permits_saved_input(&self) -> bool {
        self.turn
            .as_ref()
            .is_some_and(|source| source.declares_saved_input && source.start.saved_turn.is_some())
    }

    /// Require the receipt caller's own legacy edge and verified receipt declaration.
    pub(crate) fn permits_saved_receipt(&self, role: OriginalSavedReceiptRole) -> bool {
        self.conversation
            .as_ref()
            .is_some_and(|source| source.permits_saved_receipt(role))
    }

    /// Require this caller's original grants and the verified append declaration.
    pub(crate) fn permits_conversation_append_for(&self, caller: ConversationAppendCaller) -> bool {
        self.conversation
            .as_ref()
            .and_then(|source| source.append.bundle(caller))
            .is_some()
    }

    /// Require the exact Shell, Turn, and Conversation grants for this caller.
    pub(crate) fn permits_conversation_confirmation(&self, role: OriginalSavedReceiptRole) -> bool {
        self.conversation
            .as_ref()
            .is_some_and(|source| source.permits_confirmation(role))
    }
}

/// Separate installed Bundle proof for each caller, sharing one build-pinned archive receipt.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedApplicationPresentationSource {
    bundles: [Option<VerifiedOrdinarySavedBundle>; 3],
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

impl VerifiedApplicationPresentationSource {
    fn bundle(&self, role: ApplicationPresentationRole) -> Option<&VerifiedOrdinarySavedBundle> {
        let index = match role {
            ApplicationPresentationRole::Settings => 0,
            ApplicationPresentationRole::Catalog => 1,
            ApplicationPresentationRole::Commands => 2,
        };
        self.bundles[index].as_ref()
    }
}

/// The exact ordinary Conversation Complete source and build-pinned archive.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedConversationCompleteSource {
    bundle: VerifiedOrdinarySavedBundle,
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

/// Signed legacy Provider route pins and the receipt identity of its Wasm Pack.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedProviderPrepareSource {
    pub(crate) coordinator: VerifiedLegacyRoutePin,
    pub(crate) provider: VerifiedLegacyRoutePin,
    pub(crate) archive_sha256: String,
    pub(crate) archive_bytes: u64,
    pub(crate) manifest_sha256: String,
}

/// Signed legacy Model list pin and the receipt identity of its Wasm Pack.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedModelSettingsSource {
    pub(crate) model_list: VerifiedLegacyRoutePin,
    pub(crate) archive_sha256: String,
    pub(crate) archive_bytes: u64,
    pub(crate) manifest_sha256: String,
}

/// The two signed Model Save edges and the receipt identity of their Wasm replacement.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedModelSaveSource {
    pub(crate) model_manage: VerifiedLegacyRoutePin,
    pub(crate) provider_read: VerifiedLegacyRoutePin,
    pub(crate) archive_sha256: String,
    pub(crate) archive_bytes: u64,
    pub(crate) manifest_sha256: String,
}

/// Independent Gateway role pins and the exact Model Pack receipt they share.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedModelResolveSource {
    roles: [Option<(OriginalModelResolveRole, VerifiedLegacyRoutePin)>; 4],
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

impl VerifiedModelResolveSource {
    fn permits(&self, role: OriginalModelResolveRole) -> bool {
        self.roles
            .iter()
            .flatten()
            .any(|(granted, _)| *granted == role)
    }
}

/// Independent ordinary saved grants that share one verified AI Routing archive.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedAiRoutingSource {
    routing: Option<VerifiedAiRoutingRoles>,
    stream_result: Option<VerifiedStreamResultRole>,
    usage_cost: [Option<VerifiedAiGatewayRole>; 2],
    request_prepare: [Option<VerifiedAiGatewayRole>; 4],
    failover: Option<VerifiedAiGatewayRole>,
    tool_intents: [Option<VerifiedAiGatewayRole>; 2],
    saved_gateway: Option<VerifiedSavedGatewaySource>,
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

impl VerifiedAiRoutingSource {
    fn permits(&self, role: OriginalModelResolveRole) -> bool {
        self.routing.as_ref().is_some_and(|routing| {
            routing
                .roles
                .iter()
                .flatten()
                .any(|(granted, _)| *granted == role)
        })
    }

    fn usage_cost(&self, role: OriginalModelResolveRole) -> Option<&VerifiedAiGatewayRole> {
        self.usage_cost
            .iter()
            .flatten()
            .find(|grant| grant.role == role)
    }

    fn request_prepare(&self, role: OriginalModelResolveRole) -> Option<&VerifiedAiGatewayRole> {
        self.request_prepare
            .iter()
            .flatten()
            .find(|grant| grant.role == role)
    }

    fn tool_intents(&self, role: OriginalModelResolveRole) -> Option<&VerifiedAiGatewayRole> {
        self.tool_intents
            .iter()
            .flatten()
            .find(|grant| grant.role == role)
    }

    fn gateway_result(&self, role: OriginalModelResolveRole) -> Option<&VerifiedAiGatewayRole<3>> {
        let gateway = self.saved_gateway.as_ref()?;
        if gateway.result && AI_GATEWAY_ROLES.contains(&role) {
            gateway.grant(role)
        } else {
            None
        }
    }

    fn gateway_policy(&self, role: OriginalModelResolveRole) -> Option<&VerifiedAiGatewayRole<3>> {
        let gateway = self.saved_gateway.as_ref()?;
        if gateway.policy {
            gateway.grant(role)
        } else {
            None
        }
    }
}

/// Original Gateway grants and the replacement ports independently verified at Launch.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedSavedGatewaySource {
    grants: [Option<VerifiedAiGatewayRole<3>>; 3],
    result: bool,
    policy: bool,
}

impl VerifiedSavedGatewaySource {
    fn grant(&self, role: OriginalModelResolveRole) -> Option<&VerifiedAiGatewayRole<3>> {
        // Both delivery modes of Quote use the same signed Gateway edge.
        let physical_role = saved_gateway_physical_role(role);
        self.grants
            .iter()
            .flatten()
            .find(|grant| grant.role == physical_role)
    }
}

const fn saved_gateway_physical_role(role: OriginalModelResolveRole) -> OriginalModelResolveRole {
    match role {
        OriginalModelResolveRole::QuoteStream => OriginalModelResolveRole::QuoteGenerate,
        _ => role,
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedAiRoutingRoles {
    bundle: VerifiedOrdinarySavedBundle,
    roles: [Option<(OriginalModelResolveRole, [VerifiedLegacyRoutePin; 4])>; 4],
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedStreamResultRole {
    bundle: VerifiedOrdinarySavedBundle,
    pins: [VerifiedLegacyRoutePin; 4],
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedAiGatewayRole<const PINS: usize = 4> {
    role: OriginalModelResolveRole,
    bundle: VerifiedOrdinarySavedBundle,
    pins: [VerifiedLegacyRoutePin; PINS],
}

/// Separate signed conversation-owner edges and the receipt identity they share.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedConversationSource {
    shell: Option<VerifiedLegacyRoutePin>,
    saved_turn: Option<VerifiedLegacyRoutePin>,
    saved_orchestration: Option<VerifiedLegacyRoutePin>,
    linked_context_declared: bool,
    list_declared: bool,
    saved_receipt: [Option<(OriginalSavedReceiptRole, VerifiedLegacyRoutePin)>; 2],
    side_chat_saved_receipt: VerifiedSideChatSavedRoles,
    confirmation: VerifiedConversationConfirmationRoles,
    append: VerifiedConversationAppendRoles,
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

type VerifiedSideChatSavedRoles =
    [Option<(OriginalSavedReceiptRole, VerifiedOrdinarySavedBundle)>; 2];

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct VerifiedConversationConfirmationRoles {
    ordinary: [Option<(OriginalSavedReceiptRole, [VerifiedLegacyRoutePin; 3])>; 2],
    side_chat: VerifiedSideChatSavedRoles,
}

impl VerifiedConversationConfirmationRoles {
    fn is_empty(&self) -> bool {
        self.ordinary.iter().all(Option::is_none) && self.side_chat.iter().all(Option::is_none)
    }

    fn permits(&self, role: OriginalSavedReceiptRole) -> bool {
        self.ordinary
            .iter()
            .flatten()
            .any(|(granted, _)| *granted == role)
            || self.side_chat_bundle(role).is_some()
    }

    fn side_chat_bundle(
        &self,
        role: OriginalSavedReceiptRole,
    ) -> Option<&VerifiedOrdinarySavedBundle> {
        self.side_chat
            .iter()
            .flatten()
            .find_map(|(granted, bundle)| (*granted == role).then_some(bundle))
    }

    fn revalidate_side_chat(
        &self,
        inspection: &NativeArtifactInspection<NativeApplicationSources>,
        role: OriginalSavedReceiptRole,
        checkpoint: &impl Fn() -> Result<()>,
    ) -> Result<()> {
        if let Some(bundle) = self.side_chat_bundle(role) {
            checkpoint()?;
            inspection.with_source(|sources| bundle.require_source(sources))??;
            bundle.revalidate(inspection.allocation_budget()?, checkpoint)?;
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct VerifiedConversationAppendRoles {
    saved_turn: Option<VerifiedOrdinarySavedBundle>,
    side_chat: Option<VerifiedOrdinarySavedBundle>,
}

impl VerifiedConversationAppendRoles {
    fn is_empty(&self) -> bool {
        self.saved_turn.is_none() && self.side_chat.is_none()
    }

    fn bundle(&self, caller: ConversationAppendCaller) -> Option<&VerifiedOrdinarySavedBundle> {
        match caller {
            ConversationAppendCaller::SavedTurn => self.saved_turn.as_ref(),
            ConversationAppendCaller::SideChat => self.side_chat.as_ref(),
        }
    }
}

impl VerifiedConversationSource {
    fn permits_read(&self, caller: ConversationReadCaller) -> bool {
        match caller {
            ConversationReadCaller::Shell => self.shell.is_some(),
            ConversationReadCaller::SavedTurn => self.saved_turn.is_some(),
            ConversationReadCaller::SavedOrchestration => self.saved_orchestration.is_some(),
        }
    }

    fn permits_linked_context(&self) -> bool {
        self.saved_orchestration.is_some() && self.linked_context_declared
    }

    fn permits_list(&self) -> bool {
        self.shell.is_some() && self.list_declared
    }

    fn permits_saved_receipt(&self, role: OriginalSavedReceiptRole) -> bool {
        self.saved_receipt
            .iter()
            .flatten()
            .any(|(granted, _)| *granted == role)
            || self.side_chat_saved_receipt_bundle(role).is_some()
    }

    fn side_chat_saved_receipt_bundle(
        &self,
        role: OriginalSavedReceiptRole,
    ) -> Option<&VerifiedOrdinarySavedBundle> {
        self.side_chat_saved_receipt
            .iter()
            .flatten()
            .find_map(|(granted, bundle)| (*granted == role).then_some(bundle))
    }

    fn permits_confirmation(&self, role: OriginalSavedReceiptRole) -> bool {
        self.confirmation.permits(role)
    }
}

/// Independent signed grants for starting, settling, and reading a Turn.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedTurnSource {
    settlement: [Option<VerifiedTurnSettlementGrant>; 4],
    read: VerifiedConversationConfirmationRoles,
    start: VerifiedTurnStartRoles,
    declares_saved_input: bool,
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedTurnSettlementGrant {
    caller: SettlementCaller,
    side_chat: Option<VerifiedLegacyRoutePin>,
    turn: VerifiedLegacyRoutePin,
    bundle: VerifiedTurnEntryBundle,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct VerifiedTurnStartRoles {
    saved_turn: Option<VerifiedTurnEntryBundle>,
    side_chat: Option<VerifiedTurnEntryBundle>,
}

impl VerifiedTurnStartRoles {
    fn is_empty(&self) -> bool {
        self.saved_turn.is_none() && self.side_chat.is_none()
    }

    fn bundle(&self, caller: SavedTurnStartCaller) -> Option<&VerifiedTurnEntryBundle> {
        match caller {
            SavedTurnStartCaller::SavedTurn => self.saved_turn.as_ref(),
            SavedTurnStartCaller::SideChat => self.side_chat.as_ref(),
        }
    }
}

/// The ordinary saved chain and its bundled executable computation.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedSavedOrchestrationSource {
    route: [VerifiedLegacyRoutePin; 2],
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

/// The exact installed Bundle and independent signed roles available for local chat.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedSavedLocalChatSource {
    direct: Option<VerifiedSavedLocalChatEntry>,
    side_chat: Option<VerifiedSavedLocalChatEntry>,
    adapter: Option<VerifiedProviderAdapterSource>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedSavedLocalChatEntry {
    bundle: VerifiedOrdinarySavedBundle,
    side_chat: Option<VerifiedLegacyRoutePin>,
    // The last pin is the Adapter's separate Registry resource edge for this exact role.
    roles: [Option<(OriginalSavedLocalChatRole, [VerifiedLegacyRoutePin; 5])>; 2],
}

impl VerifiedSavedLocalChatSource {
    fn entry(&self, role: OriginalSavedLocalChatRole) -> Option<&VerifiedSavedLocalChatEntry> {
        let entry = if role.is_side_chat() {
            self.side_chat.as_ref()
        } else {
            self.direct.as_ref()
        }?;
        (entry.side_chat.is_some() == role.is_side_chat()
            && entry
                .roles
                .iter()
                .flatten()
                .any(|(granted, _)| *granted == role))
        .then_some(entry)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedProviderAdapterSource {
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedProviderSnapshotSource {
    raw_snapshot_declared: bool,
    gateway: [Option<VerifiedGatewayProviderSnapshotEntry>; 2],
    gateway_health: [[Option<VerifiedOrdinarySavedBundle>; OriginalModelResolveRole::ALL.len()]; 2],
    workflow: VerifiedWorkflowProviderSnapshot,
    workflow_health: VerifiedWorkflowProviderSnapshot,
    conversation_health: Option<VerifiedOrdinarySavedBundle>,
    reviewer_health: [Option<VerifiedOrdinarySavedBundle>; OriginalReviewerHealthRole::ALL.len()],
    shell_settings: Option<VerifiedOrdinarySavedBundle>,
    model_save: Option<VerifiedOrdinarySavedBundle>,
    model_search: Option<VerifiedOrdinarySavedBundle>,
    command_datasource: Option<VerifiedOrdinarySavedBundle>,
    model_access_read: Option<VerifiedOrdinarySavedBundle>,
    model_access_catalog: Option<VerifiedOrdinarySavedBundle>,
    model_access_projection_declared: bool,
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct VerifiedWorkflowProviderSnapshot {
    entries_by_caller: [[bool; OriginalWorkflowSnapshotEntry::ALL.len()];
        OriginalWorkflowSnapshotCaller::ALL.len()],
    callers: [Option<VerifiedOrdinarySavedBundle>; OriginalWorkflowSnapshotCaller::ALL.len()],
}

impl VerifiedWorkflowProviderSnapshot {
    fn from_verified_routes(
        entries: [bool; OriginalWorkflowSnapshotEntry::ALL.len()],
        callers: [Option<VerifiedOrdinarySavedBundle>; OriginalWorkflowSnapshotCaller::ALL.len()],
    ) -> Self {
        Self {
            entries_by_caller: callers.each_ref().map(|caller| {
                if caller.is_some() {
                    entries
                } else {
                    [false; OriginalWorkflowSnapshotEntry::ALL.len()]
                }
            }),
            callers,
        }
    }

    fn bundle(
        &self,
        caller: OriginalWorkflowSnapshotCaller,
        entry: OriginalWorkflowSnapshotEntry,
    ) -> Option<&VerifiedOrdinarySavedBundle> {
        if !self.entries_by_caller[caller.index()][entry.index()] {
            return None;
        }
        self.callers[caller.index()].as_ref()
    }

    fn permits_any_entry(&self, caller: OriginalWorkflowSnapshotCaller) -> bool {
        self.callers[caller.index()].is_some()
            && self.entries_by_caller[caller.index()]
                .iter()
                .any(|entry| *entry)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedGatewayProviderSnapshotEntry {
    bundle: VerifiedOrdinarySavedBundle,
    side_chat: Option<VerifiedLegacyRoutePin>,
    roles: [Option<(OriginalModelResolveRole, [VerifiedLegacyRoutePin; 4])>; 4],
}

impl VerifiedTurnSource {
    fn permits_settlement(&self, role: SettlementCaller) -> bool {
        self.settlement_grant(role).is_some()
    }

    fn settlement_grant(&self, role: SettlementCaller) -> Option<&VerifiedTurnSettlementGrant> {
        self.settlement
            .iter()
            .flatten()
            .find(|grant| grant.caller == role)
    }

    fn permits_read(&self, role: OriginalSavedReceiptRole) -> bool {
        self.read.permits(role)
    }
}

/// Exact legacy binding digests confirmed against the activation and signed Bundle.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedLegacyRoutePin {
    pub(crate) artifact_digest: String,
    pub(crate) executable_catalog_digest: String,
    pub(crate) contract_revision_digest: String,
    pub(crate) function_implementation_digest: String,
    pub(crate) authority_reference: String,
    pub(crate) requested_scope_digest: String,
}

impl From<&OriginalRoutePin> for VerifiedLegacyRoutePin {
    fn from(pin: &OriginalRoutePin) -> Self {
        Self {
            artifact_digest: pin.artifact_digest().to_owned(),
            executable_catalog_digest: pin.executable_catalog_digest().to_owned(),
            contract_revision_digest: pin.contract_revision_digest().to_owned(),
            function_implementation_digest: pin.function_implementation_digest().to_owned(),
            authority_reference: pin.authority_reference().to_owned(),
            requested_scope_digest: pin.requested_scope_digest().to_owned(),
        }
    }
}

#[cfg(test)]
impl VerifiedRegistryLaunch {
    pub(crate) fn test_saved_local_chat_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalSavedLocalChatRole,
    ) -> Self {
        let mut proof = Self::test_proof(identity, catalog_revision);
        proof.provider_prepare = None;
        proof.add_test_saved_local_chat_role(role);
        proof
    }

    fn add_test_saved_local_chat_role(&mut self, role: OriginalSavedLocalChatRole) {
        let saved = self
            .local_chat
            .get_or_insert_with(|| VerifiedSavedLocalChatSource {
                direct: None,
                side_chat: None,
                adapter: None,
            });
        let entry = if role.is_side_chat() {
            &mut saved.side_chat
        } else {
            &mut saved.direct
        };
        let entry = entry.get_or_insert_with(|| VerifiedSavedLocalChatEntry {
            bundle: VerifiedOrdinarySavedBundle::fixture(),
            side_chat: role.is_side_chat().then(VerifiedLegacyRoutePin::fixture),
            roles: [None, None],
        });
        entry.roles[usize::from(role.is_stream())] = Some((
            role,
            std::array::from_fn(|_| VerifiedLegacyRoutePin::fixture()),
        ));
    }

    pub(crate) fn test_provider_snapshot_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: impl Into<OriginalProviderSnapshotRole>,
    ) -> Self {
        Self::test_provider_snapshot_proof_with_roles(identity, catalog_revision, [role.into()])
    }

    pub(crate) fn test_model_access_projection_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalProviderSnapshotRole,
    ) -> Self {
        let mut proof = Self::test_provider_snapshot_proof(identity, catalog_revision, role);
        proof
            .provider_snapshot
            .as_mut()
            .unwrap()
            .model_access_projection_declared = true;
        proof
    }

    pub(crate) fn test_provider_snapshot_proof_with_roles(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        roles: impl IntoIterator<Item = OriginalProviderSnapshotRole>,
    ) -> Self {
        let mut proof = Self::test_proof(identity, catalog_revision);
        proof.provider_prepare = None;
        let mut source = VerifiedProviderSnapshotSource {
            raw_snapshot_declared: false,
            gateway: [None, None],
            gateway_health: [const { [const { None }; OriginalModelResolveRole::ALL.len()] }; 2],
            workflow: VerifiedWorkflowProviderSnapshot::default(),
            workflow_health: VerifiedWorkflowProviderSnapshot::default(),
            conversation_health: None,
            reviewer_health: [const { None }; OriginalReviewerHealthRole::ALL.len()],
            shell_settings: None,
            model_save: None,
            model_search: None,
            command_datasource: None,
            model_access_read: None,
            model_access_catalog: None,
            model_access_projection_declared: false,
            archive_sha256: Digest::hash(b"provider-snapshot-archive").to_string(),
            archive_bytes: 512,
            manifest_sha256: Digest::hash(b"provider-snapshot-manifest").to_string(),
        };
        for role in roles {
            source.raw_snapshot_declared |= !role.is_health();
            let bundle = match role {
                OriginalProviderSnapshotRole::GatewayHealth { role, side_chat } => {
                    &mut source.gateway_health[usize::from(side_chat)][role.index()]
                }
                OriginalProviderSnapshotRole::Adapter(role) => {
                    proof.add_test_saved_local_chat_role(role);
                    continue;
                }
                OriginalProviderSnapshotRole::Gateway { role, side_chat } => {
                    let entry = source.gateway[usize::from(side_chat)].get_or_insert_with(|| {
                        VerifiedGatewayProviderSnapshotEntry {
                            bundle: VerifiedOrdinarySavedBundle::fixture(),
                            side_chat: side_chat.then(VerifiedLegacyRoutePin::fixture),
                            roles: [None, None, None, None],
                        }
                    });
                    entry.roles[role.index()] = Some((
                        role,
                        std::array::from_fn(|_| VerifiedLegacyRoutePin::fixture()),
                    ));
                    continue;
                }
                OriginalProviderSnapshotRole::Workflow { caller, entry } => {
                    source.workflow.entries_by_caller[caller.index()][entry.index()] = true;
                    &mut source.workflow.callers[caller.index()]
                }
                OriginalProviderSnapshotRole::WorkflowHealth { caller, entry } => {
                    let caller = caller.snapshot_caller().index();
                    source.workflow_health.entries_by_caller[caller][entry.index()] = true;
                    &mut source.workflow_health.callers[caller]
                }
                OriginalProviderSnapshotRole::ConversationCompleteHealth => {
                    &mut source.conversation_health
                }
                OriginalProviderSnapshotRole::ReviewerHealth(role) => {
                    &mut source.reviewer_health[role.index()]
                }
                OriginalProviderSnapshotRole::ShellSettings => &mut source.shell_settings,
                OriginalProviderSnapshotRole::ModelAccessRead => &mut source.model_access_read,
                OriginalProviderSnapshotRole::ModelAccessCatalog => {
                    &mut source.model_access_catalog
                }
                OriginalProviderSnapshotRole::ModelSave => &mut source.model_save,
                OriginalProviderSnapshotRole::ModelSearch => &mut source.model_search,
                OriginalProviderSnapshotRole::CommandDatasource => &mut source.command_datasource,
            };
            bundle.get_or_insert_with(VerifiedOrdinarySavedBundle::fixture);
        }
        proof.provider_snapshot = Some(source);
        proof
    }

    pub(crate) fn test_conversation_complete_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        let mut proof = Self::test_proof(identity, catalog_revision);
        proof.conversation_complete = Some(VerifiedConversationCompleteSource {
            bundle: VerifiedOrdinarySavedBundle::fixture(),
            archive_sha256: Digest::hash(b"conversation-complete-archive").to_string(),
            archive_bytes: 512,
            manifest_sha256: Digest::hash(b"conversation-complete-manifest").to_string(),
        });
        proof
    }

    pub(crate) fn test_complete_application_presentation_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        let mut proof = Self::test_application_presentation_proof(
            identity,
            catalog_revision,
            ApplicationPresentationRole::Settings,
        );
        proof.application_presentation.as_mut().unwrap().bundles =
            ApplicationPresentationRole::ALL.map(|_| Some(VerifiedOrdinarySavedBundle::fixture()));
        proof
    }

    pub(crate) fn test_application_presentation_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: ApplicationPresentationRole,
    ) -> Self {
        let mut proof = Self::test_proof(identity, catalog_revision);
        proof.application_presentation = Some(VerifiedApplicationPresentationSource {
            bundles: ApplicationPresentationRole::ALL
                .map(|candidate| (candidate == role).then(VerifiedOrdinarySavedBundle::fixture)),
            archive_sha256: "sha256:archive".into(),
            archive_bytes: 512,
            manifest_sha256: "sha256:manifest".into(),
        });
        proof
    }

    pub(crate) fn test_proof(identity: ExecutionProfileIdentity, catalog_revision: &str) -> Self {
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: Some(VerifiedProviderPrepareSource {
                coordinator: VerifiedLegacyRoutePin::fixture(),
                provider: VerifiedLegacyRoutePin::fixture(),
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
            provider_snapshot: None,
            model_settings: None,
            model_save: None,
            model_resolve: None,
            ai_routing: None,
            conversation: None,
            turn: None,
            orchestration: None,
            local_chat: None,
            application_presentation: None,
            conversation_complete: None,
        }
    }

    pub(crate) fn test_model_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: None,
            provider_snapshot: None,
            model_settings: Some(VerifiedModelSettingsSource {
                model_list: VerifiedLegacyRoutePin::fixture(),
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
            model_save: None,
            model_resolve: None,
            ai_routing: None,
            conversation: None,
            turn: None,
            orchestration: None,
            local_chat: None,
            application_presentation: None,
            conversation_complete: None,
        }
    }

    pub(crate) fn test_model_save_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: None,
            provider_snapshot: None,
            model_settings: None,
            model_save: Some(VerifiedModelSaveSource {
                model_manage: VerifiedLegacyRoutePin::fixture(),
                provider_read: VerifiedLegacyRoutePin::fixture(),
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
            model_resolve: None,
            ai_routing: None,
            conversation: None,
            turn: None,
            orchestration: None,
            local_chat: None,
            application_presentation: None,
            conversation_complete: None,
        }
    }

    pub(crate) fn test_model_resolve_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalModelResolveRole,
    ) -> Self {
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: None,
            provider_snapshot: None,
            model_settings: None,
            model_save: None,
            model_resolve: Some(VerifiedModelResolveSource {
                roles: [
                    Some((role, VerifiedLegacyRoutePin::fixture())),
                    None,
                    None,
                    None,
                ],
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
            ai_routing: None,
            conversation: None,
            turn: None,
            orchestration: None,
            local_chat: None,
            application_presentation: None,
            conversation_complete: None,
        }
    }

    pub(crate) fn test_ai_routing_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalModelResolveRole,
    ) -> Self {
        let mut proof = Self::test_proof(identity, catalog_revision);
        proof.provider_prepare = None;
        proof.ai_routing = Some(VerifiedAiRoutingSource {
            routing: Some(VerifiedAiRoutingRoles {
                bundle: VerifiedOrdinarySavedBundle::fixture(),
                roles: OriginalModelResolveRole::ALL.map(|granted| {
                    (granted == role).then(|| {
                        (
                            role,
                            std::array::from_fn(|_| VerifiedLegacyRoutePin::fixture()),
                        )
                    })
                }),
            }),
            stream_result: None,
            usage_cost: [None, None],
            request_prepare: [None, None, None, None],
            failover: None,
            tool_intents: [None, None],
            saved_gateway: None,
            archive_sha256: "sha256:archive".into(),
            archive_bytes: 512,
            manifest_sha256: "sha256:manifest".into(),
        });
        proof
    }

    pub(crate) fn test_stream_result_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        let mut proof = Self::test_proof(identity, catalog_revision);
        proof.provider_prepare = None;
        proof.ai_routing = Some(VerifiedAiRoutingSource {
            routing: None,
            stream_result: Some(VerifiedStreamResultRole {
                bundle: VerifiedOrdinarySavedBundle::fixture(),
                pins: std::array::from_fn(|_| VerifiedLegacyRoutePin::fixture()),
            }),
            usage_cost: [None, None],
            request_prepare: [None, None, None, None],
            failover: None,
            tool_intents: [None, None],
            saved_gateway: None,
            archive_sha256: "sha256:archive".into(),
            archive_bytes: 512,
            manifest_sha256: "sha256:manifest".into(),
        });
        proof
    }

    pub(crate) fn test_usage_cost_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalModelResolveRole,
    ) -> Self {
        let mut proof = Self::test_proof(identity, catalog_revision);
        proof.provider_prepare = None;
        proof.ai_routing = Some(VerifiedAiRoutingSource {
            routing: None,
            stream_result: None,
            usage_cost: Self::test_ai_gateway_roles(role),
            request_prepare: [None, None, None, None],
            failover: None,
            tool_intents: [None, None],
            saved_gateway: None,
            archive_sha256: "sha256:archive".into(),
            archive_bytes: 512,
            manifest_sha256: "sha256:manifest".into(),
        });
        proof
    }

    pub(crate) fn test_tool_intents_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalModelResolveRole,
    ) -> Self {
        let mut proof = Self::test_proof(identity, catalog_revision);
        proof.provider_prepare = None;
        proof.ai_routing = Some(VerifiedAiRoutingSource {
            routing: None,
            stream_result: None,
            usage_cost: [None, None],
            request_prepare: [None, None, None, None],
            failover: None,
            tool_intents: Self::test_ai_gateway_roles(role),
            saved_gateway: None,
            archive_sha256: "sha256:archive".into(),
            archive_bytes: 512,
            manifest_sha256: "sha256:manifest".into(),
        });
        proof
    }

    pub(crate) fn test_request_prepare_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalModelResolveRole,
    ) -> Self {
        let mut proof = Self::test_pipeline_proof(identity, catalog_revision);
        let source = proof.ai_routing.as_mut().unwrap();
        source.request_prepare = OriginalModelResolveRole::ALL
            .map(|candidate| (candidate == role).then(|| Self::test_ai_gateway_grant(role)));
        proof
    }

    pub(crate) fn test_failover_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        let mut proof = Self::test_pipeline_proof(identity, catalog_revision);
        proof.ai_routing.as_mut().unwrap().failover = Some(Self::test_ai_gateway_grant(
            OriginalModelResolveRole::Generate,
        ));
        proof
    }

    pub(crate) fn test_gateway_result_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalModelResolveRole,
    ) -> Self {
        let mut proof = Self::test_pipeline_proof(identity, catalog_revision);
        proof.ai_routing.as_mut().unwrap().saved_gateway = Some(VerifiedSavedGatewaySource {
            grants: SAVED_GATEWAY_ROLES.map(|candidate| {
                (AI_GATEWAY_ROLES.contains(&role) && candidate == role)
                    .then(|| Self::test_ai_gateway_grant(role))
            }),
            result: true,
            policy: false,
        });
        proof
    }

    pub(crate) fn test_gateway_policy_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalModelResolveRole,
    ) -> Self {
        let mut proof = Self::test_pipeline_proof(identity, catalog_revision);
        let role = saved_gateway_physical_role(role);
        proof.ai_routing.as_mut().unwrap().saved_gateway = Some(VerifiedSavedGatewaySource {
            grants: SAVED_GATEWAY_ROLES
                .map(|candidate| (candidate == role).then(|| Self::test_ai_gateway_grant(role))),
            result: false,
            policy: true,
        });
        proof
    }

    fn test_pipeline_proof(identity: ExecutionProfileIdentity, catalog_revision: &str) -> Self {
        let mut proof = Self::test_proof(identity, catalog_revision);
        proof.provider_prepare = None;
        proof.ai_routing = Some(VerifiedAiRoutingSource {
            routing: None,
            stream_result: None,
            usage_cost: [None, None],
            request_prepare: [None, None, None, None],
            failover: None,
            tool_intents: [None, None],
            saved_gateway: None,
            archive_sha256: "sha256:archive".into(),
            archive_bytes: 512,
            manifest_sha256: "sha256:manifest".into(),
        });
        proof
    }

    fn test_ai_gateway_grant<const PINS: usize>(
        role: OriginalModelResolveRole,
    ) -> VerifiedAiGatewayRole<PINS> {
        VerifiedAiGatewayRole {
            role,
            bundle: VerifiedOrdinarySavedBundle::fixture(),
            pins: std::array::from_fn(|_| VerifiedLegacyRoutePin::fixture()),
        }
    }

    fn test_ai_gateway_roles(role: OriginalModelResolveRole) -> [Option<VerifiedAiGatewayRole>; 2] {
        AI_GATEWAY_ROLES
            .map(|candidate| (candidate == role).then(|| Self::test_ai_gateway_grant(role)))
    }

    pub(crate) fn test_saved_receipt_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalSavedReceiptRole,
    ) -> Self {
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: None,
            provider_snapshot: None,
            model_settings: None,
            model_save: None,
            model_resolve: None,
            ai_routing: None,
            conversation: Some(VerifiedConversationSource {
                shell: None,
                saved_turn: None,
                saved_orchestration: None,
                linked_context_declared: false,
                list_declared: false,
                saved_receipt: OriginalSavedReceiptRole::ALL.map(|candidate| {
                    (candidate == role).then(|| (role, VerifiedLegacyRoutePin::fixture()))
                }),
                side_chat_saved_receipt: OriginalSavedReceiptRole::SIDE_CHAT_CALLERS.map(
                    |candidate| {
                        (candidate == role).then(|| (role, VerifiedOrdinarySavedBundle::fixture()))
                    },
                ),
                confirmation: VerifiedConversationConfirmationRoles::default(),
                append: VerifiedConversationAppendRoles::default(),
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
            turn: None,
            orchestration: None,
            local_chat: None,
            application_presentation: None,
            conversation_complete: None,
        }
    }

    pub(crate) fn test_linked_conversation_context_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        let mut proof = Self::test_saved_receipt_proof(
            identity,
            catalog_revision,
            OriginalSavedReceiptRole::ALL[0],
        );
        let source = proof.conversation.as_mut().unwrap();
        source.saved_receipt = [None, None];
        source.side_chat_saved_receipt = [None, None];
        source.saved_orchestration = Some(VerifiedLegacyRoutePin::fixture());
        source.linked_context_declared = true;
        proof
    }

    pub(crate) fn test_conversation_list_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        let mut proof = Self::test_linked_conversation_context_proof(identity, catalog_revision);
        let source = proof.conversation.as_mut().unwrap();
        source.saved_orchestration = None;
        source.linked_context_declared = false;
        source.shell = Some(VerifiedLegacyRoutePin::fixture());
        source.list_declared = true;
        proof
    }

    pub(crate) fn test_conversation_confirmation_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalSavedReceiptRole,
    ) -> Self {
        let mut proof = Self::test_saved_receipt_proof(identity, catalog_revision, role);
        let source = proof.conversation.as_mut().unwrap();
        source.saved_receipt = [None, None];
        source.side_chat_saved_receipt = [None, None];
        source.confirmation.ordinary = OriginalSavedReceiptRole::ALL.map(|candidate| {
            (candidate == role).then(|| {
                (
                    role,
                    std::array::from_fn(|_| VerifiedLegacyRoutePin::fixture()),
                )
            })
        });
        source.confirmation.side_chat =
            OriginalSavedReceiptRole::SIDE_CHAT_CALLERS.map(|candidate| {
                (candidate == role).then(|| (role, VerifiedOrdinarySavedBundle::fixture()))
            });
        proof
    }

    pub(crate) fn test_conversation_append_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        Self::test_conversation_append_proof_for(
            identity,
            catalog_revision,
            ConversationAppendCaller::SavedTurn,
        )
    }

    pub(crate) fn test_conversation_append_proof_for(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        caller: ConversationAppendCaller,
    ) -> Self {
        let mut proof = Self::test_saved_receipt_proof(
            identity,
            catalog_revision,
            OriginalSavedReceiptRole::SavedTurn,
        );
        let source = proof.conversation.as_mut().unwrap();
        source.saved_receipt = [None, None];
        let bundle = Some(VerifiedOrdinarySavedBundle::fixture());
        match caller {
            ConversationAppendCaller::SavedTurn => source.append.saved_turn = bundle,
            ConversationAppendCaller::SideChat => source.append.side_chat = bundle,
        }
        proof
    }

    pub(crate) fn test_turn_settlement_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: SettlementCaller,
    ) -> Self {
        let via_side_chat = matches!(
            role,
            SettlementCaller::SideChatTurn | SettlementCaller::SideChatReconcile
        );
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: None,
            provider_snapshot: None,
            model_settings: None,
            model_save: None,
            model_resolve: None,
            ai_routing: None,
            conversation: None,
            turn: Some(VerifiedTurnSource {
                settlement: [
                    Some(VerifiedTurnSettlementGrant {
                        caller: role,
                        side_chat: via_side_chat.then(VerifiedLegacyRoutePin::fixture),
                        turn: VerifiedLegacyRoutePin::fixture(),
                        bundle: VerifiedTurnEntryBundle::fixture(via_side_chat),
                    }),
                    None,
                    None,
                    None,
                ],
                read: VerifiedConversationConfirmationRoles::default(),
                start: VerifiedTurnStartRoles::default(),
                declares_saved_input: false,
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
            orchestration: None,
            local_chat: None,
            application_presentation: None,
            conversation_complete: None,
        }
    }

    pub(crate) fn test_turn_read_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalSavedReceiptRole,
    ) -> Self {
        let mut proof =
            Self::test_conversation_confirmation_proof(identity, catalog_revision, role);
        let confirmation = proof.conversation.take().unwrap();
        proof.turn = Some(VerifiedTurnSource {
            settlement: [None, None, None, None],
            read: confirmation.confirmation,
            start: VerifiedTurnStartRoles::default(),
            declares_saved_input: false,
            archive_sha256: confirmation.archive_sha256,
            archive_bytes: confirmation.archive_bytes,
            manifest_sha256: confirmation.manifest_sha256,
        });
        proof
    }

    pub(crate) fn test_turn_start_proof_for(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        caller: SavedTurnStartCaller,
    ) -> Self {
        let mut proof = Self::test_proof(identity, catalog_revision);
        proof.provider_prepare = None;
        let mut start = VerifiedTurnStartRoles::default();
        let bundle = Some(VerifiedTurnEntryBundle::fixture(
            caller == SavedTurnStartCaller::SideChat,
        ));
        match caller {
            SavedTurnStartCaller::SavedTurn => start.saved_turn = bundle,
            SavedTurnStartCaller::SideChat => start.side_chat = bundle,
        }
        proof.turn = Some(VerifiedTurnSource {
            settlement: [None, None, None, None],
            read: VerifiedConversationConfirmationRoles::default(),
            start,
            declares_saved_input: false,
            archive_sha256: "sha256:archive".into(),
            archive_bytes: 512,
            manifest_sha256: "sha256:manifest".into(),
        });
        proof
    }

    pub(crate) fn test_saved_input_proof_for(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        caller: SavedTurnStartCaller,
    ) -> Self {
        let mut proof = Self::test_turn_start_proof_for(identity, catalog_revision, caller);
        proof.turn.as_mut().unwrap().declares_saved_input = true;
        proof
    }

    pub(crate) fn test_saved_orchestration_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        let mut proof = Self::test_proof(identity, catalog_revision);
        proof.provider_prepare = None;
        proof.orchestration = Some(VerifiedSavedOrchestrationSource {
            route: std::array::from_fn(|_| VerifiedLegacyRoutePin::fixture()),
            archive_sha256: format!("sha256:{}", "a".repeat(64)),
            archive_bytes: 512,
            manifest_sha256: format!("sha256:{}", "b".repeat(64)),
        });
        proof
    }
}

#[cfg(test)]
impl VerifiedLegacyRoutePin {
    fn fixture() -> Self {
        Self {
            artifact_digest: "sha256:artifact".into(),
            executable_catalog_digest: "sha256:catalog".into(),
            contract_revision_digest: "sha256:contract".into(),
            function_implementation_digest: "sha256:implementation".into(),
            authority_reference: "authority-ref:fixture".into(),
            requested_scope_digest: "sha256:scope".into(),
        }
    }
}

pub(crate) fn registry_pack_receipt_bound() -> bool {
    !REGISTRY_RECEIPT_SHA256.is_empty()
}

pub(crate) fn read_launch_metadata(
    app: &AppHandle,
    deadline: Instant,
    purpose: LaunchMetadataUse,
) -> Result<DefaultspackDesktopMetadata> {
    preparation_budget::check(deadline)?;
    let runtime = app
        .try_state::<NativeRuntimeState>()
        .context("native Launch runtime owner is unavailable")?
        .inner()
        .clone();
    let shutdown = app
        .try_state::<ShutdownState>()
        .context("native Launch shutdown state is unavailable")?;
    let jobs = runtime.0.jobs.clone();
    let app = app.clone();
    let (send, receive) = mpsc::sync_channel(1);
    jobs.spawn(move |job| {
        let (result, cleanup_verified) = catch_unwind(AssertUnwindSafe(|| {
            tauri::async_runtime::block_on(collect_on_worker(
                &app, &runtime, &job, deadline, purpose,
            ))
        }))
        .unwrap_or_else(|_| (Err(anyhow!("native Launch metadata worker failed")), false));
        if !cleanup_verified {
            runtime.0.jobs.cleanup_failed();
        }
        let _ = send.send(result);
        WorkerExit { cleanup_verified }
    })?;
    loop {
        if shutdown.inner().0.load(Ordering::Acquire) {
            bail!("native Launch metadata observation stopped during shutdown");
        }
        let timeout = preparation_budget::remaining(deadline)?.min(Duration::from_millis(100));
        match receive.recv_timeout(timeout) {
            Ok(result) => {
                preparation_budget::check(deadline)?;
                return result;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(error) => {
                return Err(error).context("native Launch metadata worker is unavailable")
            }
        }
    }
}

async fn collect_on_worker(
    app: &AppHandle,
    runtime: &NativeRuntimeState,
    job: &JobTicket,
    deadline: Instant,
    purpose: LaunchMetadataUse,
) -> (Result<DefaultspackDesktopMetadata>, bool) {
    let admitted = (|| {
        preparation_budget::check(deadline)?;
        let config = app
            .try_state::<AppConfig>()
            .context("native Launch configuration is unavailable")?;
        let sources = SignedApplicationResolver::resolve_sources(&config)?;
        let identity = sources.authority().execution_identity()?;
        preparation_budget::check(deadline)?;
        let invocation = runtime.begin(app, job)?;
        let cancellation = job.attach_invocation(&invocation)?;
        Ok::<_, anyhow::Error>((config, sources, identity, invocation, cancellation))
    })();
    let (config, sources, identity, invocation, cancellation) = match admitted {
        Ok(admitted) => admitted,
        Err(error) => return (Err(error), true),
    };
    let observe_activation = registry_pack_receipt_bound()
        && match purpose {
            LaunchMetadataUse::Shell => true,
            LaunchMetadataUse::Spawn => config.native_provider_prepare_endpoint.is_some(),
            LaunchMetadataUse::Supervision => false,
        };
    let (result, cleanup_verified) = {
        let observer = LaunchActivationObserver {
            app,
            runtime,
            job,
            invocation: &invocation,
            cancellation: &cancellation,
            identity: &identity,
            deadline,
        };
        let collection = collect_for_native_launch(
            sources,
            invocation.clone(),
            &cancellation,
            observe_activation,
            async |inspection| observer.read(inspection).await,
        );
        tokio::pin!(collection);
        tokio::select! {
            result = &mut collection => result,
            () = tokio::time::sleep_until(deadline.into()) => {
                cancellation.cancel();
                // Keep the original worker and descriptors until physical cleanup.
                collection.await
            }
        }
    };
    let metadata = result.and_then(|(sources, contributions, proof)| {
        preparation_budget::check(deadline)?;
        require_current_launch_identity(&config, &identity)?;
        preparation_budget::check(deadline)?;
        job.ensure_accepting(&runtime.0.jobs)?;
        let mut metadata = assemble_defaultspack_desktop_metadata(
            &config,
            sources.into_authority(),
            identity,
            contributions,
        )?;
        if cleanup_verified && proof.is_some() {
            preparation_budget::check(deadline)?;
            job.ensure_accepting(&runtime.0.jobs)?;
            metadata.native_provider_prepare = proof;
        }
        Ok(metadata)
    });
    (metadata, cleanup_verified)
}

struct LaunchActivationObserver<'a> {
    app: &'a AppHandle,
    runtime: &'a NativeRuntimeState,
    job: &'a JobTicket,
    invocation: &'a NativeSetupInvocation,
    cancellation: &'a CoreCancellation,
    identity: &'a ExecutionProfileIdentity,
    deadline: Instant,
}

impl LaunchActivationObserver<'_> {
    async fn read(
        &self,
        inspection: &NativeArtifactInspection<NativeApplicationSources>,
    ) -> (Result<Option<VerifiedRegistryLaunch>>, bool) {
        let Self {
            app,
            runtime,
            job,
            invocation,
            cancellation,
            deadline,
            ..
        } = *self;
        let admitted = (|| {
            preparation_budget::check(deadline)?;
            job.ensure_accepting(&runtime.0.jobs)?;
            let budget = inspection.allocation_budget()?;
            let (schemas, digest) = selected_setup_component::activation_schemas()?;
            let read = OriginalActivationInspection::new(invocation)?;
            let control = invocation.control().reserve(
                ControlKind::Registration,
                (2 * std::mem::size_of::<authority::Retained>() + 1024) as u64,
            )?;
            let source =
                OriginalBootstrapInputs::new(app.clone(), job.source_custody()?, invocation, None)?;
            Ok::<_, anyhow::Error>((source, read, control, budget, schemas, digest))
        })();
        let (source, read, control, budget, schemas, digest) = match admitted {
            Ok(admitted) => admitted,
            Err(error) => return (Err(error), true),
        };

        let observed =
            caught(read.read(&source, cancellation, budget, schemas, &digest, deadline)).await;
        let receipt = read.close_receipt();
        if let Some(cleanup) = read.uncertain_cleanup() {
            runtime
                .0
                .retained_authority
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .push(authority::Retained::new(
                    cleanup,
                    source,
                    invocation.clone(),
                    cancellation.clone(),
                    control,
                ));
        }
        if !receipt.verified() {
            return (
                Err(anyhow!("native Launch Authority cleanup is uncertain")),
                false,
            );
        }

        let active = match observed {
            Ok(Ok(active)) => Some(active),
            Ok(Err(error)) => {
                // The original Host remains responsible for pending recovery.
                // Only a successful observation can supply native activation data.
                log::debug!(
                    "current v5 Authority observation is unavailable at {:?}",
                    error.stage
                );
                None
            }
            Err(error) => return (Err(anyhow!(error)), false),
        };
        (self.verify_registry(inspection, active.as_ref()), true)
    }

    fn verify_registry(
        &self,
        inspection: &NativeArtifactInspection<NativeApplicationSources>,
        active: Option<&OriginalActiveProfile>,
    ) -> Result<Option<VerifiedRegistryLaunch>> {
        let checkpoint = || {
            preparation_budget::check(self.deadline)?;
            self.job.ensure_accepting(&self.runtime.0.jobs)?;
            inspection.allocation_budget()?;
            Ok(())
        };
        checkpoint()?;
        let budget = inspection.allocation_budget()?;
        let _funding = self
            .invocation
            .control()
            .reserve(ControlKind::Frame, REGISTRY_VERIFICATION_BYTES)?;
        let provider_selection = active
            .map(|active| OriginalProviderPrepareSelection::select(active, budget))
            .transpose()?
            .flatten();
        let model_selection = active
            .map(|active| OriginalModelSettingsSelection::select(active, budget))
            .transpose()?
            .flatten();
        let model_save_selection = active
            .map(|active| OriginalModelSaveSelection::select(active, budget))
            .transpose()?
            .flatten();
        let shell_snapshot_selection = active
            .map(|active| OriginalShellProviderSnapshotSelection::select(active, budget))
            .transpose()?
            .flatten();
        let query_snapshot_selection = active
            .map(|active| OriginalQueryProviderSnapshotSelection::select(active, budget))
            .transpose()?
            .flatten();
        let model_access_snapshot_selection = active
            .map(|active| OriginalModelAccessProviderSnapshotSelection::select(active, budget))
            .transpose()?
            .flatten();
        let workflow_snapshot_selection = active
            .map(|active| OriginalWorkflowProviderSnapshotSelection::select(active, budget))
            .transpose()?
            .flatten();
        let model_resolve_selection = active
            .map(|active| OriginalModelResolveSelection::select(active, budget))
            .transpose()?
            .flatten();
        let conversation_read_selection = active
            .map(|active| OriginalConversationReadSelection::select(active, budget))
            .transpose()?
            .flatten();
        let saved_receipt_selection = active
            .map(|active| OriginalSavedReceiptSelection::select(active, budget))
            .transpose()?
            .flatten();
        let has_existing_grants = provider_selection.is_some()
            || model_selection.is_some()
            || model_resolve_selection.is_some()
            || conversation_read_selection.is_some()
            || saved_receipt_selection.is_some();
        let has_shell_snapshot_grants = shell_snapshot_selection.is_some()
            || model_save_selection.is_some()
            || query_snapshot_selection.is_some()
            || model_access_snapshot_selection.is_some();
        let (app_root, catalog_revision, locked, shell_snapshot, workflow) = inspection
            .with_source(|sources| {
                let catalog_revision = &sources.authority().catalog_revision;
                if let Some(active) = active {
                    validate_observed_activation(active, self.identity, catalog_revision)?;
                }
                let locked = has_existing_grants
                    .then(|| {
                        sources.registry_bundle(
                            provider_selection.as_ref(),
                            model_selection.as_ref(),
                            model_resolve_selection.as_ref(),
                            (
                                conversation_read_selection.as_ref(),
                                saved_receipt_selection.as_ref(),
                            ),
                            budget,
                        )
                    })
                    .transpose()?;
                let shell_snapshot = has_shell_snapshot_grants
                    .then(|| {
                        sources.shell_provider_snapshot_bundle(
                            shell_snapshot_selection.as_ref(),
                            model_save_selection.as_ref(),
                            query_snapshot_selection.as_ref(),
                            model_access_snapshot_selection.as_ref(),
                            budget,
                        )
                    })
                    .transpose()?;
                let workflow = workflow_snapshot_selection
                    .as_ref()
                    .map(|selection| sources.workflow_provider_snapshot_bundle(selection, budget))
                    .transpose()?;
                Ok::<_, anyhow::Error>((
                    sources.root(NativeSourceRoot::Application).to_path_buf(),
                    catalog_revision.clone(),
                    locked,
                    shell_snapshot,
                    workflow,
                ))
            })??;
        let Some(active) = active else {
            registry_pack_receipt::verify_installed(&app_root, REGISTRY_RECEIPT_SHA256, || {
                checkpoint().map_err(io::Error::other)
            })
            .context("bundled Registry Pack verification failed during Launch")?;
            return Ok(None);
        };
        if let Some(locked) = locked {
            locked.verify(checkpoint)?;
        }
        let [shell_settings, model_save_snapshot, model_search, command_datasource, model_access_read, model_access_catalog] =
            shell_snapshot
                .map(|bundle| bundle.verify(checkpoint))
                .transpose()?
                .unwrap_or_default();
        let workflow = workflow
            .map(|bundle| {
                let (entries, callers) = bundle.verify(checkpoint)?;
                Ok::<_, anyhow::Error>(VerifiedWorkflowProviderSnapshot::from_verified_routes(
                    entries, callers,
                ))
            })
            .transpose()?
            .unwrap_or_default();
        let mut local_chat = verify_saved_local_chat_source(inspection, active, checkpoint)?;
        let [gateway_direct, gateway_side_chat] = [false, true].map(|via_side_chat| {
            verify_gateway_provider_snapshot_entry(inspection, active, via_side_chat, checkpoint)
        });
        let gateway = [gateway_direct?, gateway_side_chat?];
        let mut gateway_health_selections =
            [const { [const { None }; OriginalModelResolveRole::ALL.len()] }; 2];
        for (side_chat, selections) in [false, true]
            .into_iter()
            .zip(&mut gateway_health_selections)
        {
            for role in OriginalModelResolveRole::ALL {
                selections[role.index()] =
                    OriginalGatewayProviderHealthChoice::select(active, budget, role, side_chat)?;
            }
        }
        let workflow_health_selection =
            OriginalWorkflowProviderSnapshotSelection::select_health(active, budget)?;
        let conversation_health_selection =
            OriginalConversationCompleteHealthChoice::select(active, budget)?;
        let [reviewer_quote, reviewer_generate] = OriginalReviewerHealthRole::ALL
            .map(|role| OriginalReviewerHealthChoice::select(active, budget, role));
        let reviewer_health_selections = [reviewer_quote?, reviewer_generate?];
        let has_health_grants = workflow_health_selection.is_some()
            || conversation_health_selection.is_some()
            || reviewer_health_selections.iter().any(Option::is_some)
            || gateway_health_selections
                .iter()
                .flatten()
                .any(Option::is_some);
        let has_snapshot_grants = local_chat.is_some()
            || gateway.iter().any(Option::is_some)
            || workflow.callers.iter().any(Option::is_some)
            || shell_settings.is_some()
            || model_save_snapshot.is_some()
            || model_search.is_some()
            || command_datasource.is_some()
            || model_access_read.is_some()
            || model_access_catalog.is_some();
        let verified_registry_pack_evidence =
            registry_pack_receipt::verify_installed_with_ai_routing_archives(
                &app_root,
                REGISTRY_RECEIPT_SHA256,
                || checkpoint().map_err(io::Error::other),
                |receipt, input| {
                    if provider_selection.is_none() && !has_snapshot_grants && !has_health_grants {
                        return Ok((None, None));
                    }
                    let pack = verify_registry_archive(inspection, receipt, input)?;
                    let model_access_projection_declared =
                        observe_model_access_projection_declaration(&pack)
                            .map_err(io::Error::other)?;
                    let prepare = provider_selection
                        .as_ref()
                        .map(|selection| {
                            require_provider_prepare_ports(&pack)?;
                            Ok::<_, io::Error>(VerifiedProviderPrepareSource {
                                coordinator: selection.coordinator().into(),
                                provider: selection.provider().into(),
                                archive_sha256: receipt.archive_sha256().to_owned(),
                                archive_bytes: receipt.archive_bytes(),
                                manifest_sha256: receipt.manifest_sha256().to_owned(),
                            })
                        })
                        .transpose()?;
                    let raw_snapshot_declared = has_snapshot_grants
                        && observe_provider_snapshot_declaration(&pack)
                            .map_err(io::Error::other)?;
                    let mut gateway_health =
                        [const { [const { None }; OriginalModelResolveRole::ALL.len()] }; 2];
                    let mut workflow_health = VerifiedWorkflowProviderSnapshot::default();
                    let mut conversation_health = None;
                    let mut reviewer_health =
                        [const { None }; OriginalReviewerHealthRole::ALL.len()];
                    let health_declared = has_health_grants
                        && observe_provider_health_declaration(&pack).map_err(io::Error::other)?;
                    if health_declared {
                        for (entry, choice) in
                            reviewer_health.iter_mut().zip(reviewer_health_selections)
                        {
                            let Some(choice) = choice else {
                                continue;
                            };
                            let locked = inspection
                                .with_source(|sources| {
                                    sources.reviewer_provider_health_bundle(&choice, budget)
                                })
                                .map_err(io::Error::other)?
                                .map_err(io::Error::other)?;
                            *entry = Some(locked.verify(&checkpoint).map_err(io::Error::other)?);
                        }
                        if let Some(choice) = &conversation_health_selection {
                            let locked = inspection
                                .with_source(|sources| {
                                    sources.conversation_complete_bundle(
                                        choice.complete(),
                                        Some(choice.health()),
                                        budget,
                                    )
                                })
                                .map_err(io::Error::other)?
                                .map_err(io::Error::other)?;
                            conversation_health = locked
                                .map(|bundle| bundle.verify(&checkpoint).map_err(io::Error::other))
                                .transpose()?;
                        }
                        if let Some(selection) = &workflow_health_selection {
                            let locked = inspection
                                .with_source(|sources| {
                                    sources.workflow_provider_snapshot_bundle(selection, budget)
                                })
                                .map_err(io::Error::other)?
                                .map_err(io::Error::other)?;
                            let (entries, callers) =
                                locked.verify(&checkpoint).map_err(io::Error::other)?;
                            workflow_health =
                                VerifiedWorkflowProviderSnapshot::from_verified_routes(
                                    entries, callers,
                                );
                        }
                        for (entry, choice) in gateway_health
                            .iter_mut()
                            .flatten()
                            .zip(gateway_health_selections.into_iter().flatten())
                        {
                            let Some(choice) = choice else {
                                continue;
                            };
                            let locked = inspection
                                .with_source(|sources| {
                                    sources.gateway_provider_health_bundle(&choice, budget)
                                })
                                .map_err(io::Error::other)?
                                .map_err(io::Error::other)?;
                            *entry = Some(locked.verify(&checkpoint).map_err(io::Error::other)?);
                        }
                    }
                    let snapshot = if raw_snapshot_declared || health_declared {
                        Some(VerifiedProviderSnapshotSource {
                            raw_snapshot_declared,
                            gateway,
                            gateway_health,
                            workflow,
                            workflow_health,
                            conversation_health,
                            reviewer_health,
                            shell_settings,
                            model_save: model_save_snapshot,
                            model_search,
                            command_datasource,
                            model_access_read,
                            model_access_catalog,
                            model_access_projection_declared,
                            archive_sha256: receipt.archive_sha256().to_owned(),
                            archive_bytes: receipt.archive_bytes(),
                            manifest_sha256: receipt.manifest_sha256().to_owned(),
                        })
                    } else {
                        None
                    };
                    Ok((prepare, snapshot))
                },
                |receipt, input| {
                    let model_list = model_selection.as_ref();
                    let model_save = model_save_selection.as_ref();
                    let model_resolve = model_resolve_selection.as_ref();
                    if model_list.is_none() && model_save.is_none() && model_resolve.is_none() {
                        return Ok((None, None, None));
                    }
                    let pack = verify_registry_archive(inspection, receipt, input)?;
                    let list = model_list
                        .map(|selection| {
                            require_model_list_port(&pack)?;
                            Ok::<_, io::Error>(VerifiedModelSettingsSource {
                                model_list: selection.model_list().into(),
                                archive_sha256: receipt.archive_sha256().to_owned(),
                                archive_bytes: receipt.archive_bytes(),
                                manifest_sha256: receipt.manifest_sha256().to_owned(),
                            })
                        })
                        .transpose()?;
                    let save = model_save
                        .map(|selection| {
                            verify_legacy_model_save_declaration(&pack)
                                .map_err(io::Error::other)?;
                            Ok::<_, io::Error>(VerifiedModelSaveSource {
                                model_manage: selection.model_manage().into(),
                                provider_read: selection.provider_read().into(),
                                archive_sha256: receipt.archive_sha256().to_owned(),
                                archive_bytes: receipt.archive_bytes(),
                                manifest_sha256: receipt.manifest_sha256().to_owned(),
                            })
                        })
                        .transpose()?;
                    let resolve = model_resolve
                        .map(|selection| {
                            verify_legacy_model_resolve_declaration(&pack)
                                .map_err(io::Error::other)?;
                            let roles = OriginalModelResolveRole::ALL
                                .map(|role| selection.pin(role).map(|pin| (role, pin.into())));
                            Ok::<_, io::Error>(VerifiedModelResolveSource {
                                roles,
                                archive_sha256: receipt.archive_sha256().to_owned(),
                                archive_bytes: receipt.archive_bytes(),
                                manifest_sha256: receipt.manifest_sha256().to_owned(),
                            })
                        })
                        .transpose()?;
                    Ok((list, save, resolve))
                },
                |receipt, input| {
                    let read = conversation_read_selection.as_ref();
                    let saved_receipt = saved_receipt_selection.as_ref();
                    let side_chat_saved_receipt =
                        verify_side_chat_saved_receipt_source(inspection, active, checkpoint)
                            .map_err(io::Error::other)?;
                    let has_side_chat_saved_receipt =
                        side_chat_saved_receipt.iter().any(Option::is_some);
                    let pack = verify_registry_archive(inspection, receipt, input)?;
                    if read.is_some() {
                        verify_legacy_conversation_read_declaration(&pack)
                            .map_err(io::Error::other)?;
                    }
                    let linked_context_declared =
                        observe_linked_conversation_context_declaration(&pack)
                            .map_err(io::Error::other)?;
                    let list_declared =
                        observe_conversation_list_declaration(&pack).map_err(io::Error::other)?;
                    if saved_receipt.is_some() || has_side_chat_saved_receipt {
                        verify_legacy_conversation_receipt_declaration(&pack)
                            .map_err(io::Error::other)?;
                    }
                    let confirmation_declared =
                        observe_conversation_confirmation_declaration(&pack)
                            .map_err(io::Error::other)?;
                    let append_declared =
                        observe_conversation_append_declaration(&pack).map_err(io::Error::other)?;
                    drop(pack);
                    let confirmation = if confirmation_declared {
                        verify_conversation_confirmation_source(inspection, active, checkpoint)
                            .map_err(io::Error::other)?
                    } else {
                        VerifiedConversationConfirmationRoles::default()
                    };
                    let append = if append_declared {
                        verify_conversation_append_source(inspection, active, checkpoint)
                            .map_err(io::Error::other)?
                    } else {
                        VerifiedConversationAppendRoles::default()
                    };
                    if read.is_none()
                        && saved_receipt.is_none()
                        && !has_side_chat_saved_receipt
                        && confirmation.is_empty()
                        && append.is_empty()
                    {
                        return Ok(None);
                    }
                    Ok(Some(VerifiedConversationSource {
                        shell: read.and_then(|selection| selection.shell()).map(Into::into),
                        saved_turn: read
                            .and_then(|selection| selection.saved_turn())
                            .map(Into::into),
                        saved_orchestration: read
                            .and_then(|selection| selection.saved_orchestration())
                            .map(Into::into),
                        linked_context_declared,
                        list_declared,
                        saved_receipt: OriginalSavedReceiptRole::ALL.map(|role| {
                            saved_receipt
                                .and_then(|selection| selection.pin(role))
                                .map(|pin| (role, pin.into()))
                        }),
                        side_chat_saved_receipt,
                        confirmation,
                        append,
                        archive_sha256: receipt.archive_sha256().to_owned(),
                        archive_bytes: receipt.archive_bytes(),
                        manifest_sha256: receipt.manifest_sha256().to_owned(),
                    }))
                },
                |receipt, input| {
                    verify_turn_source(inspection, active, receipt, input, checkpoint)
                        .map_err(io::Error::other)
                },
                |receipt, input| {
                    verify_saved_orchestration_source(
                        inspection, active, receipt, input, checkpoint,
                    )
                    .map_err(io::Error::other)
                },
                |receipt, input| {
                    let pack = verify_registry_archive(inspection, receipt, input)?;
                    require_provider_adapter_declaration(&pack).map_err(io::Error::other)?;
                    Ok(VerifiedProviderAdapterSource {
                        archive_sha256: receipt.archive_sha256().to_owned(),
                        archive_bytes: receipt.archive_bytes(),
                        manifest_sha256: receipt.manifest_sha256().to_owned(),
                    })
                },
                |receipt, input| {
                    verify_ai_routing_source(inspection, active, receipt, input, checkpoint)
                        .map_err(io::Error::other)
                },
            )
            .context("bundled Registry Pack verification failed during Launch")?;
        let (
            (provider_prepare, provider_snapshot),
            (model_settings, model_save, model_resolve),
            conversation,
            turn,
            orchestration,
            adapter,
            ai_routing,
        ) = verified_registry_pack_evidence;
        let turn = turn.flatten();
        let orchestration = orchestration.flatten();
        let ai_routing = ai_routing.flatten();
        if let Some(source) = &mut local_chat {
            source.adapter = adapter;
        }
        let application_presentation =
            registry_pack_receipt::verify_installed_with_application_presentation(
                &app_root,
                REGISTRY_RECEIPT_SHA256,
                || registry_checkpoint(inspection, &checkpoint),
                |receipt, input| {
                    verify_application_presentation_source(
                        inspection, active, receipt, input, checkpoint,
                    )
                    .map_err(io::Error::other)
                },
            )
            .context("bundled Application Presentation Pack verification failed during Launch")?
            .flatten();
        let conversation_complete =
            registry_pack_receipt::verify_installed_with_conversation_complete(
                &app_root,
                REGISTRY_RECEIPT_SHA256,
                || registry_checkpoint(inspection, &checkpoint),
                |receipt, input| {
                    verify_conversation_complete_source(
                        inspection, active, receipt, input, checkpoint,
                    )
                    .map_err(io::Error::other)
                },
            )
            .context("bundled Conversation Complete Pack verification failed during Launch")?
            .flatten();
        if !has_existing_grants
            && model_save.is_none()
            && conversation.is_none()
            && turn.is_none()
            && orchestration.is_none()
            && local_chat.is_none()
            && provider_snapshot.is_none()
            && ai_routing.is_none()
            && application_presentation.is_none()
            && conversation_complete.is_none()
        {
            return Ok(None);
        }
        let security_epoch = active
            .identity()
            .get_key("security_epoch")
            .and_then(|value| value.as_i64())
            .and_then(|epoch| u64::try_from(epoch).ok())
            .context("native Authority activation has an invalid security epoch")?;
        if provider_prepare.is_some() {
            log::info!("verified the selected old Provider prepare and its bundled Wasm replacement for this activation");
        }
        if model_settings.is_some() {
            log::info!("verified the selected old Model list and its bundled Wasm replacement for this activation");
        }
        if model_save.is_some() {
            log::info!("verified the selected old Model save grants and its bundled Wasm replacement for this activation");
        }
        if model_resolve.is_some() {
            log::info!("verified selected old Model Resolve grants and their bundled Wasm replacement for this activation");
        }
        if let Some(routing) = ai_routing
            .as_ref()
            .and_then(|source| source.routing.as_ref())
        {
            log::info!(
                "verified {} ordinary saved AI Routing role(s) for this activation",
                routing.roles.iter().flatten().count()
            );
        }
        if conversation_read_selection.is_some() {
            log::info!("verified selected old Conversation read grants and their bundled Wasm replacement for this activation");
        }
        if saved_receipt_selection.is_some() {
            log::info!("verified selected old Conversation saved receipt grants and their bundled Wasm replacement for this activation");
        }
        if turn
            .as_ref()
            .is_some_and(|source| source.settlement.iter().any(Option::is_some))
        {
            log::info!("verified selected old Turn settlement grants and their bundled Wasm replacement for this activation");
        }
        if turn.as_ref().is_some_and(|source| !source.start.is_empty()) {
            log::info!("verified the ordinary Saved Turn Lifecycle grants and their bundled Wasm replacement for this activation");
        }
        let verified = VerifiedRegistryLaunch {
            identity: self.identity.clone(),
            security_epoch,
            catalog_revision,
            provider_prepare,
            provider_snapshot,
            model_settings,
            model_save,
            model_resolve,
            ai_routing,
            conversation,
            turn,
            orchestration,
            local_chat,
            application_presentation,
            conversation_complete,
        };
        if let Some(local_chat) = &verified.local_chat {
            let mut roles = 0;
            for entry in [&local_chat.direct, &local_chat.side_chat]
                .into_iter()
                .flatten()
            {
                inspection.with_source(|sources| entry.bundle.require_source(sources))??;
                roles += entry.roles.iter().flatten().count();
            }
            log::info!("verified {roles} saved local Provider role(s) for this activation");
        }
        Ok(Some(verified))
    }
}

/// Reopen the verified read port after confirming this role's original installed Bundle.
pub(super) fn reacquire_application_presentation_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: ApplicationPresentationRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .application_presentation
        .as_ref()
        .context("admitted launch has no verified Application Presentation source")?;
    let bundle = source
        .bundle(role)
        .context("admitted launch has no verified Application Presentation grant for the caller")?;
    let app_root = registry_pack_root(inspection, proof)?;
    checkpoint()?;
    inspection.with_source(|sources| bundle.require_source(sources))??;
    bundle.revalidate(inspection.allocation_budget()?, &checkpoint)?;
    registry_pack_receipt::verify_installed_with_application_presentation(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            require_application_presentation_declaration(&pack).map_err(io::Error::other)?;
            Ok(pack)
        },
    )?
    .context("bundled Registry receipt has no Application Presentation archive")
}

/// Reopen the DATA port after confirming the original installed Conversation chain.
pub(super) fn reacquire_conversation_complete_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .conversation_complete
        .as_ref()
        .context("admitted launch has no verified Conversation Complete source")?;
    let app_root = registry_pack_root(inspection, proof)?;
    checkpoint()?;
    inspection.with_source(|sources| source.bundle.require_source(sources))??;
    source
        .bundle
        .revalidate(inspection.allocation_budget()?, &checkpoint)?;
    registry_pack_receipt::verify_installed_with_conversation_complete(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            require_conversation_complete_declaration(&pack).map_err(io::Error::other)?;
            Ok(pack)
        },
    )?
    .context("bundled Registry receipt has no Conversation Complete archive")
}

/// Reacquire the Provider Pack from the original signed resolver source and receipt pin.
pub(super) fn reacquire_provider_prepare_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .provider_prepare
        .as_ref()
        .context("admitted launch has no verified Provider preparation source")?;
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_provider(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            require_provider_prepare_ports(&pack)?;
            Ok(pack)
        },
    )
    .context("bundled Provider prepare archive verification failed")
}

/// Reopen the read-only Registry Pack after revalidating the caller's signed chain.
pub(super) fn reacquire_provider_snapshot_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalProviderSnapshotRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let (bundle, source) = proof
        .provider_snapshot(role)
        .context("admitted launch has no verified Provider Registry read grant for the role")?;
    checkpoint()?;
    let app_root = registry_pack_root(inspection, proof)?;
    inspection.with_source(|sources| bundle.require_source(sources))??;
    bundle.revalidate(inspection.allocation_budget()?, &checkpoint)?;
    registry_pack_receipt::verify_installed_with_provider_snapshot(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            if role.is_health() {
                verify_legacy_provider_health_declaration(&pack).map_err(io::Error::other)?;
            } else {
                verify_legacy_provider_snapshot_declaration(&pack).map_err(io::Error::other)?;
            }
            if proof.permits_model_access_projection(role) {
                verify_legacy_provider_model_access_declaration(&pack).map_err(io::Error::other)?;
            }
            Ok(pack)
        },
    )
    .context("bundled Provider Registry snapshot archive verification failed")
}

/// Reacquire the Model Pack from the original signed resolver source and receipt pin.
pub(super) fn reacquire_model_settings_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .model_settings
        .as_ref()
        .context("admitted launch has no verified Model settings source")?;
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_model(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            verify_model_settings_archive(inspection, receipt, input)
        },
    )
    .context("bundled Model settings archive verification failed")
}

/// Reacquire the Model Save Pack using its own launch evidence and fixed declaration.
pub(super) fn reacquire_model_save_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .model_save
        .as_ref()
        .context("admitted launch has no verified Model Save source")?;
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_model(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            verify_legacy_model_save_declaration(&pack).map_err(io::Error::other)?;
            Ok(pack)
        },
    )
    .context("bundled Model Save archive verification failed")
}

/// Reacquire the Model Pack using the Gateway role's grant and pinned archive.
pub(super) fn reacquire_model_resolve_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalModelResolveRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .model_resolve
        .as_ref()
        .filter(|source| source.permits(role))
        .context("admitted launch has no verified Model Resolve source for the Gateway role")?;
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_model(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            verify_legacy_model_resolve_declaration(&pack).map_err(io::Error::other)?;
            Ok(pack)
        },
    )
    .context("bundled Model Resolve archive verification failed")
}

/// Reacquire Routing only behind the original installed grants and pinned archive.
pub(super) fn reacquire_ai_routing_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalModelResolveRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .ai_routing
        .as_ref()
        .filter(|source| source.permits(role))
        .context("admitted launch has no verified AI Routing source for the Gateway role")?;
    let routing = source
        .routing
        .as_ref()
        .context("admitted launch has no verified AI Routing grants")?;
    let (pack, _) =
        reacquire_ai_routing_archive(inspection, proof, source, &routing.bundle, checkpoint)?;
    Ok(pack)
}

/// Reacquire the stream-result port behind its own signed Gateway Stream grant.
pub(super) fn reacquire_stream_result_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .ai_routing
        .as_ref()
        .context("admitted launch has no verified AI Routing archive")?;
    let stream_result = source
        .stream_result
        .as_ref()
        .context("admitted launch has no verified Gateway Stream normalization grant")?;
    let (pack, declaration) =
        reacquire_ai_routing_archive(inspection, proof, source, &stream_result.bundle, checkpoint)?;
    ensure!(
        declaration.has_stream_result(),
        "bundled AI Routing Pack has no verified stream-result port"
    );
    Ok(pack)
}

/// Reacquire the usage-cost port behind the Gateway role's signed installed grant.
pub(super) fn reacquire_usage_cost_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalModelResolveRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .ai_routing
        .as_ref()
        .context("admitted launch has no verified AI Routing archive")?;
    let usage_cost = source
        .usage_cost(role)
        .context("admitted launch has no verified usage-cost grant for the Gateway role")?;
    let (pack, declaration) =
        reacquire_ai_routing_archive(inspection, proof, source, &usage_cost.bundle, checkpoint)?;
    ensure!(
        declaration.has_usage_cost(),
        "bundled AI Routing Pack has no verified usage-cost port"
    );
    Ok(pack)
}

/// Reacquire the tool-intent normalization port behind the Gateway role's signed installed grant.
pub(super) fn reacquire_tool_intents_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalModelResolveRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .ai_routing
        .as_ref()
        .context("admitted launch has no verified AI Routing archive")?;
    let grant = source.tool_intents(role).context(
        "admitted launch has no verified tool-intent normalization grant for the Gateway role",
    )?;
    let (pack, declaration) =
        reacquire_ai_routing_archive(inspection, proof, source, &grant.bundle, checkpoint)?;
    ensure!(
        declaration.has_tool_intents(),
        "bundled AI Routing Pack has no verified tool-intent normalization port"
    );
    Ok(pack)
}

/// Reacquire the request-preparation port under this Gateway role's signed installed grant.
pub(super) fn reacquire_request_prepare_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalModelResolveRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .ai_routing
        .as_ref()
        .context("admitted launch has no verified AI Routing archive")?;
    let grant = source.request_prepare(role).context(
        "admitted launch has no verified request-preparation grant for the Gateway role",
    )?;
    let (pack, declaration) =
        reacquire_ai_routing_archive(inspection, proof, source, &grant.bundle, checkpoint)?;
    ensure!(
        declaration.has_request_prepare(),
        "bundled AI Routing Pack has no verified request-preparation port"
    );
    Ok(pack)
}

/// Reacquire the failover-decision port under the original Gateway Generate grant.
pub(super) fn reacquire_failover_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .ai_routing
        .as_ref()
        .context("admitted launch has no verified AI Routing archive")?;
    let grant = source
        .failover
        .as_ref()
        .context("admitted launch has no verified Gateway Generate failover grant")?;
    let (pack, declaration) =
        reacquire_ai_routing_archive(inspection, proof, source, &grant.bundle, checkpoint)?;
    ensure!(
        declaration.has_failover(),
        "bundled AI Routing Pack has no verified failover-decision port"
    );
    Ok(pack)
}

/// Reacquire the result port under the original saved Gateway grant for this role.
pub(super) fn reacquire_gateway_result_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalModelResolveRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .ai_routing
        .as_ref()
        .context("admitted launch has no verified AI Routing archive")?;
    let grant = source
        .gateway_result(role)
        .context("admitted launch has no verified ordinary saved Gateway grant for the role")?;
    let (pack, declaration) =
        reacquire_ai_routing_archive(inspection, proof, source, &grant.bundle, checkpoint)?;
    ensure!(
        declaration.has_gateway_result(),
        "bundled AI Routing Pack has no verified Gateway result port"
    );
    Ok(pack)
}

/// Reacquire the stateless policy port under the original saved Gateway grant.
pub(super) fn reacquire_gateway_policy_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalModelResolveRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .ai_routing
        .as_ref()
        .context("admitted launch has no verified AI Routing archive")?;
    let grant = source.gateway_policy(role).context(
        "admitted launch has no verified ordinary saved Gateway policy grant for the role",
    )?;
    let (pack, declaration) =
        reacquire_ai_routing_archive(inspection, proof, source, &grant.bundle, checkpoint)?;
    ensure!(
        declaration.has_gateway_policy(),
        "bundled AI Routing Pack has no verified Gateway policy port"
    );
    Ok(pack)
}

fn reacquire_ai_routing_archive(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    source: &VerifiedAiRoutingSource,
    bundle: &VerifiedOrdinarySavedBundle,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<(VerifiedPack, AiRoutingDeclaration)> {
    checkpoint()?;
    let app_root = registry_pack_root(inspection, proof)?;
    inspection.with_source(|sources| bundle.require_source(sources))??;
    bundle.revalidate(inspection.allocation_budget()?, &checkpoint)?;
    registry_pack_receipt::verify_installed_with_ai_routing(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            let declaration = require_ai_routing_declaration(&pack).map_err(io::Error::other)?;
            Ok((pack, declaration))
        },
    )
    .context("bundled AI Routing archive verification failed")?
    .context("bundled Registry receipt has no AI Routing archive")
}

/// Reacquire the Conversation Pack using the caller's own grant and pinned archive.
pub(crate) fn reacquire_conversation_read_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    caller: ConversationReadCaller,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .conversation
        .as_ref()
        .filter(|source| source.permits_read(caller))
        .context("admitted launch has no verified Conversation read source for the caller")?;
    reacquire_conversation_archive(
        inspection,
        proof,
        source,
        checkpoint,
        verify_legacy_conversation_read_declaration,
    )
    .context("bundled Conversation read archive verification failed")
}

/// Reopen the read-only list port against the admitted Shell read grant.
pub(super) fn reacquire_conversation_list_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .conversation
        .as_ref()
        .filter(|source| source.permits_list())
        .context("admitted launch has no verified Shell Conversation list source")?;
    reacquire_conversation_archive(
        inspection,
        proof,
        source,
        checkpoint,
        verify_legacy_conversation_list_declaration,
    )
    .context("bundled Conversation list archive verification failed")
}

/// Reopen the private linked context port against the admitted Saved Orchestration read grant.
pub(super) fn reacquire_linked_conversation_context_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .conversation
        .as_ref()
        .filter(|source| source.permits_linked_context())
        .context("admitted launch has no verified linked Conversation context source")?;
    reacquire_conversation_archive(
        inspection,
        proof,
        source,
        checkpoint,
        verify_legacy_conversation_linked_context_declaration,
    )
    .context("bundled linked Conversation context archive verification failed")
}

/// Reacquire the Conversation Pack using the Turn Runtime caller's receipt grant.
pub(super) fn reacquire_saved_receipt_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalSavedReceiptRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .conversation
        .as_ref()
        .filter(|source| source.permits_saved_receipt(role))
        .context(
            "admitted launch has no verified Conversation saved receipt source for the caller",
        )?;
    if let Some(bundle) = source.side_chat_saved_receipt_bundle(role) {
        checkpoint()?;
        inspection.with_source(|sources| bundle.require_source(sources))??;
        bundle.revalidate(inspection.allocation_budget()?, &checkpoint)?;
    }
    reacquire_conversation_archive(
        inspection,
        proof,
        source,
        checkpoint,
        verify_legacy_conversation_receipt_declaration,
    )
    .context("bundled Conversation saved receipt archive verification failed")
}

/// Reacquire the Conversation Pack using this caller's verified grant chain.
pub(super) fn reacquire_conversation_append_pack_for(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    caller: ConversationAppendCaller,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .conversation
        .as_ref()
        .context("admitted launch has no verified Conversation append source")?;
    let bundle = source
        .append
        .bundle(caller)
        .context("admitted launch has no verified Conversation append source for the caller")?;
    checkpoint()?;
    inspection.with_source(|sources| bundle.require_source(sources))??;
    bundle.revalidate(inspection.allocation_budget()?, &checkpoint)?;
    reacquire_conversation_archive(
        inspection,
        proof,
        source,
        checkpoint,
        verify_legacy_conversation_append_declaration,
    )
    .context("bundled Conversation append archive verification failed")
}

/// Reacquire the Conversation Pack using the exact confirmation chain for this role.
pub(super) fn reacquire_conversation_confirmation_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalSavedReceiptRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .conversation
        .as_ref()
        .filter(|source| source.permits_confirmation(role))
        .context(
            "admitted launch has no verified Conversation confirmation source for the caller",
        )?;
    source
        .confirmation
        .revalidate_side_chat(inspection, role, &checkpoint)?;
    reacquire_conversation_archive(
        inspection,
        proof,
        source,
        checkpoint,
        verify_legacy_conversation_confirmation_declaration,
    )
    .context("bundled Conversation confirmation archive verification failed")
}

/// Reacquire the Turn settlement Pack using the caller's signed grant and pinned archive.
pub(crate) fn reacquire_turn_settlement_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: SettlementCaller,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .turn
        .as_ref()
        .context("admitted launch has no verified Turn source")?;
    let grant = source
        .settlement_grant(role)
        .context("admitted launch has no verified Turn settlement source for the caller")?;
    checkpoint()?;
    inspection.with_source(|sources| grant.bundle.require_source(sources))??;
    grant
        .bundle
        .revalidate(inspection.allocation_budget()?, &checkpoint)?;
    reacquire_turn_archive(inspection, proof, source, checkpoint, |pack| {
        require_turn_settlement_declaration(pack).map(|_| ())
    })
    .context("bundled Turn settlement archive verification failed")
}

/// Reacquire the Turn Pack using this completion caller's signed grants and pinned read port.
pub(super) fn reacquire_turn_read_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalSavedReceiptRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .turn
        .as_ref()
        .filter(|source| source.permits_read(role))
        .context("admitted launch has no verified Turn read source for the caller")?;
    source
        .read
        .revalidate_side_chat(inspection, role, &checkpoint)?;
    reacquire_turn_archive(
        inspection,
        proof,
        source,
        checkpoint,
        require_turn_read_declaration,
    )
    .context("bundled Turn read archive verification failed")
}

/// Reacquire the Turn start port with the exact caller chain verified at Launch.
pub(super) fn reacquire_turn_start_pack_for(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    caller: SavedTurnStartCaller,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = revalidate_turn_start_source(inspection, proof, caller, &checkpoint)?;
    reacquire_turn_archive(
        inspection,
        proof,
        source,
        checkpoint,
        require_turn_start_declaration,
    )
    .context("bundled Turn start archive verification failed")
}

/// Reacquire the Turn input port under the ordinary signed Turn start chain.
pub(super) fn reacquire_saved_input_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    ensure!(
        proof.permits_saved_input(),
        "admitted launch has no verified ordinary Saved Input source"
    );
    let source = revalidate_turn_start_source(
        inspection,
        proof,
        SavedTurnStartCaller::SavedTurn,
        &checkpoint,
    )?;
    reacquire_turn_archive(
        inspection,
        proof,
        source,
        checkpoint,
        require_turn_input_declaration,
    )
    .context("bundled Turn input archive verification failed")
}

fn revalidate_turn_start_source<'proof>(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &'proof VerifiedRegistryLaunch,
    caller: SavedTurnStartCaller,
    checkpoint: &impl Fn() -> Result<()>,
) -> Result<&'proof VerifiedTurnSource> {
    let source = proof
        .turn
        .as_ref()
        .context("admitted launch has no verified Turn source")?;
    let start = source
        .start
        .bundle(caller)
        .context("admitted launch has no verified Turn start source for the caller")?;
    checkpoint()?;
    inspection.with_source(|sources| start.require_source(sources))??;
    start.revalidate(inspection.allocation_budget()?, checkpoint)?;
    Ok(source)
}

fn reacquire_turn_archive(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    source: &VerifiedTurnSource,
    checkpoint: impl Fn() -> Result<()>,
    verify_declaration: fn(&VerifiedPack) -> Result<()>,
) -> Result<VerifiedPack> {
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_settlement(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            verify_declaration(&pack).map_err(io::Error::other)?;
            Ok(pack)
        },
    )?
    .context("bundled Registry receipt has no Turn archive")
}

/// Bind the already admitted local Provider role to the original physical source.
/// The caller revalidates the retained bundle at the actual dispatch boundary.
pub(super) fn saved_local_chat_dispatch_bundle<'proof>(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &'proof VerifiedRegistryLaunch,
    role: OriginalSavedLocalChatRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<&'proof VerifiedOrdinarySavedBundle> {
    let source = proof
        .local_chat
        .as_ref()
        .and_then(|source| source.entry(role))
        .context("admitted launch has no verified local Provider source for the role")?;
    checkpoint()?;
    registry_pack_root(inspection, proof)?;
    inspection.with_source(|sources| source.bundle.require_source(sources))??;
    Ok(&source.bundle)
}

/// Reopen the effect-free Adapter paired with this exact original Provider grant.
pub(super) fn reacquire_saved_local_chat_adapter(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalSavedLocalChatRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<Option<VerifiedPack>> {
    let Some(adapter) = proof
        .local_chat
        .as_ref()
        .filter(|_| proof.permits_saved_local_chat(role))
        .and_then(|source| source.adapter.as_ref())
    else {
        return Ok(None);
    };
    let app_root = registry_pack_root(inspection, proof)?;
    let pack = registry_pack_receipt::verify_installed_with_provider_adapter(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &adapter.archive_sha256,
                adapter.archive_bytes,
                &adapter.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            require_provider_adapter_declaration(&pack).map_err(io::Error::other)?;
            Ok(pack)
        },
    )?;
    Ok(Some(pack.context(
        "bundled Registry receipt has no Provider Adapter archive",
    )?))
}

/// Reopen the installed candidate under the original authority source and Launch receipt.
pub(super) fn reacquire_saved_orchestration_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .orchestration
        .as_ref()
        .context("admitted launch has no verified Saved Orchestration source")?;
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_orchestration(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            require_saved_orchestration_declaration(&pack).map_err(io::Error::other)?;
            Ok(pack)
        },
    )?
    .context("bundled Registry receipt has no Saved Orchestration archive")
}

fn observe_linked_conversation_context_declaration(pack: &VerifiedPack) -> Result<bool> {
    let declared = registry_port_declared(pack, "local.saved-conversation.legacy-linked-context");
    if declared {
        verify_legacy_conversation_linked_context_declaration(pack)?;
    }
    Ok(declared)
}

fn observe_conversation_list_declaration(pack: &VerifiedPack) -> Result<bool> {
    let declared = registry_port_declared(pack, "local.saved-conversation.legacy-list");
    if declared {
        verify_legacy_conversation_list_declaration(pack)?;
    }
    Ok(declared)
}

fn observe_conversation_confirmation_declaration(pack: &VerifiedPack) -> Result<bool> {
    let declared = registry_port_declared(pack, "local.saved-conversation.legacy-confirmation");
    if declared {
        verify_legacy_conversation_confirmation_declaration(pack)?;
    }
    Ok(declared)
}

fn observe_conversation_append_declaration(pack: &VerifiedPack) -> Result<bool> {
    let declared = registry_port_declared(pack, "local.saved-conversation.legacy-append");
    if declared {
        verify_legacy_conversation_append_declaration(pack)?;
    }
    Ok(declared)
}

fn observe_provider_snapshot_declaration(pack: &VerifiedPack) -> Result<bool> {
    let declared = registry_port_declared(pack, "local.provider-registry.legacy-resource");
    if declared {
        verify_legacy_provider_snapshot_declaration(pack)?;
    }
    Ok(declared)
}

fn observe_provider_health_declaration(pack: &VerifiedPack) -> Result<bool> {
    let declared = registry_port_declared(pack, "local.provider-registry.legacy-health");
    if declared {
        verify_legacy_provider_health_declaration(pack)?;
    }
    Ok(declared)
}

fn observe_model_access_projection_declaration(pack: &VerifiedPack) -> Result<bool> {
    let declared = registry_port_declared(pack, "local.provider-registry.model-access-projection");
    if declared {
        verify_legacy_provider_model_access_declaration(pack)?;
    }
    Ok(declared)
}

fn registry_port_declared(pack: &VerifiedPack, name: &str) -> bool {
    pack.manifest
        .ports
        .provides
        .iter()
        .any(|port| port.shape.as_str() == name)
        || pack
            .shapes
            .iter()
            .any(|shape| shape.reference.name.as_str() == name)
}

fn reacquire_conversation_archive(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    source: &VerifiedConversationSource,
    checkpoint: impl Fn() -> Result<()>,
    verify_declaration: fn(&VerifiedPack) -> Result<()>,
) -> Result<VerifiedPack> {
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_conversation(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            verify_declaration(&pack).map_err(io::Error::other)?;
            Ok(pack)
        },
    )
    .map_err(Into::into)
}

fn registry_pack_root(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
) -> Result<std::path::PathBuf> {
    inspection.with_source(|sources| {
        let authority = sources.authority();
        ensure!(
            proof.identity.matches(&authority.execution_identity()?)
                && proof.catalog_revision == authority.catalog_revision,
            "signed Registry source differs from the admitted launch"
        );
        Ok(sources.root(NativeSourceRoot::Application).to_path_buf())
    })?
}

fn registry_checkpoint(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    checkpoint: &impl Fn() -> Result<()>,
) -> io::Result<()> {
    checkpoint().map_err(io::Error::other)?;
    inspection.allocation_budget().map_err(io::Error::other)?;
    Ok(())
}

fn require_archive_receipt(
    receipt: &registry_pack_receipt::Pack,
    archive_sha256: &str,
    archive_bytes: u64,
    manifest_sha256: &str,
) -> io::Result<()> {
    if receipt.archive_sha256() != archive_sha256
        || receipt.archive_bytes() != archive_bytes
        || receipt.manifest_sha256() != manifest_sha256
    {
        return Err(io::Error::other(
            "installed Registry receipt differs from the admitted launch",
        ));
    }
    Ok(())
}

fn verify_conversation_confirmation_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedConversationConfirmationRoles> {
    let budget = inspection.allocation_budget()?;
    let [saved_turn, saved_reconcile] = OriginalSavedReceiptRole::ALL;
    let choices = [
        OriginalConversationConfirmationChoice::select(active, budget, saved_turn)?,
        OriginalConversationConfirmationChoice::select(active, budget, saved_reconcile)?,
    ];
    if choices.iter().any(Option::is_some) {
        let locked = inspection.with_source(|sources| {
            sources.conversation_confirmation_bundle(choices.each_ref().map(Option::as_ref), budget)
        })??;
        locked.verify(&checkpoint)?;
    }
    let ordinary = std::array::from_fn(|index| {
        let role = OriginalSavedReceiptRole::ALL[index];
        choices[index].as_ref().map(|choice| {
            (
                role,
                [
                    choice.turn_caller().into(),
                    choice.completion().into(),
                    choice.turn_get().into(),
                ],
            )
        })
    });
    let mut side_chat = [None, None];
    for (bundle, role) in side_chat
        .iter_mut()
        .zip(OriginalSavedReceiptRole::SIDE_CHAT_CALLERS)
    {
        checkpoint()?;
        let Some(choice) = OriginalConversationConfirmationChoice::select(active, budget, role)?
        else {
            continue;
        };
        let locked = inspection
            .with_source(|sources| sources.side_chat_confirmation_bundle(&choice, budget))??;
        *bundle = Some((role, locked.verify(&checkpoint)?));
    }
    Ok(VerifiedConversationConfirmationRoles {
        ordinary,
        side_chat,
    })
}

fn verify_application_presentation_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<Option<VerifiedApplicationPresentationSource>> {
    let budget = inspection.allocation_budget()?;
    let mut bundles = [None, None, None];
    for (bundle, role) in bundles.iter_mut().zip(ApplicationPresentationRole::ALL) {
        checkpoint()?;
        let Some(choice) = OriginalApplicationPresentationChoice::select(active, budget, role)?
        else {
            continue;
        };
        if let Some(locked) = inspection
            .with_source(|sources| sources.application_presentation_bundle(&choice, budget))??
        {
            *bundle = Some(locked.verify(&checkpoint)?);
        }
    }
    if bundles.iter().all(Option::is_none) {
        return Ok(None);
    }
    let pack = verify_registry_archive(inspection, receipt, input)?;
    require_application_presentation_declaration(&pack)?;
    Ok(Some(VerifiedApplicationPresentationSource {
        bundles,
        archive_sha256: receipt.archive_sha256().to_owned(),
        archive_bytes: receipt.archive_bytes(),
        manifest_sha256: receipt.manifest_sha256().to_owned(),
    }))
}

fn verify_conversation_complete_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<Option<VerifiedConversationCompleteSource>> {
    let budget = inspection.allocation_budget()?;
    let Some(choice) = OriginalConversationCompleteChoice::select(active, budget)? else {
        return Ok(None);
    };
    let Some(locked) = inspection
        .with_source(|sources| sources.conversation_complete_bundle(&choice, None, budget))??
    else {
        return Ok(None);
    };
    let bundle = locked.verify(&checkpoint)?;
    let pack = verify_registry_archive(inspection, receipt, input)?;
    require_conversation_complete_declaration(&pack)?;
    Ok(Some(VerifiedConversationCompleteSource {
        bundle,
        archive_sha256: receipt.archive_sha256().to_owned(),
        archive_bytes: receipt.archive_bytes(),
        manifest_sha256: receipt.manifest_sha256().to_owned(),
    }))
}

fn verify_side_chat_saved_receipt_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedSideChatSavedRoles> {
    let budget = inspection.allocation_budget()?;
    let mut bundles = [None, None];
    for (bundle, role) in bundles
        .iter_mut()
        .zip(OriginalSavedReceiptRole::SIDE_CHAT_CALLERS)
    {
        checkpoint()?;
        let Some(choice) = OriginalSideChatSavedReceiptChoice::select(active, budget, role)? else {
            continue;
        };
        let locked = inspection
            .with_source(|sources| sources.side_chat_saved_receipt_bundle(&choice, budget))??;
        *bundle = Some((role, locked.verify(&checkpoint)?));
    }
    Ok(bundles)
}

fn verify_conversation_append_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedConversationAppendRoles> {
    let budget = inspection.allocation_budget()?;
    let verify = |caller| -> Result<Option<VerifiedOrdinarySavedBundle>> {
        let Some(choice) = OriginalConversationAppendChoice::select_for(active, budget, caller)?
        else {
            return Ok(None);
        };
        let locked = inspection
            .with_source(|sources| sources.conversation_append_bundle(&choice, budget))??;
        Ok(Some(locked.verify(&checkpoint)?))
    };
    Ok(VerifiedConversationAppendRoles {
        saved_turn: verify(ConversationAppendCaller::SavedTurn)?,
        side_chat: verify(ConversationAppendCaller::SideChat)?,
    })
}

fn verify_turn_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<Option<VerifiedTurnSource>> {
    let budget = inspection.allocation_budget()?;
    let mut selections = [None, None, None, None];
    for (selection, caller) in selections.iter_mut().zip(SettlementCaller::ALL) {
        *selection = select_original_saved_turn_settlement(active, budget, caller)?;
    }
    let read = if matches!(receipt.version(), "0.2.0" | "0.3.0" | TURN_INPUT_VERSION) {
        verify_conversation_confirmation_source(inspection, active, &checkpoint)?
    } else {
        VerifiedConversationConfirmationRoles::default()
    };
    let start = if matches!(receipt.version(), "0.3.0" | TURN_INPUT_VERSION) {
        verify_turn_start_source(inspection, active, &checkpoint)?
    } else {
        VerifiedTurnStartRoles::default()
    };
    let has_read = !read.is_empty();
    let has_settlement = selections.iter().any(Option::is_some);
    if !has_settlement && !has_read && start.is_empty() {
        return Ok(None);
    }
    let mut settlement = [None, None, None, None];
    if has_settlement {
        let locked = inspection.with_source(|sources| {
            sources.turn_settlement_bundle(selections.each_ref().map(Option::as_ref), budget)
        })??;
        let bundles = locked.verify(&checkpoint)?;
        for (grant, (selection, bundle)) in
            settlement.iter_mut().zip(selections.iter().zip(bundles))
        {
            if let Some(selection) = selection {
                *grant = Some(VerifiedTurnSettlementGrant {
                    caller: selection.role(),
                    side_chat: selection.side_chat().map(Into::into),
                    turn: selection.pin().into(),
                    bundle: bundle
                        .context("verified Turn settlement caller has no Bundle documents")?,
                });
            }
        }
    }
    let pack = verify_registry_archive(inspection, receipt, input)?;
    let declares_read = require_turn_settlement_declaration(&pack)?;
    ensure!(
        !has_read || declares_read,
        "bundled Turn Pack has no read port"
    );
    Ok(Some(VerifiedTurnSource {
        settlement,
        read,
        start,
        declares_saved_input: pack.manifest.version == TURN_INPUT_VERSION,
        archive_sha256: receipt.archive_sha256().to_owned(),
        archive_bytes: receipt.archive_bytes(),
        manifest_sha256: receipt.manifest_sha256().to_owned(),
    }))
}

fn verify_turn_start_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedTurnStartRoles> {
    let budget = inspection.allocation_budget()?;
    let verify = |caller| -> Result<Option<VerifiedTurnEntryBundle>> {
        let Some(choice) = OriginalSavedTurnStartChoice::select_for(active, budget, caller)? else {
            return Ok(None);
        };
        let locked =
            inspection.with_source(|sources| sources.turn_start_bundle(&choice, budget))??;
        Ok(Some(locked.verify(&checkpoint)?))
    };
    Ok(VerifiedTurnStartRoles {
        saved_turn: verify(SavedTurnStartCaller::SavedTurn)?,
        side_chat: verify(SavedTurnStartCaller::SideChat)?,
    })
}

fn verify_saved_orchestration_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<Option<VerifiedSavedOrchestrationSource>> {
    let budget = inspection.allocation_budget()?;
    let Some(choice) = OriginalSavedOrchestrationChoice::select(active, budget)? else {
        return Ok(None);
    };
    let locked = inspection.with_source(|sources| {
        sources.saved_orchestration_bundle(choice.pins().each_ref(), budget)
    })??;
    locked.verify(checkpoint)?;
    let pack = verify_registry_archive(inspection, receipt, input)?;
    require_saved_orchestration_declaration(&pack)?;
    Ok(Some(VerifiedSavedOrchestrationSource {
        route: choice.pins().each_ref().map(Into::into),
        archive_sha256: receipt.archive_sha256().to_owned(),
        archive_bytes: receipt.archive_bytes(),
        manifest_sha256: receipt.manifest_sha256().to_owned(),
    }))
}

fn verify_saved_local_chat_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<Option<VerifiedSavedLocalChatSource>> {
    use OriginalSavedLocalChatRole::{Generate, SideChatGenerate, SideChatStream, Stream};

    let budget = inspection.allocation_budget()?;
    let verify = |roles: [OriginalSavedLocalChatRole; 2],
                  via_side_chat|
     -> Result<Option<VerifiedSavedLocalChatEntry>> {
        let [generate, stream] =
            roles.map(|role| OriginalSavedLocalChatChoice::select(active, budget, role));
        let selections = [generate?, stream?];
        if selections.iter().all(Option::is_none) {
            return Ok(None);
        }
        let locked = inspection.with_source(|sources| {
            sources.saved_local_chat_bundle(
                selections.each_ref().map(Option::as_ref),
                via_side_chat,
                budget,
            )
        })??;
        let bundle = locked.verify(&checkpoint)?;
        let side_chat = selections
            .iter()
            .flatten()
            .find_map(OriginalSavedLocalChatChoice::side_chat)
            .map(Into::into);
        let roles = selections.map(|selection| {
            selection.map(|selected| (selected.role(), selected.pins().each_ref().map(Into::into)))
        });
        Ok(Some(VerifiedSavedLocalChatEntry {
            bundle,
            side_chat,
            roles,
        }))
    };
    let direct = verify([Generate, Stream], false)?;
    let side_chat = verify([SideChatGenerate, SideChatStream], true)?;
    if direct.is_none() && side_chat.is_none() {
        return Ok(None);
    }
    Ok(Some(VerifiedSavedLocalChatSource {
        direct,
        side_chat,
        adapter: None,
    }))
}

fn verify_gateway_provider_snapshot_entry(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    via_side_chat: bool,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<Option<VerifiedGatewayProviderSnapshotEntry>> {
    let budget = inspection.allocation_budget()?;
    let [quote_generate, quote_stream, generate, stream] =
        OriginalModelResolveRole::ALL.map(|role| {
            OriginalGatewayProviderSnapshotChoice::select(active, budget, role, via_side_chat)
        });
    let selections = [quote_generate?, quote_stream?, generate?, stream?];
    if selections.iter().all(Option::is_none) {
        return Ok(None);
    }
    let locked = inspection.with_source(|sources| {
        sources.gateway_provider_snapshot_bundle(
            selections.each_ref().map(Option::as_ref),
            via_side_chat,
            budget,
        )
    })??;
    let bundle = locked.verify(&checkpoint)?;
    let side_chat = selections
        .iter()
        .flatten()
        .find_map(OriginalGatewayProviderSnapshotChoice::side_chat)
        .map(Into::into);
    let mut roles = [None, None, None, None];
    for ((entry, role), selection) in roles
        .iter_mut()
        .zip(OriginalModelResolveRole::ALL)
        .zip(selections)
    {
        *entry = selection.map(|selected| (role, selected.pins().each_ref().map(Into::into)));
    }
    Ok(Some(VerifiedGatewayProviderSnapshotEntry {
        bundle,
        side_chat,
        roles,
    }))
}

fn verify_ai_routing_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<Option<VerifiedAiRoutingSource>> {
    let budget = inspection.allocation_budget()?;
    let routing_selection = OriginalAiRoutingSelection::select(active, budget)?;
    let stream_result_selection = if matches!(
        receipt.version(),
        STREAM_RESULT_PACK_VERSION
            | USAGE_COST_PACK_VERSION
            | TOOL_INTENTS_PACK_VERSION
            | PIPELINE_PACK_VERSION
            | GATEWAY_RESULT_PACK_VERSION
            | GATEWAY_POLICY_PACK_VERSION
    ) {
        OriginalStreamResultSelection::select(active, budget)?
    } else {
        None
    };
    let mut usage_cost_selections = [None, None];
    if matches!(
        receipt.version(),
        USAGE_COST_PACK_VERSION
            | TOOL_INTENTS_PACK_VERSION
            | PIPELINE_PACK_VERSION
            | GATEWAY_RESULT_PACK_VERSION
            | GATEWAY_POLICY_PACK_VERSION
    ) {
        for (selection, role) in usage_cost_selections.iter_mut().zip(AI_GATEWAY_ROLES) {
            *selection =
                OriginalUsageCostChoice::select(active, budget, role)?.map(|choice| (role, choice));
        }
    }
    let mut tool_intents_selections = [None, None];
    if matches!(
        receipt.version(),
        TOOL_INTENTS_PACK_VERSION
            | PIPELINE_PACK_VERSION
            | GATEWAY_RESULT_PACK_VERSION
            | GATEWAY_POLICY_PACK_VERSION
    ) {
        for (selection, role) in tool_intents_selections.iter_mut().zip(AI_GATEWAY_ROLES) {
            *selection = OriginalToolIntentsChoice::select(active, budget, role)?
                .map(|choice| (role, choice));
        }
    }
    let mut request_prepare_selections = [None, None, None, None];
    let mut failover_selection = None;
    if matches!(
        receipt.version(),
        PIPELINE_PACK_VERSION | GATEWAY_RESULT_PACK_VERSION | GATEWAY_POLICY_PACK_VERSION
    ) {
        failover_selection = OriginalFailoverChoice::select(active, budget)?;
        for (selection, role) in request_prepare_selections
            .iter_mut()
            .zip(OriginalModelResolveRole::ALL)
        {
            *selection = OriginalRequestPrepareChoice::select(active, budget, role)?
                .map(|choice| (role, choice));
        }
    }
    let mut saved_gateway_selections = [None, None, None];
    for (selection, role) in saved_gateway_selections.iter_mut().zip(SAVED_GATEWAY_ROLES) {
        if receipt.version() == GATEWAY_POLICY_PACK_VERSION
            || (receipt.version() == GATEWAY_RESULT_PACK_VERSION
                && AI_GATEWAY_ROLES.contains(&role))
        {
            *selection = OriginalSavedGatewayChoice::select(active, budget, role)?
                .map(|choice| (role, choice));
        }
    }
    if routing_selection.is_none()
        && stream_result_selection.is_none()
        && usage_cost_selections.iter().all(Option::is_none)
        && request_prepare_selections.iter().all(Option::is_none)
        && failover_selection.is_none()
        && tool_intents_selections.iter().all(Option::is_none)
        && saved_gateway_selections.iter().all(Option::is_none)
    {
        return Ok(None);
    }
    let pack = verify_registry_archive(inspection, receipt, input)?;
    let declaration = require_ai_routing_declaration(&pack)?;
    let routing = if let Some(selection) = routing_selection {
        let locked =
            inspection.with_source(|sources| sources.ai_routing_bundle(&selection, budget))??;
        let bundle = locked.verify(&checkpoint)?;
        let roles = OriginalModelResolveRole::ALL.map(|role| {
            selection
                .pins(role)
                .map(|pins| (role, pins.each_ref().map(Into::into)))
        });
        Some(VerifiedAiRoutingRoles { bundle, roles })
    } else {
        None
    };
    let stream_result = if let Some(selection) = stream_result_selection {
        ensure!(
            declaration.has_stream_result(),
            "bundled AI Routing Pack has no verified stream-result port"
        );
        let locked = inspection
            .with_source(|sources| sources.stream_result_bundle(&selection, budget))??;
        Some(VerifiedStreamResultRole {
            bundle: locked.verify(&checkpoint)?,
            pins: selection.pins().each_ref().map(Into::into),
        })
    } else {
        None
    };
    let mut usage_cost = [None, None];
    for (grant, selection) in usage_cost.iter_mut().zip(usage_cost_selections) {
        let Some((role, choice)) = selection else {
            continue;
        };
        ensure!(
            declaration.has_usage_cost(),
            "bundled AI Routing Pack has no verified usage-cost port"
        );
        let locked =
            inspection.with_source(|sources| sources.usage_cost_bundle(&choice, budget))??;
        *grant = Some(VerifiedAiGatewayRole {
            role,
            bundle: locked.verify(&checkpoint)?,
            pins: choice.pins().each_ref().map(Into::into),
        });
    }
    let mut tool_intents = [None, None];
    for (grant, selection) in tool_intents.iter_mut().zip(tool_intents_selections) {
        let Some((role, choice)) = selection else {
            continue;
        };
        ensure!(
            declaration.has_tool_intents(),
            "bundled AI Routing Pack has no verified tool-intent normalization port"
        );
        let locked =
            inspection.with_source(|sources| sources.tool_intents_bundle(&choice, budget))??;
        *grant = Some(VerifiedAiGatewayRole {
            role,
            bundle: locked.verify(&checkpoint)?,
            pins: choice.pins().each_ref().map(Into::into),
        });
    }
    let mut request_prepare = [None, None, None, None];
    for (grant, selection) in request_prepare.iter_mut().zip(request_prepare_selections) {
        let Some((role, choice)) = selection else {
            continue;
        };
        ensure!(
            declaration.has_request_prepare(),
            "bundled AI Routing Pack has no verified request-preparation port"
        );
        let locked =
            inspection.with_source(|sources| sources.request_prepare_bundle(&choice, budget))??;
        *grant = Some(VerifiedAiGatewayRole {
            role,
            bundle: locked.verify(&checkpoint)?,
            pins: choice.pins().each_ref().map(Into::into),
        });
    }
    let failover = if let Some(choice) = failover_selection {
        ensure!(
            declaration.has_failover(),
            "bundled AI Routing Pack has no verified failover-decision port"
        );
        let locked =
            inspection.with_source(|sources| sources.failover_bundle(&choice, budget))??;
        Some(VerifiedAiGatewayRole {
            role: OriginalModelResolveRole::Generate,
            bundle: locked.verify(&checkpoint)?,
            pins: choice.pins().each_ref().map(Into::into),
        })
    } else {
        None
    };
    let mut saved_gateway_grants = [None, None, None];
    for (grant, selection) in saved_gateway_grants
        .iter_mut()
        .zip(saved_gateway_selections)
    {
        let Some((role, choice)) = selection else {
            continue;
        };
        ensure!(
            declaration.has_gateway_policy()
                || (declaration.has_gateway_result() && AI_GATEWAY_ROLES.contains(&role)),
            "bundled AI Routing Pack has no verified stateless port for the saved Gateway role"
        );
        let locked =
            inspection.with_source(|sources| sources.saved_gateway_bundle(&choice, budget))??;
        *grant = Some(VerifiedAiGatewayRole {
            role,
            bundle: locked.verify(&checkpoint)?,
            pins: choice.pins().each_ref().map(Into::into),
        });
    }
    let saved_gateway =
        saved_gateway_grants
            .iter()
            .any(Option::is_some)
            .then_some(VerifiedSavedGatewaySource {
                grants: saved_gateway_grants,
                result: declaration.has_gateway_result(),
                policy: declaration.has_gateway_policy(),
            });
    Ok(Some(VerifiedAiRoutingSource {
        routing,
        stream_result,
        usage_cost,
        request_prepare,
        failover,
        tool_intents,
        saved_gateway,
        archive_sha256: receipt.archive_sha256().to_owned(),
        archive_bytes: receipt.archive_bytes(),
        manifest_sha256: receipt.manifest_sha256().to_owned(),
    }))
}

pub(super) const AI_ROUTING_SHAPE: &str = "local.ai-routing.routing@1.0";
pub(super) const AI_ROUTING_OPERATION: &str = "evaluate";
pub(super) const STREAM_RESULT_SHAPE: &str = "local.ai-routing.stream-result@1.0";
pub(super) const STREAM_RESULT_OPERATION: &str = "normalize";
pub(super) const USAGE_COST_SHAPE: &str = "local.ai-routing.usage-cost@1.0";
pub(super) const USAGE_COST_OPERATION: &str = "calculate";
pub(super) const TOOL_INTENTS_SHAPE: &str = "local.ai-routing.tool-intents@1.0";
pub(super) const TOOL_INTENTS_OPERATION: &str = "normalize";
pub(super) const REQUEST_PREPARE_SHAPE: &str = "local.ai-routing.request-prepare@1.0";
pub(super) const REQUEST_PREPARE_OPERATION: &str = "prepare";
pub(super) const FAILOVER_SHAPE: &str = "local.ai-routing.failover@1.0";
pub(super) const FAILOVER_OPERATION: &str = "decide";
pub(super) const GATEWAY_RESULT_SHAPE: &str = "local.ai-routing.gateway-result@1.0";
pub(super) const GATEWAY_RESULT_OPERATION: &str = "project";
pub(super) const GATEWAY_POLICY_SHAPE: &str = "local.ai-routing.gateway-policy@1.0";
pub(super) const GATEWAY_POLICY_OPERATION: &str = "project";
const GATEWAY_POLICY_PACK_VERSION: &str = "0.7.0";
const GATEWAY_RESULT_PACK_VERSION: &str = "0.6.0";
const PIPELINE_PACK_VERSION: &str = "0.5.0";
const STREAM_RESULT_PACK_VERSION: &str = "0.2.0";
const USAGE_COST_PACK_VERSION: &str = "0.3.0";
const TOOL_INTENTS_PACK_VERSION: &str = "0.4.0";
const AI_GATEWAY_ROLES: [OriginalModelResolveRole; 2] = [
    OriginalModelResolveRole::Generate,
    OriginalModelResolveRole::Stream,
];

const SAVED_GATEWAY_ROLES: [OriginalModelResolveRole; 3] = [
    OriginalModelResolveRole::Generate,
    OriginalModelResolveRole::Stream,
    OriginalModelResolveRole::QuoteGenerate,
];

// Published WIT and sidecar digests for earlier AI Routing Pack versions.
const AI_ROUTING_LEGACY_WIT: &str =
    "sha256:eab599c796a6dbc930e02f4b79f27a333e205e5f87231481893564dfdc924343";
const AI_ROUTING_STREAM_RESULT_WIT: &str =
    "sha256:417b89e703eac09fc7ff852e3c736c3c06473c25d91a4bf39401f3ecf811a528";
const AI_ROUTING_USAGE_COST_WIT: &str =
    "sha256:a6808c624f89f0feef62f2d8df783d7e7750ac6b0cfb39f7c57d70ccc138e779";
const AI_ROUTING_TOOL_INTENTS_WIT: &str =
    "sha256:4f70172ce8537aeb23fcf67b605a3608ec0232a03face1f9df1969c2c62b6289";
const AI_ROUTING_PIPELINE_WIT: &str =
    "sha256:93e81c7f38f59735f23075a06b4dcf01f8c5997531d149904ca253c40d5908e1";
const AI_ROUTING_GATEWAY_RESULT_WIT: &str =
    "sha256:169c11de102d54c130f530f8960ec1a3f770f9451d6b08850964daa07157b1e2";
const AI_ROUTING_LEGACY_CORE_WIT: &str =
    "sha256:b5604d1611c56eff3078e43b6f0ba30a10f6b2793c8fe9480c240c58a6c390d5";
const AI_ROUTING_LEGACY_SIDECAR: &str =
    "sha256:1faeaa2c351dc457027b827aa92f8cc78f133170ba8df1aade57ef5d82a464be";
const AI_ROUTING_WIT: (&str, &[u8]) = (
    "wit/pack.wit",
    include_bytes!("../../../packs/local.ai-routing-rust/wit/pack.wit"),
);
const AI_ROUTING_SHARED_WIT_FILES: [(&str, &[u8]); 2] = [
    CORE_V1_2_WIT,
    (
        "wit/routing.shape.json",
        include_bytes!("../../../packs/local.ai-routing-rust/wit/routing.shape.json"),
    ),
];
const STREAM_RESULT_SIDECAR: (&str, &[u8]) = (
    "wit/stream-result.shape.json",
    include_bytes!("../../../packs/local.ai-routing-rust/wit/stream-result.shape.json"),
);
const USAGE_COST_SIDECAR: (&str, &[u8]) = (
    "wit/usage-cost.shape.json",
    include_bytes!("../../../packs/local.ai-routing-rust/wit/usage-cost.shape.json"),
);
const TOOL_INTENTS_SIDECAR: (&str, &[u8]) = (
    "wit/tool-intents.shape.json",
    include_bytes!("../../../packs/local.ai-routing-rust/wit/tool-intents.shape.json"),
);

const REQUEST_PREPARE_SIDECAR: (&str, &[u8]) = (
    "wit/request-prepare.shape.json",
    include_bytes!("../../../packs/local.ai-routing-rust/wit/request-prepare.shape.json"),
);
const FAILOVER_SIDECAR: (&str, &[u8]) = (
    "wit/failover.shape.json",
    include_bytes!("../../../packs/local.ai-routing-rust/wit/failover.shape.json"),
);
const GATEWAY_RESULT_SIDECAR: (&str, &[u8]) = (
    "wit/gateway-result.shape.json",
    include_bytes!("../../../packs/local.ai-routing-rust/wit/gateway-result.shape.json"),
);

const GATEWAY_POLICY_SIDECAR: (&str, &[u8]) = (
    "wit/gateway-policy.shape.json",
    include_bytes!("../../../packs/local.ai-routing-rust/wit/gateway-policy.shape.json"),
);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum AiRoutingDeclaration {
    Routing,
    RoutingWithStreamResult,
    RoutingWithUsageCost,
    RoutingWithToolIntents,
    RoutingWithPipeline,
    RoutingWithGatewayResult,
    RoutingWithGatewayPolicy,
}

impl AiRoutingDeclaration {
    fn has_stream_result(self) -> bool {
        self != Self::Routing
    }

    fn has_usage_cost(self) -> bool {
        matches!(
            self,
            Self::RoutingWithUsageCost
                | Self::RoutingWithToolIntents
                | Self::RoutingWithPipeline
                | Self::RoutingWithGatewayResult
                | Self::RoutingWithGatewayPolicy
        )
    }

    fn has_tool_intents(self) -> bool {
        matches!(
            self,
            Self::RoutingWithToolIntents
                | Self::RoutingWithPipeline
                | Self::RoutingWithGatewayResult
                | Self::RoutingWithGatewayPolicy
        )
    }

    fn has_request_prepare(self) -> bool {
        matches!(
            self,
            Self::RoutingWithPipeline
                | Self::RoutingWithGatewayResult
                | Self::RoutingWithGatewayPolicy
        )
    }

    fn has_failover(self) -> bool {
        self.has_request_prepare()
    }

    fn has_gateway_result(self) -> bool {
        matches!(
            self,
            Self::RoutingWithGatewayResult | Self::RoutingWithGatewayPolicy
        )
    }

    fn has_gateway_policy(self) -> bool {
        self == Self::RoutingWithGatewayPolicy
    }
}

fn require_ai_routing_declaration(pack: &VerifiedPack) -> Result<AiRoutingDeclaration> {
    let manifest = &pack.manifest;
    let (declaration, wit, port_count) = match manifest.version.as_str() {
        "0.1.0" => (
            AiRoutingDeclaration::Routing,
            Digest::parse(AI_ROUTING_LEGACY_WIT)?,
            1,
        ),
        STREAM_RESULT_PACK_VERSION => (
            AiRoutingDeclaration::RoutingWithStreamResult,
            Digest::parse(AI_ROUTING_STREAM_RESULT_WIT)?,
            2,
        ),
        USAGE_COST_PACK_VERSION => (
            AiRoutingDeclaration::RoutingWithUsageCost,
            Digest::parse(AI_ROUTING_USAGE_COST_WIT)?,
            3,
        ),
        TOOL_INTENTS_PACK_VERSION => (
            AiRoutingDeclaration::RoutingWithToolIntents,
            Digest::parse(AI_ROUTING_TOOL_INTENTS_WIT)?,
            4,
        ),
        PIPELINE_PACK_VERSION => (
            AiRoutingDeclaration::RoutingWithPipeline,
            Digest::parse(AI_ROUTING_PIPELINE_WIT)?,
            6,
        ),
        GATEWAY_RESULT_PACK_VERSION => (
            AiRoutingDeclaration::RoutingWithGatewayResult,
            Digest::parse(AI_ROUTING_GATEWAY_RESULT_WIT)?,
            7,
        ),
        GATEWAY_POLICY_PACK_VERSION => (
            AiRoutingDeclaration::RoutingWithGatewayPolicy,
            Digest::hash(AI_ROUTING_WIT.1),
            8,
        ),
        _ => bail!("bundled AI Routing Pack has an unsupported version"),
    };
    verify_stateless_registry_surface(pack, registry_pack_receipt::AI_ROUTING_PACK_ID, port_count)?;
    let file_matches = |path, digest: &Digest| {
        pack.files
            .get(path)
            .is_some_and(|file| file.digest() == digest)
    };
    let shared_wit_matches = if declaration.has_request_prepare() {
        AI_ROUTING_SHARED_WIT_FILES
            .into_iter()
            .chain([
                STREAM_RESULT_SIDECAR,
                USAGE_COST_SIDECAR,
                TOOL_INTENTS_SIDECAR,
                REQUEST_PREPARE_SIDECAR,
                FAILOVER_SIDECAR,
            ])
            .all(|(path, bytes)| file_matches(path, &Digest::hash(bytes)))
    } else {
        let sidecar = Digest::parse(AI_ROUTING_LEGACY_SIDECAR)?;
        file_matches(CORE_V1_2_WIT.0, &Digest::parse(AI_ROUTING_LEGACY_CORE_WIT)?)
            && file_matches(AI_ROUTING_SHARED_WIT_FILES[1].0, &sidecar)
            && (!declaration.has_stream_result() || file_matches(STREAM_RESULT_SIDECAR.0, &sidecar))
            && (!declaration.has_tool_intents() || file_matches(TOOL_INTENTS_SIDECAR.0, &sidecar))
            && (!declaration.has_usage_cost() || file_matches(USAGE_COST_SIDECAR.0, &sidecar))
    };
    ensure!(
        manifest.files.len() == AI_ROUTING_SHARED_WIT_FILES.len() + port_count + 1
            && manifest
                .shapes
                .iter()
                .all(|shape| shape.wit == AI_ROUTING_WIT.0)
            && file_matches(AI_ROUTING_WIT.0, &wit)
            && shared_wit_matches
            && (!declaration.has_gateway_result()
                || file_matches(
                    GATEWAY_RESULT_SIDECAR.0,
                    &Digest::hash(GATEWAY_RESULT_SIDECAR.1)
                ))
            && (!declaration.has_gateway_policy()
                || file_matches(
                    GATEWAY_POLICY_SIDECAR.0,
                    &Digest::hash(GATEWAY_POLICY_SIDECAR.1)
                )),
        "bundled AI Routing Pack has unexpected WIT files"
    );
    require_registry_ports(pack, AI_ROUTING_OPERATION, &[AI_ROUTING_SHAPE])?;
    if declaration.has_stream_result() {
        require_registry_ports(pack, STREAM_RESULT_OPERATION, &[STREAM_RESULT_SHAPE])?;
    }
    if declaration.has_usage_cost() {
        require_registry_ports(pack, USAGE_COST_OPERATION, &[USAGE_COST_SHAPE])?;
    }
    if declaration.has_tool_intents() {
        require_registry_ports(pack, TOOL_INTENTS_OPERATION, &[TOOL_INTENTS_SHAPE])?;
    }
    if declaration.has_request_prepare() {
        require_registry_ports(pack, REQUEST_PREPARE_OPERATION, &[REQUEST_PREPARE_SHAPE])?;
    }
    if declaration.has_failover() {
        require_registry_ports(pack, FAILOVER_OPERATION, &[FAILOVER_SHAPE])?;
    }
    if declaration.has_gateway_result() {
        require_registry_ports(pack, GATEWAY_RESULT_OPERATION, &[GATEWAY_RESULT_SHAPE])?;
    }
    if declaration.has_gateway_policy() {
        require_registry_ports(pack, GATEWAY_POLICY_OPERATION, &[GATEWAY_POLICY_SHAPE])?;
    }
    Ok(declaration)
}

pub(super) const PROVIDER_ADAPTER_SHAPE: &str = "local.provider-adapter.local-completion@1.0";
pub(super) const PROVIDER_ADAPTER_OPERATION: &str = "project";
pub(super) const PROVIDER_REQUEST_SHAPE: &str = "local.provider-adapter.local-request@1.0";
pub(super) const PROVIDER_REQUEST_OPERATION: &str = "compile";
pub(super) const PROVIDER_POLICY_OPERATION: &str = "compile-with-policy";
pub(super) const PROVIDER_REQUEST_ADAPTER_VERSION: &str = "0.2.0";
pub(super) const PROVIDER_POLICY_ADAPTER_VERSION: &str = "0.3.0";
const PROVIDER_ADAPTER_LEGACY_WIT: &str =
    "sha256:4bb485fd5459f256fc6eaaf25eff05e43c9a485f0a67e61eafa1feeb53336610";
const PROVIDER_ADAPTER_REQUEST_WIT: &str =
    "sha256:ccd7db5753e6f5f1f7accf2b236767b009c879010f1a0b5dd98236c353c2337b";
const PROVIDER_ADAPTER_WIT: &[u8] =
    include_bytes!("../../../packs/local.provider-adapter-rust/wit/pack.wit");
const PROVIDER_ADAPTER_SHARED_FILES: [(&str, &[u8]); 2] = [
    CORE_V1_2_WIT,
    (
        "wit/local-completion.shape.json",
        include_bytes!(
            "../../../packs/local.provider-adapter-rust/wit/local-completion.shape.json"
        ),
    ),
];
const PROVIDER_REQUEST_SIDECAR: (&str, &[u8]) = (
    "wit/local-request.shape.json",
    include_bytes!("../../../packs/local.provider-adapter-rust/wit/local-request.shape.json"),
);

fn require_provider_adapter_declaration(pack: &VerifiedPack) -> Result<()> {
    let manifest = &pack.manifest;
    let (wit, ports) = match manifest.version.as_str() {
        "0.1.0" => (Digest::parse(PROVIDER_ADAPTER_LEGACY_WIT)?, 1),
        PROVIDER_REQUEST_ADAPTER_VERSION => (Digest::parse(PROVIDER_ADAPTER_REQUEST_WIT)?, 2),
        PROVIDER_POLICY_ADAPTER_VERSION => (Digest::hash(PROVIDER_ADAPTER_WIT), 2),
        _ => anyhow::bail!("bundled Provider Adapter Pack has an unsupported version"),
    };
    verify_stateless_registry_surface(
        pack,
        registry_pack_receipt::PROVIDER_ADAPTER_PACK_ID,
        ports,
    )?;
    ensure!(
        manifest.files.len() == PROVIDER_ADAPTER_SHARED_FILES.len() + ports + 1
            && manifest
                .shapes
                .iter()
                .all(|shape| shape.wit == "wit/pack.wit")
            && pack
                .files
                .get("wit/pack.wit")
                .is_some_and(|file| *file.digest() == wit)
            && PROVIDER_ADAPTER_SHARED_FILES
                .into_iter()
                .chain((ports == 2).then_some(PROVIDER_REQUEST_SIDECAR))
                .all(|(path, bytes)| pack
                    .files
                    .get(path)
                    .is_some_and(|file| *file.digest() == Digest::hash(bytes))),
        "bundled Provider Adapter Pack has unexpected WIT files"
    );
    require_registry_ports(pack, PROVIDER_ADAPTER_OPERATION, &[PROVIDER_ADAPTER_SHAPE])?;
    if manifest.version == PROVIDER_POLICY_ADAPTER_VERSION {
        let shape = ShapeRef::parse(PROVIDER_REQUEST_SHAPE)?;
        let operations = [
            OpName::parse(PROVIDER_REQUEST_OPERATION)?,
            OpName::parse(PROVIDER_POLICY_OPERATION)?,
        ];
        ensure!(
            declares_registry_port(pack, &shape, &operations),
            "bundled Provider Adapter has unexpected request operations"
        );
    } else if ports == 2 {
        require_registry_ports(pack, PROVIDER_REQUEST_OPERATION, &[PROVIDER_REQUEST_SHAPE])?;
    }
    Ok(())
}

fn require_turn_read_declaration(pack: &VerifiedPack) -> Result<()> {
    ensure!(
        require_turn_settlement_declaration(pack)?,
        "bundled Turn Pack has no read port"
    );
    Ok(())
}

fn require_turn_start_declaration(pack: &VerifiedPack) -> Result<()> {
    ensure!(
        matches!(pack.manifest.version.as_str(), "0.3.0" | TURN_INPUT_VERSION),
        "bundled Turn Pack has no verified start port"
    );
    require_turn_settlement_declaration(pack)?;
    Ok(())
}

fn require_turn_input_declaration(pack: &VerifiedPack) -> Result<()> {
    ensure!(
        pack.manifest.version == TURN_INPUT_VERSION,
        "bundled Turn Pack has no verified input port"
    );
    require_turn_settlement_declaration(pack)?;
    Ok(())
}

const CORE_V1_2_WIT: (&str, &[u8]) = (
    "wit/deps/core.wit",
    include_bytes!("../../../crates/tobkiri-wit/wit/core-v1-2/core.wit"),
);

const ORCHESTRATION_WIT_FILES: [(&str, &[u8]); 3] = [
    (
        "wit/pack.wit",
        include_bytes!("../../../packs/local.saved-orchestration-rust/wit/pack.wit"),
    ),
    CORE_V1_2_WIT,
    (
        "wit/legacy-step.shape.json",
        include_bytes!("../../../packs/local.saved-orchestration-rust/wit/legacy-step.shape.json"),
    ),
];

fn require_saved_orchestration_declaration(pack: &VerifiedPack) -> Result<()> {
    verify_stateless_registry_surface(pack, registry_pack_receipt::ORCHESTRATION_PACK_ID, 1)?;
    let manifest = &pack.manifest;
    ensure!(
        manifest.version == "0.1.0"
            && manifest.files.len() == ORCHESTRATION_WIT_FILES.len() + 1
            && manifest.shapes[0].wit == "wit/pack.wit"
            && ORCHESTRATION_WIT_FILES.into_iter().all(|(path, bytes)| {
                pack.files
                    .get(path)
                    .is_some_and(|file| *file.digest() == Digest::hash(bytes))
            }),
        "bundled Saved Orchestration Pack has an unexpected WIT or version"
    );
    require_registry_ports(
        pack,
        "advance",
        &["local.saved-orchestration.legacy-step@1.0"],
    )?;
    Ok(())
}

const CONVERSATION_COMPLETE_WIT: [(&str, &[u8]); 3] = [
    (
        "wit/pack.wit",
        include_bytes!("../../../packs/local.conversation-complete-rust/wit/pack.wit"),
    ),
    CORE_V1_2_WIT,
    (
        "wit/data.shape.json",
        include_bytes!("../../../packs/local.conversation-complete-rust/wit/data.shape.json"),
    ),
];

fn require_conversation_complete_declaration(pack: &VerifiedPack) -> Result<()> {
    verify_stateless_registry_surface(
        pack,
        registry_pack_receipt::CONVERSATION_COMPLETE_PACK_ID,
        1,
    )?;
    let manifest = &pack.manifest;
    ensure!(
        manifest.version == "0.1.0"
            && manifest.files.len() == CONVERSATION_COMPLETE_WIT.len() + 1
            && manifest.shapes[0].wit == "wit/pack.wit"
            && CONVERSATION_COMPLETE_WIT.into_iter().all(|(path, bytes)| {
                pack.files
                    .get(path)
                    .is_some_and(|file| *file.digest() == Digest::hash(bytes))
            }),
        "bundled Conversation Complete Pack has unexpected WIT or version"
    );
    ensure!(
        declares_registry_port(
            pack,
            &ShapeRef::parse("local.conversation-complete.data@1.0")?,
            &[OpName::parse("prepare")?, OpName::parse("finish")?],
        ),
        "bundled Conversation Complete Pack has no exact DATA port"
    );
    Ok(())
}

const APPLICATION_PRESENTATION_WIT: [(&str, &[u8]); 3] = [
    (
        "wit/pack.wit",
        include_bytes!("../../../packs/local.application-presentation-rust/registry-wit/pack.wit"),
    ),
    (
        "wit/deps/core.wit",
        include_bytes!("../../../crates/tobkiri-wit/wit/core-v1-2/core.wit"),
    ),
    (
        "wit/presentation.shape.json",
        include_bytes!(
            "../../../packs/local.application-presentation-rust/registry-wit/presentation.shape.json"
        ),
    ),
];

fn require_application_presentation_declaration(pack: &VerifiedPack) -> Result<()> {
    verify_stateless_registry_surface(
        pack,
        registry_pack_receipt::APPLICATION_PRESENTATION_PACK_ID,
        1,
    )?;
    let manifest = &pack.manifest;
    ensure!(
        manifest.version == "0.1.0"
            && manifest.files.len() == APPLICATION_PRESENTATION_WIT.len() + 1
            && manifest.shapes[0].wit == "wit/pack.wit"
            && APPLICATION_PRESENTATION_WIT
                .into_iter()
                .all(|(path, bytes)| {
                    pack.files
                        .get(path)
                        .is_some_and(|file| *file.digest() == Digest::hash(bytes))
                }),
        "bundled Application Presentation Pack has unexpected WIT or version"
    );
    require_registry_ports(
        pack,
        "read",
        &["local.application-presentation.presentation@1.0"],
    )?;
    Ok(())
}

// The staged declarations pin the exact ABI, including Core imports and cardinality.
const TURN_WIT: (&str, &[u8]) = (
    "wit/pack.wit",
    include_bytes!("../../../packs/local.saved-turn-runtime-rust/wit/pack.wit"),
);
const ORIGINAL_TURN_WIT_DIGEST: &str =
    "sha256:6308b8f72305ac93942d4596116f3ddbdcfe1f590b6617ced2e0a75b0a93056e";
const READ_TURN_WIT_DIGEST: &str =
    "sha256:c1c9ade00909c4c8d63eb64ad00fcd9e9b5ea9bcf96a821c492e8c2d0efbe8a6";
const START_TURN_WIT_DIGEST: &str =
    "sha256:0fe4841f35ba3986068facc99ca11880837acfe737cc9b79b5bc1ba72a62e3b5";
const TURN_INPUT_VERSION: &str = "0.4.0";
const TURN_READ_SHAPE: (&str, &[u8]) = (
    "wit/legacy-read.shape.json",
    include_bytes!("../../../packs/local.saved-turn-runtime-rust/wit/legacy-read.shape.json"),
);
const TURN_START_SHAPE: (&str, &[u8]) = (
    "wit/legacy-start.shape.json",
    include_bytes!("../../../packs/local.saved-turn-runtime-rust/wit/legacy-start.shape.json"),
);
const TURN_INPUT_SHAPE: (&str, &[u8]) = (
    "wit/legacy-input.shape.json",
    include_bytes!("../../../packs/local.saved-turn-runtime-rust/wit/legacy-input.shape.json"),
);
const TURN_SETTLEMENT_WIT_FILES: [(&str, &[u8]); 2] = [
    (
        "wit/deps/core.wit",
        include_bytes!("../../../crates/tobkiri-wit/wit/core-v1-2/core.wit"),
    ),
    (
        "wit/legacy-settlement.shape.json",
        include_bytes!(
            "../../../packs/local.saved-turn-runtime-rust/wit/legacy-settlement.shape.json"
        ),
    ),
];

fn require_turn_settlement_declaration(pack: &VerifiedPack) -> Result<bool> {
    const SETTLEMENT: &str = "local.saved-turn-runtime.legacy-settlement@1.0";
    const READ: &str = "local.saved-turn-runtime.legacy-read@1.0";
    const START: &str = "local.saved-turn-runtime.legacy-start@1.0";
    const INPUT: &str = "local.saved-turn-runtime.legacy-input@1.0";
    let manifest = &pack.manifest;
    let (ports, wit_digest) = match manifest.version.as_str() {
        "0.1.0" => (1, Digest::parse(ORIGINAL_TURN_WIT_DIGEST)?),
        "0.2.0" => (2, Digest::parse(READ_TURN_WIT_DIGEST)?),
        "0.3.0" => (3, Digest::parse(START_TURN_WIT_DIGEST)?),
        TURN_INPUT_VERSION => (4, Digest::hash(TURN_WIT.1)),
        _ => anyhow::bail!("bundled Turn settlement Pack version is unsupported"),
    };
    let read = ports >= 2;
    let start = ports >= 3;
    let input = ports == 4;
    verify_stateless_registry_surface(pack, registry_pack_receipt::SETTLEMENT_PACK_ID, ports)?;
    let matches_wit = |(path, bytes): (&str, &[u8])| {
        pack.files
            .get(path)
            .is_some_and(|file| *file.digest() == Digest::hash(bytes))
    };
    ensure!(
        manifest.shapes.iter().all(|shape| shape.wit == TURN_WIT.0)
            && manifest.files.len() == TURN_SETTLEMENT_WIT_FILES.len() + 1 + ports
            && TURN_SETTLEMENT_WIT_FILES.into_iter().all(matches_wit)
            && pack
                .files
                .get(TURN_WIT.0)
                .is_some_and(|file| *file.digest() == wit_digest)
            && (!read || matches_wit(TURN_READ_SHAPE))
            && (!start || matches_wit(TURN_START_SHAPE))
            && (!input || matches_wit(TURN_INPUT_SHAPE)),
        "bundled Turn settlement Pack has an unexpected WIT or runtime declaration"
    );
    require_registry_ports(pack, "prepare", &[SETTLEMENT])?;
    if read {
        require_registry_ports(pack, "validate", &[READ])?;
    }
    if start {
        ensure!(
            declares_registry_port(
                pack,
                &ShapeRef::parse(START)?,
                &[OpName::parse("begin")?, OpName::parse("claim")?],
            ),
            "bundled Turn Pack has no exact legacy start port"
        );
    }
    if input {
        ensure!(
            declares_registry_port(
                pack,
                &ShapeRef::parse(INPUT)?,
                &[
                    OpName::parse("identify")?,
                    OpName::parse("prepare")?,
                    OpName::parse("recover")?,
                ],
            ),
            "bundled Turn Pack has no exact legacy input port"
        );
    }
    Ok(read)
}

fn verify_stateless_registry_surface(pack: &VerifiedPack, id: &str, ports: usize) -> Result<()> {
    let manifest = &pack.manifest;
    let permissions = PermissionSet {
        wasm: manifest.permissions.wasm.clone(),
        loops: manifest.permissions.loops.clone(),
        ..PermissionSet::default()
    };
    ensure!(
        manifest.id.as_str() == id
            && manifest.shapes.len() == ports
            && manifest.ports.provides.len() == ports
            && manifest.ports.consumes.is_empty()
            && manifest.ports.untyped.is_empty()
            && manifest.inputs.is_empty()
            && manifest.flows.is_empty()
            && manifest.overlays.is_empty()
            && manifest.stores.is_empty()
            && manifest.ui.is_none()
            && manifest.permissions == permissions,
        "bundled {id} Pack has unexpected capabilities"
    );
    ensure!(
        matches!(&manifest.runtime, RuntimeSpec::Wasm { component, core_modules, config, .. }
            if component == "component.wasm" && core_modules.is_empty() && config.is_none()),
        "bundled {id} Pack has an unexpected runtime declaration"
    );
    Ok(())
}

fn require_provider_prepare_ports(pack: &VerifiedPack) -> io::Result<()> {
    require_registry_ports(
        pack,
        "prepare-local",
        &[
            "local.provider-registry.legacy-configuration@1.0",
            "local.provider-registry.legacy-configuration-live@1.0",
        ],
    )
}

fn verify_model_settings_archive(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
) -> io::Result<VerifiedPack> {
    let pack = verify_registry_archive(inspection, receipt, input)?;
    require_model_list_port(&pack)?;
    Ok(pack)
}

fn require_model_list_port(pack: &VerifiedPack) -> io::Result<()> {
    require_registry_ports(
        pack,
        "list-models",
        &["local.model-registry.legacy-settings@1.0"],
    )
}

fn verify_registry_archive(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
) -> io::Result<VerifiedPack> {
    (|| {
        let pack = inspection.verify_pack_archive(input, receipt.archive_bytes())?;
        ensure!(
            pack.manifest.id.as_str() == receipt.id()
                && pack.manifest.version == receipt.version()
                && pack.hash.as_str() == receipt.manifest_sha256()
                && matches!(&pack.manifest.runtime, RuntimeSpec::Wasm { .. }),
            "Registry Pack manifest differs from the bundled receipt"
        );
        Ok::<_, anyhow::Error>(pack)
    })()
    .map_err(io::Error::other)
}

fn require_registry_ports(pack: &VerifiedPack, operation: &str, shapes: &[&str]) -> io::Result<()> {
    (|| {
        let operation = OpName::parse(operation)?;
        for name in shapes {
            let shape = ShapeRef::parse(name)?;
            ensure!(
                declares_registry_port(pack, &shape, std::slice::from_ref(&operation)),
                "bundled Registry Pack has no exact legacy {operation} port {name}"
            );
        }
        Ok(())
    })()
    .map_err(io::Error::other)
}

fn declares_registry_port(pack: &VerifiedPack, shape: &ShapeRef, operations: &[OpName]) -> bool {
    pack.manifest
        .ports
        .provides
        .iter()
        .any(|port| port.shape == shape.name && port.version == shape.version && !port.optional)
        && pack.shapes.iter().any(|definition| {
            &definition.reference == shape
                && definition.kind == ShapeKind::Port
                && definition.functions.len() == operations.len()
                && operations
                    .iter()
                    .all(|op| definition.functions.contains_key(op))
                && definition.streams.is_empty()
                && definition.callbacks.is_empty()
        })
}

fn validate_observed_activation(
    active: &OriginalActiveProfile,
    identity: &ExecutionProfileIdentity,
    catalog_revision: &str,
) -> Result<()> {
    let (_, _, plan) = active.records();
    ensure!(
        [
            ("profile_id", identity.profile_id.as_str()),
            ("profile_revision", identity.profile_revision.as_str()),
            ("activation_id", identity.activation_id.as_str()),
            ("plan_digest", identity.plan_digest.as_str()),
        ]
        .into_iter()
        .all(|(field, expected)| {
            active
                .identity()
                .get_key(field)
                .is_some_and(|value| value.as_str() == Some(expected))
        }) && plan
            .get_key("catalog_revision")
            .is_some_and(|value| value.as_str() == Some(catalog_revision)),
        "native Authority activation differs from the signed Shell selection"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::native_harness_bootstrap::NativeRegistryRequest;

    #[test]
    fn provider_snapshot_requires_its_port_and_the_exact_saved_adapter_role() {
        use OriginalSavedLocalChatRole::{Generate, SideChatGenerate, SideChatStream, Stream};

        let digest = Digest::hash(b"provider-snapshot-activation");
        let identity = ExecutionProfileIdentity::new(
            "defaults",
            digest.as_str(),
            "activation:provider-snapshot-test",
            Digest::hash(b"plan").as_str(),
        )
        .unwrap();
        let roles = [Generate, Stream, SideChatGenerate, SideChatStream];
        let prepare_only = VerifiedRegistryLaunch::test_proof(identity.clone(), digest.as_str());
        for role in roles {
            assert!(!prepare_only.permits_provider_snapshot(role.into()));
            let older_pack = VerifiedRegistryLaunch::test_saved_local_chat_proof(
                identity.clone(),
                digest.as_str(),
                role,
            );
            assert!(older_pack.permits_saved_local_chat(role));
            assert!(!older_pack.permits_provider_snapshot(role.into()));

            let proof = VerifiedRegistryLaunch::test_provider_snapshot_proof(
                identity.clone(),
                digest.as_str(),
                role,
            );
            assert!(proof.provider_prepare.is_none());
            assert!(!proof.permits_gateway_provider_snapshot());
            assert!(!proof.permits_projected_provider_snapshot());
            for candidate in roles {
                assert_eq!(
                    proof.permits_provider_snapshot(candidate.into()),
                    candidate == role
                );
            }
        }
    }

    #[test]
    fn gateway_health_launch_proof_keeps_saved_roles_and_snapshot_port_independent() {
        use OriginalProviderSnapshotRole::{
            ConversationCompleteHealth, Gateway, GatewayHealth, ReviewerHealth,
        };
        use OriginalSavedLocalChatRole::Generate;

        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let gateway = Gateway {
            role: OriginalModelResolveRole::Generate,
            side_chat: false,
        };
        let older_pack = VerifiedRegistryLaunch::test_provider_snapshot_proof(
            identity.clone(),
            "catalog",
            gateway,
        );
        assert!(older_pack.permits_provider_snapshot(gateway));

        let health_roles = || {
            [false, true].into_iter().flat_map(|side_chat| {
                OriginalModelResolveRole::ALL.map(|role| GatewayHealth { role, side_chat })
            })
        };
        let complete = VerifiedRegistryLaunch::test_provider_snapshot_proof(
            identity.clone(),
            "catalog",
            ConversationCompleteHealth,
        );
        assert!(complete.permits_provider_snapshot(ConversationCompleteHealth));
        assert!(complete.raw_provider_snapshot().is_none());
        assert!(!complete.permits_workflow_provider_health());
        for granted in OriginalReviewerHealthRole::ALL {
            let review = VerifiedRegistryLaunch::test_provider_snapshot_proof(
                identity.clone(),
                "catalog",
                ReviewerHealth(granted),
            );
            assert!(review.raw_provider_snapshot().is_none());
            assert!(!review.permits_provider_snapshot(ConversationCompleteHealth));
            for candidate in OriginalReviewerHealthRole::ALL {
                assert_eq!(
                    review.permits_provider_snapshot(ReviewerHealth(candidate)),
                    granted == candidate
                );
                assert!(!older_pack.permits_provider_snapshot(ReviewerHealth(candidate)));
            }
            assert!(health_roles().all(|role| !review.permits_provider_snapshot(role)));
        }
        for health in health_roles() {
            assert!(!older_pack.permits_provider_snapshot(health));
            assert!(!complete.permits_provider_snapshot(health));
            let mut proof = VerifiedRegistryLaunch::test_provider_snapshot_proof_with_roles(
                identity.clone(),
                "catalog",
                [health, gateway, Generate.into()],
            );
            for candidate in health_roles() {
                assert_eq!(
                    proof.permits_provider_snapshot(candidate),
                    candidate == health,
                );
            }
            assert!(proof.permits_provider_snapshot(gateway));
            assert!(!proof.permits_provider_snapshot(ConversationCompleteHealth));
            assert!(proof.permits_provider_snapshot(Generate.into()));

            proof
                .provider_snapshot
                .as_mut()
                .unwrap()
                .raw_snapshot_declared = false;
            assert!(proof.permits_provider_snapshot(health));
            assert!(!proof.permits_provider_snapshot(gateway));
            assert!(!proof.permits_provider_snapshot(Generate.into()));
            assert!(!proof.permits_gateway_provider_snapshot());
        }
    }

    #[test]
    fn provider_snapshot_requires_the_exact_gateway_grant_and_saved_entry() {
        let digest = Digest::hash(b"gateway-provider-snapshot-activation");
        let identity = ExecutionProfileIdentity::new(
            "defaults",
            digest.as_str(),
            "activation:gateway-provider-snapshot-test",
            Digest::hash(b"plan").as_str(),
        )
        .unwrap();
        for role in OriginalModelResolveRole::ALL {
            for side_chat in [false, true] {
                let granted = OriginalProviderSnapshotRole::Gateway { role, side_chat };
                let proof = VerifiedRegistryLaunch::test_provider_snapshot_proof(
                    identity.clone(),
                    digest.as_str(),
                    granted,
                );
                assert!(proof.permits_gateway_provider_snapshot());
                assert!(!proof.permits_projected_provider_snapshot());
                assert!(
                    !proof.permits_provider_snapshot(OriginalSavedLocalChatRole::Generate.into())
                );
                for candidate_role in OriginalModelResolveRole::ALL {
                    for candidate_side_chat in [false, true] {
                        let candidate = OriginalProviderSnapshotRole::Gateway {
                            role: candidate_role,
                            side_chat: candidate_side_chat,
                        };
                        assert_eq!(
                            proof.permits_provider_snapshot(candidate),
                            candidate == granted
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn projected_provider_snapshot_proof_requires_the_individual_read_grant() {
        use OriginalProviderSnapshotRole::{
            Adapter, CommandDatasource, Gateway, ModelSave, ModelSearch, ShellSettings,
        };

        let digest = Digest::hash(b"projected-provider-snapshot-activation");
        let identity = ExecutionProfileIdentity::new(
            "defaults",
            digest.as_str(),
            "activation:projected-provider-snapshot-test",
            Digest::hash(b"plan").as_str(),
        )
        .unwrap();
        let native_save =
            VerifiedRegistryLaunch::test_model_save_proof(identity.clone(), digest.as_str());
        assert!(!native_save.permits_projected_provider_snapshot());
        assert!(!native_save.permits_query_provider_snapshot());
        for role in [ShellSettings, ModelSave, ModelSearch, CommandDatasource] {
            assert!(!native_save.permits_provider_snapshot(role));
            let mut proof = VerifiedRegistryLaunch::test_provider_snapshot_proof(
                identity.clone(),
                digest.as_str(),
                role,
            );
            assert!(proof.model_save.is_none());
            let query_role = matches!(role, ModelSearch | CommandDatasource);
            assert_eq!(proof.permits_query_provider_snapshot(), query_role);
            assert_eq!(proof.permits_projected_provider_snapshot(), !query_role);
            assert!(!proof.permits_gateway_provider_snapshot());
            for candidate in [
                ShellSettings,
                ModelSave,
                ModelSearch,
                CommandDatasource,
                Adapter(OriginalSavedLocalChatRole::Generate),
                Gateway {
                    role: OriginalModelResolveRole::Generate,
                    side_chat: false,
                },
            ] {
                assert_eq!(
                    proof.permits_provider_snapshot(candidate),
                    candidate == role
                );
            }

            proof.provider_snapshot = None;
            assert!(!proof.permits_projected_provider_snapshot());
            assert!(!proof.permits_query_provider_snapshot());
            assert!(!proof.permits_provider_snapshot(role));
        }
    }

    #[test]
    fn model_access_provider_snapshot_proof_requires_its_own_grant_and_port() {
        use OriginalProviderSnapshotRole::{
            CommandDatasource, ModelAccessCatalog, ModelAccessRead, ModelSave, ModelSearch,
            ShellSettings,
        };

        let digest = Digest::hash(b"model-access-provider-snapshot-activation");
        let identity = ExecutionProfileIdentity::new(
            "defaults",
            digest.as_str(),
            "activation:model-access-provider-snapshot-test",
            Digest::hash(b"plan").as_str(),
        )
        .unwrap();
        let roles = [ModelAccessRead, ModelAccessCatalog];
        let other_roles = [ShellSettings, ModelSave, ModelSearch, CommandDatasource];
        let unrelated = VerifiedRegistryLaunch::test_provider_snapshot_proof_with_roles(
            identity.clone(),
            digest.as_str(),
            other_roles,
        );
        assert!(!unrelated.permits_model_access_provider_snapshot());
        for role in roles {
            assert!(!unrelated.permits_provider_snapshot(role));
            let raw_proof = VerifiedRegistryLaunch::test_provider_snapshot_proof(
                identity.clone(),
                digest.as_str(),
                role,
            );
            assert!(raw_proof.permits_model_access_provider_snapshot());
            assert!(!raw_proof.permits_model_access_projection(role));
            let mut proof = VerifiedRegistryLaunch::test_model_access_projection_proof(
                identity.clone(),
                digest.as_str(),
                role,
            );
            assert!(proof.permits_model_access_provider_snapshot());
            for candidate in roles.into_iter().chain(other_roles) {
                assert_eq!(
                    proof.permits_provider_snapshot(candidate),
                    candidate == role,
                );
                assert_eq!(
                    proof.permits_model_access_projection(candidate),
                    candidate == role,
                );
            }
            proof.provider_snapshot = None;
            assert!(!proof.permits_model_access_provider_snapshot());
            assert!(!proof.permits_provider_snapshot(role));
            assert!(!proof.permits_model_access_projection(role));
        }
    }

    #[test]
    fn workflow_provider_snapshot_proof_keeps_caller_entry_pairs_independent() {
        use OriginalProviderSnapshotRole::{Gateway, ShellSettings, Workflow, WorkflowHealth};
        use OriginalWorkflowSnapshotCaller::{AudioSpeech, Text};
        use OriginalWorkflowSnapshotEntry::{Advance, StepResume};

        let digest = Digest::hash(b"workflow-provider-snapshot-activation");
        let identity = ExecutionProfileIdentity::new(
            "defaults",
            digest.as_str(),
            "activation:workflow-provider-snapshot-test",
            Digest::hash(b"plan").as_str(),
        )
        .unwrap();
        let gateway = Gateway {
            role: OriginalModelResolveRole::Generate,
            side_chat: false,
        };
        let workflow = [
            Workflow {
                caller: Text,
                entry: Advance,
            },
            Workflow {
                caller: AudioSpeech,
                entry: StepResume,
            },
        ];
        let proof = VerifiedRegistryLaunch::test_provider_snapshot_proof_with_roles(
            identity.clone(),
            "catalog",
            [workflow[0], workflow[1], gateway, ShellSettings],
        );
        assert!(proof.permits_workflow_provider_snapshot());
        assert!(!proof.permits_workflow_adapter_provider_snapshot());
        assert!(!proof.permits_workflow_provider_health());
        assert!(proof.permits_gateway_provider_snapshot());
        assert!(proof.permits_projected_provider_snapshot());
        for caller in OriginalWorkflowSnapshotCaller::ALL {
            for entry in OriginalWorkflowSnapshotEntry::ALL {
                let candidate = Workflow { caller, entry };
                assert_eq!(
                    proof.permits_provider_snapshot(candidate),
                    workflow.contains(&candidate)
                );
            }
        }
        let gateway_only = VerifiedRegistryLaunch::test_provider_snapshot_proof(
            identity.clone(),
            "catalog",
            gateway,
        );
        assert!(!gateway_only.permits_workflow_provider_snapshot());
        assert!(!gateway_only.permits_provider_snapshot(workflow[0]));

        let health = [
            WorkflowHealth {
                caller: OriginalWorkflowHealthCaller::Text,
                entry: Advance,
            },
            WorkflowHealth {
                caller: OriginalWorkflowHealthCaller::Messages,
                entry: StepResume,
            },
        ];
        let health_only = VerifiedRegistryLaunch::test_provider_snapshot_proof_with_roles(
            identity, "catalog", health,
        );
        assert!(health_only.permits_workflow_provider_health());
        assert!(!health_only.permits_workflow_provider_snapshot());
        assert!(health_only.raw_provider_snapshot().is_none());
        for caller in OriginalWorkflowHealthCaller::ALL {
            for entry in OriginalWorkflowSnapshotEntry::ALL {
                let candidate = WorkflowHealth { caller, entry };
                assert_eq!(
                    health_only.permits_provider_snapshot(candidate),
                    health.contains(&candidate)
                );
                assert!(!proof.permits_provider_snapshot(candidate));
            }
        }
    }

    #[test]
    fn workflow_adapter_provider_snapshot_proof_requires_its_exact_caller_and_entry() {
        use OriginalProviderSnapshotRole::{Adapter, Workflow};
        use OriginalWorkflowSnapshotCaller::{MessagesAdapter, TextAdapter};
        use OriginalWorkflowSnapshotEntry::{StepExecute, StepRetry};

        let digest = Digest::hash(b"workflow-adapter-provider-snapshot-activation");
        let identity = ExecutionProfileIdentity::new(
            "defaults",
            digest.as_str(),
            "activation:workflow-adapter-provider-snapshot",
            Digest::hash(b"plan").as_str(),
        )
        .unwrap();
        let saved = Adapter(OriginalSavedLocalChatRole::Generate);
        let saved_only = VerifiedRegistryLaunch::test_provider_snapshot_proof(
            identity.clone(),
            "catalog",
            saved,
        );
        assert!(saved_only.permits_provider_snapshot(saved));
        assert!(!saved_only.permits_workflow_adapter_provider_snapshot());

        for (caller, entry) in [(TextAdapter, StepExecute), (MessagesAdapter, StepRetry)] {
            let granted = Workflow { caller, entry };
            let proof = VerifiedRegistryLaunch::test_provider_snapshot_proof(
                identity.clone(),
                "catalog",
                granted,
            );
            assert!(proof.permits_workflow_adapter_provider_snapshot());
            assert!(!proof.permits_workflow_provider_snapshot());
            assert!(!proof.permits_provider_snapshot(saved));
            for caller in OriginalWorkflowSnapshotCaller::ALL {
                for entry in OriginalWorkflowSnapshotEntry::ALL {
                    let candidate = Workflow { caller, entry };
                    assert_eq!(
                        proof.permits_provider_snapshot(candidate),
                        candidate == granted
                    );
                    assert!(!saved_only.permits_provider_snapshot(candidate));
                }
            }
        }
    }

    #[test]
    fn conversation_complete_declaration_pins_both_effect_free_phases() {
        use std::{fs, path::PathBuf};
        use tobkiri_core::pack::{generate_manifest, verify_directory};

        struct Root(PathBuf);
        impl Drop for Root {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = Root(std::env::temp_dir().join(format!(
            "tobkiri-conversation-complete-declaration-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        )));
        fs::create_dir_all(root.0.join("wit/deps")).unwrap();
        let path = root.0.canonicalize().unwrap();
        for (relative, bytes) in CONVERSATION_COMPLETE_WIT {
            fs::write(path.join(relative), bytes).unwrap();
        }
        fs::write(path.join("component.wasm"), b"fixture").unwrap();
        let draft =
            include_bytes!("../../../packs/local.conversation-complete-rust/pack.draft.json");
        let verify = |draft: &[u8]| {
            let manifest = generate_manifest(&path, draft).unwrap();
            fs::write(path.join("pack.json"), manifest).unwrap();
            verify_directory(&path, &Default::default()).unwrap()
        };
        require_conversation_complete_declaration(&verify(draft)).unwrap();

        let mut modified: serde_json::Value = serde_json::from_slice(draft).unwrap();
        modified["permissions"]["storage"] = serde_json::json!({"private": true});
        assert!(require_conversation_complete_declaration(&verify(
            &serde_json::to_vec(&modified).unwrap()
        ))
        .is_err());
        modified = serde_json::from_slice(draft).unwrap();
        modified["version"] = serde_json::json!("0.1.1");
        assert!(require_conversation_complete_declaration(&verify(
            &serde_json::to_vec(&modified).unwrap()
        ))
        .is_err());
        let (wit_path, bytes) = CONVERSATION_COMPLETE_WIT[0];
        fs::write(
            path.join(wit_path),
            std::str::from_utf8(bytes)
                .unwrap()
                .replace("finish: func(input: string)", "finish: func(input: u32)"),
        )
        .unwrap();
        assert!(require_conversation_complete_declaration(&verify(draft)).is_err());
    }

    #[test]
    fn application_presentation_launch_advertises_only_after_all_three_roles_are_proved() {
        let digest = Digest::hash(b"test");
        let identity = ExecutionProfileIdentity::new(
            "defaults",
            digest.as_str(),
            "activation:application-presentation-test",
            Digest::hash(b"plan").as_str(),
        )
        .unwrap();
        let role = ApplicationPresentationRole::Commands;
        let proof = VerifiedRegistryLaunch::test_application_presentation_proof(
            identity.clone(),
            digest.as_str(),
            role,
        );
        for candidate in ApplicationPresentationRole::ALL {
            assert_eq!(
                proof.permits_application_presentation(candidate),
                candidate == role
            );
        }
        assert_eq!(proof.application_presentation_manifest_sha256(), None);
        let proof = VerifiedRegistryLaunch::test_complete_application_presentation_proof(
            identity,
            digest.as_str(),
        );
        assert_eq!(
            proof.application_presentation_manifest_sha256(),
            Some("sha256:manifest"),
        );
    }

    #[test]
    fn application_presentation_declaration_requires_the_sealed_read_wit() {
        use std::{fs, path::PathBuf};
        use tobkiri_core::pack::{generate_manifest, verify_directory};

        struct Root(PathBuf);
        impl Drop for Root {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = Root(std::env::temp_dir().join(format!(
            "tobkiri-application-presentation-declaration-{}-{}",
            std::process::id(),
            rand::random::<u64>(),
        )));
        fs::create_dir_all(root.0.join("wit/deps")).unwrap();
        let path = root.0.canonicalize().unwrap();
        for (relative, wit) in APPLICATION_PRESENTATION_WIT {
            fs::write(path.join(relative), wit).unwrap();
        }
        fs::write(path.join("component.wasm"), b"fixture").unwrap();
        let draft =
            include_bytes!("../../../packs/local.application-presentation-rust/pack.draft.json");
        let verify = || {
            let manifest = generate_manifest(&path, draft).unwrap();
            fs::write(path.join("pack.json"), manifest).unwrap();
            verify_directory(&path, &Default::default()).unwrap()
        };
        require_application_presentation_declaration(&verify()).unwrap();
        let wit = std::str::from_utf8(APPLICATION_PRESENTATION_WIT[0].1).unwrap();
        fs::write(
            path.join("wit/pack.wit"),
            wit.replace("input: string", "input: u32"),
        )
        .unwrap();
        assert!(require_application_presentation_declaration(&verify()).is_err());
    }

    #[test]
    fn ai_routing_declaration_pins_the_stateless_ports_by_version() {
        use std::{fs, path::PathBuf};
        use tobkiri_core::pack::{generate_manifest, verify_directory};

        struct Root(PathBuf);
        impl Drop for Root {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = Root(std::env::temp_dir().join(format!(
            "tobkiri-ai-routing-declaration-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        )));
        fs::create_dir_all(root.0.join("wit/deps")).unwrap();
        let path = root.0.canonicalize().unwrap();
        for (relative, wit) in AI_ROUTING_SHARED_WIT_FILES.into_iter().chain([
            AI_ROUTING_WIT,
            STREAM_RESULT_SIDECAR,
            USAGE_COST_SIDECAR,
            TOOL_INTENTS_SIDECAR,
            REQUEST_PREPARE_SIDECAR,
            FAILOVER_SIDECAR,
            GATEWAY_RESULT_SIDECAR,
            GATEWAY_POLICY_SIDECAR,
        ]) {
            fs::write(path.join(relative), wit).unwrap();
        }
        fs::write(path.join("component.wasm"), b"fixture").unwrap();
        let draft = include_bytes!("../../../packs/local.ai-routing-rust/pack.draft.json");
        let verify = |draft: &[u8]| {
            let manifest = generate_manifest(&path, draft).unwrap();
            fs::write(path.join("pack.json"), manifest).unwrap();
            verify_directory(&path, &Default::default()).unwrap()
        };
        let declaration = require_ai_routing_declaration(&verify(draft)).unwrap();
        assert_eq!(declaration, AiRoutingDeclaration::RoutingWithGatewayPolicy);
        assert!(declaration.has_stream_result());
        assert!(declaration.has_usage_cost());
        assert!(declaration.has_tool_intents());
        assert!(declaration.has_request_prepare());
        assert!(declaration.has_failover());
        assert!(declaration.has_gateway_result());
        assert!(declaration.has_gateway_policy());

        let mut modified: serde_json::Value = serde_json::from_slice(draft).unwrap();
        modified["permissions"]["storage"] = serde_json::json!({"private": true});
        assert!(
            require_ai_routing_declaration(&verify(&serde_json::to_vec(&modified).unwrap()))
                .unwrap_err()
                .to_string()
                .contains("unexpected capabilities")
        );
        modified = serde_json::from_slice(draft).unwrap();
        modified["version"] = serde_json::json!("0.1.1");
        assert!(
            require_ai_routing_declaration(&verify(&serde_json::to_vec(&modified).unwrap()))
                .is_err()
        );

        for (relative, sidecar) in [
            USAGE_COST_SIDECAR,
            TOOL_INTENTS_SIDECAR,
            REQUEST_PREPARE_SIDECAR,
            FAILOVER_SIDECAR,
            GATEWAY_RESULT_SIDECAR,
            GATEWAY_POLICY_SIDECAR,
        ] {
            let sidecar = std::str::from_utf8(sidecar).unwrap();
            fs::write(path.join(relative), format!("{sidecar}\n")).unwrap();
            assert!(require_ai_routing_declaration(&verify(draft))
                .unwrap_err()
                .to_string()
                .contains("unexpected WIT"));
            fs::write(path.join(relative), sidecar).unwrap();
        }

        let wit = std::str::from_utf8(AI_ROUTING_WIT.1).unwrap();
        fs::write(
            path.join("wit/pack.wit"),
            format!("{wit}\n// changed WIT\n"),
        )
        .unwrap();
        assert!(require_ai_routing_declaration(&verify(draft))
            .unwrap_err()
            .to_string()
            .contains("unexpected WIT"));

        let mut legacy: serde_json::Value = serde_json::from_slice(draft).unwrap();
        let mut legacy_wit = wit.to_owned();
        let mut remove_port =
            |legacy: &mut serde_json::Value, removed_interface, removed_sidecar| {
                let interface = format!("interface {removed_interface}");
                let (_, trailing) = legacy_wit.split_once(&interface).unwrap();
                let (body, _) = trailing.split_once("\n}\n\n").unwrap();
                legacy_wit = legacy_wit
                    .replace(&format!("{interface}{body}\n}}\n\n"), "")
                    .replace(&format!("  export {removed_interface};\n"), "");
                let removed_shape = format!("local.ai-routing.{removed_interface}");
                legacy["shapes"]
                    .as_array_mut()
                    .unwrap()
                    .retain(|shape| shape["name"] != removed_shape.as_str());
                legacy["ports"]["provides"]
                    .as_array_mut()
                    .unwrap()
                    .retain(|port| port["shape"] != removed_shape.as_str());
                legacy["files"]
                    .as_array_mut()
                    .unwrap()
                    .retain(|file| file != removed_sidecar);
                fs::write(path.join(AI_ROUTING_WIT.0), &legacy_wit).unwrap();
                fs::remove_file(path.join(removed_sidecar)).unwrap();
            };
        remove_port(&mut legacy, "gateway-policy", GATEWAY_POLICY_SIDECAR.0);
        assert!(
            require_ai_routing_declaration(&verify(&serde_json::to_vec(&legacy).unwrap())).is_err()
        );
        legacy["version"] = serde_json::json!(GATEWAY_RESULT_PACK_VERSION);
        let result_draft = serde_json::to_vec(&legacy).unwrap();
        let result = require_ai_routing_declaration(&verify(&result_draft)).unwrap();
        assert_eq!(result, AiRoutingDeclaration::RoutingWithGatewayResult);
        assert!(result.has_gateway_result());
        assert!(!result.has_gateway_policy());

        remove_port(&mut legacy, "gateway-result", GATEWAY_RESULT_SIDECAR.0);
        assert!(
            require_ai_routing_declaration(&verify(&serde_json::to_vec(&legacy).unwrap())).is_err()
        );
        legacy["version"] = serde_json::json!(PIPELINE_PACK_VERSION);
        let pipeline =
            require_ai_routing_declaration(&verify(&serde_json::to_vec(&legacy).unwrap())).unwrap();
        assert_eq!(pipeline, AiRoutingDeclaration::RoutingWithPipeline);
        assert!(pipeline.has_request_prepare());
        assert!(pipeline.has_failover());
        assert!(!pipeline.has_gateway_result());
        assert!(!pipeline.has_gateway_policy());
        // A .5 declaration still needs both ports even after the bytes are re-hashed.
        remove_port(&mut legacy, "failover", FAILOVER_SIDECAR.0);
        let missing_failover = serde_json::to_vec(&legacy).unwrap();
        assert!(require_ai_routing_declaration(&verify(&missing_failover)).is_err());

        remove_port(&mut legacy, "request-prepare", REQUEST_PREPARE_SIDECAR.0);
        for (version, declaration, removed_port) in [
            (
                TOOL_INTENTS_PACK_VERSION,
                AiRoutingDeclaration::RoutingWithToolIntents,
                None,
            ),
            (
                USAGE_COST_PACK_VERSION,
                AiRoutingDeclaration::RoutingWithUsageCost,
                Some(("tool-intents", TOOL_INTENTS_SIDECAR.0)),
            ),
            (
                STREAM_RESULT_PACK_VERSION,
                AiRoutingDeclaration::RoutingWithStreamResult,
                Some(("usage-cost", USAGE_COST_SIDECAR.0)),
            ),
            (
                "0.1.0",
                AiRoutingDeclaration::Routing,
                Some(("stream-result", STREAM_RESULT_SIDECAR.0)),
            ),
        ] {
            legacy["version"] = serde_json::json!(version);
            if let Some((interface, sidecar)) = removed_port {
                remove_port(&mut legacy, interface, sidecar);
            }
            let legacy_draft = serde_json::to_vec(&legacy).unwrap();
            let verified = require_ai_routing_declaration(&verify(&legacy_draft)).unwrap();
            assert_eq!(verified, declaration);
            assert!(!verified.has_request_prepare());
            assert!(!verified.has_failover());
            assert!(!verified.has_gateway_result());
            assert!(!verified.has_gateway_policy());
            assert_eq!(
                verified.has_tool_intents(),
                version == TOOL_INTENTS_PACK_VERSION
            );
            assert_eq!(
                verified.has_usage_cost(),
                matches!(version, USAGE_COST_PACK_VERSION | TOOL_INTENTS_PACK_VERSION)
            );
            assert_eq!(verified.has_stream_result(), version != "0.1.0");
            let sidecar_path = path.join(AI_ROUTING_SHARED_WIT_FILES[1].0);
            let sidecar = std::str::from_utf8(AI_ROUTING_SHARED_WIT_FILES[1].1).unwrap();
            fs::write(&sidecar_path, format!("{sidecar}\n")).unwrap();
            assert!(require_ai_routing_declaration(&verify(&legacy_draft)).is_err());
            fs::write(sidecar_path, sidecar).unwrap();
        }
    }

    #[test]
    fn pipeline_evidence_is_independent_for_each_gateway_role_and_operation() {
        use OriginalModelResolveRole::{Generate, Stream};

        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let catalog = "catalog:fixture";
        let failover = VerifiedRegistryLaunch::test_failover_proof(identity.clone(), catalog);
        assert!(failover.permits_failover());
        assert!(!failover.permits_stream_result());
        assert!(
            !VerifiedRegistryLaunch::test_stream_result_proof(identity.clone(), catalog)
                .permits_failover()
        );
        for granted in OriginalModelResolveRole::ALL {
            let prepare = VerifiedRegistryLaunch::test_request_prepare_proof(
                identity.clone(),
                catalog,
                granted,
            );
            let routing =
                VerifiedRegistryLaunch::test_ai_routing_proof(identity.clone(), catalog, granted);
            assert!(!prepare.permits_failover());
            assert!(!routing.permits_failover());
            for role in OriginalModelResolveRole::ALL {
                assert_eq!(prepare.permits_request_prepare(role), granted == role);
                assert!(!failover.permits_request_prepare(role));
                assert!(!routing.permits_request_prepare(role));
                assert!(!prepare.permits_ai_routing(role));
            }
            for other in [Generate, Stream] {
                assert!(!prepare.permits_usage_cost(other));
                assert!(!prepare.permits_tool_intents(other));
                assert!(!failover.permits_usage_cost(other));
                assert!(!failover.permits_tool_intents(other));
            }
        }
    }

    #[test]
    fn gateway_result_evidence_requires_its_own_exact_gateway_role() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let catalog = "catalog:fixture";
        let without_result = VerifiedRegistryLaunch::test_pipeline_proof(identity.clone(), catalog);
        for granted in OriginalModelResolveRole::ALL {
            let result = VerifiedRegistryLaunch::test_gateway_result_proof(
                identity.clone(),
                catalog,
                granted,
            );
            for role in OriginalModelResolveRole::ALL {
                assert_eq!(
                    result.permits_gateway_result(role),
                    AI_GATEWAY_ROLES.contains(&granted) && role == granted
                );
                assert!(!without_result.permits_gateway_result(role));
                assert!(!result.permits_ai_routing(role));
                assert!(!result.permits_usage_cost(role));
                assert!(!result.permits_tool_intents(role));
                assert!(!result.permits_request_prepare(role));
            }
            assert!(!result.permits_stream_result());
            assert!(!result.permits_failover());
        }
    }

    #[test]
    fn gateway_policy_evidence_is_independent_of_the_result_and_gateway_child_ports() {
        use OriginalModelResolveRole::{QuoteGenerate, QuoteStream};

        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let catalog = "catalog:fixture";
        for granted in OriginalModelResolveRole::ALL {
            let policy = VerifiedRegistryLaunch::test_gateway_policy_proof(
                identity.clone(),
                catalog,
                granted,
            );
            let result = VerifiedRegistryLaunch::test_gateway_result_proof(
                identity.clone(),
                catalog,
                granted,
            );
            for role in OriginalModelResolveRole::ALL {
                let quote_roles = [QuoteGenerate, QuoteStream];
                let same_edge = granted == role
                    || (quote_roles.contains(&granted) && quote_roles.contains(&role));
                assert_eq!(policy.permits_gateway_policy(role), same_edge);
                assert!(!result.permits_gateway_policy(role));
                assert!(!policy.permits_gateway_result(role));
                assert!(!policy.permits_ai_routing(role));
                assert!(!policy.permits_model_resolve(role));
                assert!(!policy.permits_usage_cost(role));
                assert!(!policy.permits_tool_intents(role));
                assert!(!policy.permits_request_prepare(role));
            }
            assert!(!policy.permits_stream_result());
            assert!(!policy.permits_failover());
        }
    }

    #[test]
    fn ai_routing_evidence_requires_its_own_exact_gateway_role() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        for granted in OriginalModelResolveRole::ALL {
            let model = VerifiedRegistryLaunch::test_model_resolve_proof(
                identity.clone(),
                "catalog:fixture",
                granted,
            );
            let routing = VerifiedRegistryLaunch::test_ai_routing_proof(
                identity.clone(),
                "catalog:fixture",
                granted,
            );
            for role in OriginalModelResolveRole::ALL {
                assert!(!model.permits_ai_routing(role));
                assert!(!routing.permits_model_resolve(role));
                assert_eq!(routing.permits_ai_routing(role), role == granted);
            }
            assert!(!routing.permits_stream_result());
        }

        let stream_result =
            VerifiedRegistryLaunch::test_stream_result_proof(identity, "catalog:fixture");
        assert!(stream_result.permits_stream_result());
        for role in OriginalModelResolveRole::ALL {
            assert!(!stream_result.permits_ai_routing(role));
        }
    }

    #[test]
    fn usage_cost_evidence_is_independent_of_other_gateway_roles_and_operations() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let catalog = "catalog:fixture";
        let normalizer =
            VerifiedRegistryLaunch::test_stream_result_proof(identity.clone(), catalog);
        for granted in [
            OriginalModelResolveRole::Generate,
            OriginalModelResolveRole::Stream,
        ] {
            let usage =
                VerifiedRegistryLaunch::test_usage_cost_proof(identity.clone(), catalog, granted);
            let router =
                VerifiedRegistryLaunch::test_ai_routing_proof(identity.clone(), catalog, granted);
            assert!(!usage.permits_stream_result());
            for role in OriginalModelResolveRole::ALL {
                assert_eq!(usage.permits_usage_cost(role), role == granted);
                assert!(!usage.permits_ai_routing(role));
                assert!(!router.permits_usage_cost(role));
                assert!(!normalizer.permits_usage_cost(role));
            }
        }
    }

    #[test]
    fn tool_intents_evidence_is_independent_of_other_gateway_roles_and_operations() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let catalog = "catalog:fixture";
        let stream_result =
            VerifiedRegistryLaunch::test_stream_result_proof(identity.clone(), catalog);
        for granted in AI_GATEWAY_ROLES {
            let intents =
                VerifiedRegistryLaunch::test_tool_intents_proof(identity.clone(), catalog, granted);
            let usage =
                VerifiedRegistryLaunch::test_usage_cost_proof(identity.clone(), catalog, granted);
            let router =
                VerifiedRegistryLaunch::test_ai_routing_proof(identity.clone(), catalog, granted);
            assert!(!intents.permits_stream_result());
            for role in OriginalModelResolveRole::ALL {
                assert_eq!(intents.permits_tool_intents(role), role == granted);
                assert!(!intents.permits_usage_cost(role));
                assert!(!intents.permits_ai_routing(role));
                assert!(!usage.permits_tool_intents(role));
                assert!(!router.permits_tool_intents(role));
                assert!(!stream_result.permits_tool_intents(role));
            }
        }
    }

    #[test]
    fn saved_orchestration_declaration_pins_the_effect_free_step() {
        use std::{fs, path::PathBuf};
        use tobkiri_core::pack::{generate_manifest, verify_directory};

        struct Root(PathBuf);
        impl Drop for Root {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = Root(std::env::temp_dir().join(format!(
            "tobkiri-orchestration-declaration-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        )));
        fs::create_dir_all(root.0.join("wit/deps")).unwrap();
        let path = root.0.canonicalize().unwrap();
        for (relative, wit) in ORCHESTRATION_WIT_FILES {
            fs::write(path.join(relative), wit).unwrap();
        }
        fs::write(path.join("component.wasm"), b"fixture").unwrap();
        let draft = include_bytes!("../../../packs/local.saved-orchestration-rust/pack.draft.json");
        let verify = |draft: &[u8]| {
            let manifest = generate_manifest(&path, draft).unwrap();
            fs::write(path.join("pack.json"), manifest).unwrap();
            verify_directory(&path, &Default::default()).unwrap()
        };
        require_saved_orchestration_declaration(&verify(draft)).unwrap();

        let mut modified: serde_json::Value = serde_json::from_slice(draft).unwrap();
        modified["permissions"]["storage"] = serde_json::json!({"private": true});
        assert!(require_saved_orchestration_declaration(&verify(
            &serde_json::to_vec(&modified).unwrap()
        ))
        .unwrap_err()
        .to_string()
        .contains("unexpected capabilities"));
        modified = serde_json::from_slice(draft).unwrap();
        modified["version"] = serde_json::json!("0.1.1");
        assert!(require_saved_orchestration_declaration(&verify(
            &serde_json::to_vec(&modified).unwrap()
        ))
        .is_err());

        let source = std::str::from_utf8(ORCHESTRATION_WIT_FILES[0].1).unwrap();
        fs::write(
            path.join("wit/pack.wit"),
            source.replace("input: string", "input: u32"),
        )
        .unwrap();
        assert!(require_saved_orchestration_declaration(&verify(draft))
            .unwrap_err()
            .to_string()
            .contains("unexpected WIT"));
    }

    #[test]
    fn model_resolve_evidence_requires_the_exact_gateway_role() {
        use OriginalModelResolveRole::{Generate, QuoteGenerate, QuoteStream, Stream};
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let catalog_revision = "catalog:fixture";
        let roles = [QuoteGenerate, QuoteStream, Generate, Stream];
        for proof in [
            VerifiedRegistryLaunch::test_proof(identity.clone(), catalog_revision),
            VerifiedRegistryLaunch::test_model_proof(identity.clone(), catalog_revision),
            VerifiedRegistryLaunch::test_model_save_proof(identity.clone(), catalog_revision),
        ] {
            for role in roles {
                assert!(!proof.permits_model_resolve(role));
            }
        }
        for granted in roles {
            let proof = VerifiedRegistryLaunch::test_model_resolve_proof(
                identity.clone(),
                catalog_revision,
                granted,
            );
            for role in roles {
                assert_eq!(proof.permits_model_resolve(role), role == granted);
            }
            assert!(!NativeRegistryRequest::ModelList.has_launch_proof(&proof));
            assert!(!NativeRegistryRequest::ModelSave {
                request: Vec::new(),
                provider: Vec::new(),
            }
            .has_launch_proof(&proof));
        }
    }

    #[test]
    fn model_save_evidence_keeps_the_other_registry_operations_separate() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let save = NativeRegistryRequest::ModelSave {
            request: b"{}".to_vec(),
            provider: b"{}".to_vec(),
        };
        assert!(!save.has_launch_proof(&VerifiedRegistryLaunch::test_proof(
            identity.clone(),
            "catalog:fixture"
        )));
        assert!(
            !save.has_launch_proof(&VerifiedRegistryLaunch::test_model_proof(
                identity.clone(),
                "catalog:fixture"
            ))
        );
        let proof = VerifiedRegistryLaunch::test_model_save_proof(identity, "catalog:fixture");
        assert!(save.has_launch_proof(&proof));
        assert!(!NativeRegistryRequest::ModelList.has_launch_proof(&proof));
        assert!(!NativeRegistryRequest::Provider {
            request: Vec::new(),
            apply: None,
        }
        .has_launch_proof(&proof));
    }

    #[test]
    fn turn_settlement_declaration_pins_wit_and_rejects_extra_permissions() {
        use std::{fs, path::PathBuf};
        use tobkiri_core::pack::{generate_manifest, verify_directory};

        struct Root(PathBuf);
        impl Drop for Root {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = Root(std::env::temp_dir().join(format!(
            "tobkiri-settlement-declaration-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        )));
        fs::create_dir_all(root.0.join("wit/deps")).unwrap();
        let path = root.0.canonicalize().unwrap();
        for (relative, wit) in TURN_SETTLEMENT_WIT_FILES.into_iter().chain([
            TURN_WIT,
            TURN_READ_SHAPE,
            TURN_START_SHAPE,
            TURN_INPUT_SHAPE,
        ]) {
            fs::write(path.join(relative), wit).unwrap();
        }
        fs::write(path.join("component.wasm"), b"fixture").unwrap();
        let draft = include_bytes!("../../../packs/local.saved-turn-runtime-rust/pack.draft.json");
        let verify = |draft: &[u8]| {
            let manifest = generate_manifest(&path, draft).unwrap();
            fs::write(path.join("pack.json"), manifest).unwrap();
            verify_directory(&path, &Default::default()).unwrap()
        };
        let current = verify(draft);
        assert!(require_turn_settlement_declaration(&current).unwrap());
        assert!(require_turn_read_declaration(&current).is_ok());
        assert!(require_turn_start_declaration(&current).is_ok());
        assert!(require_turn_input_declaration(&current).is_ok());

        let mut extra: serde_json::Value = serde_json::from_slice(draft).unwrap();
        extra["permissions"]["storage"] = serde_json::json!({"private": true});
        assert!(
            require_turn_settlement_declaration(&verify(&serde_json::to_vec(&extra).unwrap()))
                .unwrap_err()
                .to_string()
                .contains("unexpected capabilities")
        );
        let current_wit = std::str::from_utf8(TURN_WIT.1).unwrap();
        fs::write(
            path.join(TURN_WIT.0),
            current_wit.replace("    valid,", "    accepted,"),
        )
        .unwrap();
        assert!(require_turn_read_declaration(&verify(draft))
            .unwrap_err()
            .to_string()
            .contains("unexpected WIT"));
        assert!(require_turn_start_declaration(&verify(draft)).is_err());
        assert!(require_turn_input_declaration(&verify(draft)).is_err());
        fs::write(
            path.join("wit/pack.wit"),
            current_wit.replace("option<u64>", "option<u32>"),
        )
        .unwrap();
        assert!(require_turn_settlement_declaration(&verify(draft))
            .unwrap_err()
            .to_string()
            .contains("unexpected WIT"));

        let (start, after) = current_wit.split_once("interface legacy-input {").unwrap();
        let (_, world) = after.split_once("world pack {").unwrap();
        let start_wit =
            format!("{start}world pack {{{world}").replace("  export legacy-input;\n", "");
        assert_eq!(
            Digest::hash(start_wit.as_bytes()).as_str(),
            START_TURN_WIT_DIGEST
        );
        fs::write(path.join(TURN_WIT.0), &start_wit).unwrap();
        fs::remove_file(path.join(TURN_INPUT_SHAPE.0)).unwrap();
        let mut old: serde_json::Value = serde_json::from_slice(draft).unwrap();
        old["version"] = serde_json::json!("0.3.0");
        old["shapes"].as_array_mut().unwrap().pop();
        old["ports"]["provides"].as_array_mut().unwrap().pop();
        old["files"]
            .as_array_mut()
            .unwrap()
            .retain(|file| file != TURN_INPUT_SHAPE.0);
        let start = verify(&serde_json::to_vec(&old).unwrap());
        assert!(require_turn_start_declaration(&start).is_ok());
        assert!(require_turn_input_declaration(&start).is_err());

        let (previous, after) = start_wit.split_once("interface legacy-start {").unwrap();
        let (_, world) = after.split_once("world pack {").unwrap();
        let previous_wit =
            format!("{previous}world pack {{{world}").replace("  export legacy-start;\n", "");
        assert_eq!(
            Digest::hash(previous_wit.as_bytes()).as_str(),
            READ_TURN_WIT_DIGEST
        );
        fs::write(path.join(TURN_WIT.0), &previous_wit).unwrap();
        fs::remove_file(path.join(TURN_START_SHAPE.0)).unwrap();
        old["version"] = serde_json::json!("0.2.0");
        old["shapes"].as_array_mut().unwrap().pop();
        old["ports"]["provides"].as_array_mut().unwrap().pop();
        old["files"]
            .as_array_mut()
            .unwrap()
            .retain(|file| file != TURN_START_SHAPE.0);
        let previous = verify(&serde_json::to_vec(&old).unwrap());
        assert!(require_turn_read_declaration(&previous).is_ok());
        assert!(require_turn_start_declaration(&previous).is_err());

        let (settlement, after) = previous_wit.split_once("interface legacy-read {").unwrap();
        let (_, world) = after.split_once("world pack {").unwrap();
        let old_wit =
            format!("{settlement}world pack {{{world}").replace("  export legacy-read;\n", "");
        assert_eq!(
            Digest::hash(old_wit.as_bytes()).as_str(),
            ORIGINAL_TURN_WIT_DIGEST
        );
        fs::write(path.join(TURN_WIT.0), old_wit).unwrap();
        fs::remove_file(path.join(TURN_READ_SHAPE.0)).unwrap();
        old["version"] = serde_json::json!("0.1.0");
        old["shapes"].as_array_mut().unwrap().pop();
        old["ports"]["provides"].as_array_mut().unwrap().pop();
        old["files"]
            .as_array_mut()
            .unwrap()
            .retain(|file| file != TURN_READ_SHAPE.0);
        let old = serde_json::to_vec(&old).unwrap();
        let original = verify(&old);
        assert!(!require_turn_settlement_declaration(&original).unwrap());
        assert!(require_turn_read_declaration(&original).is_err());
        assert!(require_turn_start_declaration(&original).is_err());
    }

    #[test]
    fn conversation_optional_declarations_preserve_earlier_ports() {
        use std::{fs, path::PathBuf};
        use tobkiri_core::pack::{generate_manifest, verify_directory};

        struct Root(PathBuf);
        impl Drop for Root {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = Root(std::env::temp_dir().join(format!(
            "tobkiri-conversation-declaration-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        )));
        fs::create_dir_all(root.0.join("wit/deps")).unwrap();
        let path = root.0.canonicalize().unwrap();
        macro_rules! copy_wit {
            ($($filename:literal),+) => {$(
                fs::write(
                    path.join(concat!("wit/", $filename)),
                    include_bytes!(concat!("../../../packs/local.saved-conversation-rust/wit/", $filename)),
                )
                .unwrap();
            )+};
        }
        copy_wit!(
            "pack.wit",
            "data.shape.json",
            "legacy-append.shape.json",
            "legacy-confirmation.shape.json",
            "legacy-linked-context.shape.json",
            "legacy-list.shape.json",
            "legacy-read.shape.json",
            "legacy-receipt.shape.json",
            "model.shape.json"
        );
        fs::write(
            path.join("wit/deps/core.wit"),
            include_bytes!("../../../crates/tobkiri-wit/wit/core-v1-2/core.wit"),
        )
        .unwrap();
        fs::write(path.join("component.wasm"), b"fixture").unwrap();
        let draft: serde_json::Value = serde_json::from_slice(include_bytes!(
            "../../../packs/local.saved-conversation-rust/pack.draft.json"
        ))
        .unwrap();
        let verify = |draft: &serde_json::Value| {
            let manifest = generate_manifest(&path, &serde_json::to_vec(draft).unwrap()).unwrap();
            fs::write(path.join("pack.json"), manifest).unwrap();
            verify_directory(&path, &Default::default()).unwrap()
        };
        let current = verify(&draft);
        assert!(observe_conversation_append_declaration(&current).unwrap());
        assert!(observe_conversation_confirmation_declaration(&current).unwrap());
        assert!(observe_linked_conversation_context_declaration(&current).unwrap());
        assert!(observe_conversation_list_declaration(&current).unwrap());

        let append = "local.saved-conversation.legacy-append";
        let confirmation = "local.saved-conversation.legacy-confirmation";
        let linked_context = "local.saved-conversation.legacy-linked-context";
        let list = "local.saved-conversation.legacy-list";
        let undeclare = |draft: &mut serde_json::Value, shape: &str| {
            draft["shapes"]
                .as_array_mut()
                .unwrap()
                .retain(|row| row["name"] != shape);
            draft["ports"]["provides"]
                .as_array_mut()
                .unwrap()
                .retain(|row| row["shape"] != shape);
        };

        let mut previous = draft.clone();
        undeclare(&mut previous, list);
        let previous_pack = verify(&previous);
        assert!(!observe_conversation_list_declaration(&previous_pack).unwrap());
        verify_legacy_conversation_read_declaration(&previous_pack).unwrap();

        undeclare(&mut previous, linked_context);
        let previous_pack = verify(&previous);
        assert!(!observe_linked_conversation_context_declaration(&previous_pack).unwrap());
        assert!(verify_legacy_conversation_linked_context_declaration(&previous_pack).is_err());
        verify_legacy_conversation_read_declaration(&previous_pack).unwrap();
        verify_legacy_conversation_receipt_declaration(&previous_pack).unwrap();

        undeclare(&mut previous, append);
        let previous_pack = verify(&previous);
        assert!(!observe_conversation_append_declaration(&previous_pack).unwrap());
        assert!(observe_conversation_confirmation_declaration(&previous_pack).unwrap());
        verify_legacy_conversation_read_declaration(&previous_pack).unwrap();
        verify_legacy_conversation_receipt_declaration(&previous_pack).unwrap();

        undeclare(&mut previous, confirmation);
        let old = verify(&previous);
        assert!(!observe_conversation_append_declaration(&old).unwrap());
        assert!(!observe_conversation_confirmation_declaration(&old).unwrap());
        verify_legacy_conversation_read_declaration(&old).unwrap();
        verify_legacy_conversation_receipt_declaration(&old).unwrap();

        let ports: [(&str, fn(&VerifiedPack) -> Result<bool>); 4] = [
            (append, observe_conversation_append_declaration),
            (confirmation, observe_conversation_confirmation_declaration),
            (list, observe_conversation_list_declaration),
            (
                linked_context,
                observe_linked_conversation_context_declaration,
            ),
        ];
        for (shape, observe) in ports {
            let mut optional = draft.clone();
            optional["ports"]["provides"]
                .as_array_mut()
                .unwrap()
                .iter_mut()
                .find(|port| port["shape"] == shape)
                .unwrap()["optional"] = serde_json::json!(true);
            assert!(observe(&verify(&optional)).is_err(), "{shape}");
        }

        let mut missing_provide = draft.clone();
        missing_provide["ports"]["provides"]
            .as_array_mut()
            .unwrap()
            .retain(|row| row["shape"] != linked_context);
        assert!(
            observe_linked_conversation_context_declaration(&verify(&missing_provide)).is_err()
        );
    }

    #[test]
    fn optional_model_access_projection_declaration_preserves_legacy_registry_read() {
        use std::{
            fs,
            path::{Path, PathBuf},
        };
        use tobkiri_core::pack::{generate_manifest, verify_directory};

        struct Root(PathBuf);
        impl Drop for Root {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = Root(std::env::temp_dir().join(format!(
            "tobkiri-provider-registry-declaration-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        )));
        fs::create_dir_all(root.0.join("wit/deps")).unwrap();
        let path = root.0.canonicalize().unwrap();
        let source =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../packs/local.provider-registry-rust");
        let draft: serde_json::Value = serde_json::from_slice(include_bytes!(
            "../../../packs/local.provider-registry-rust/pack.draft.json"
        ))
        .unwrap();
        for file in draft["files"].as_array().unwrap() {
            let file = file.as_str().unwrap();
            match file {
                "component.wasm" => fs::write(path.join(file), b"fixture").unwrap(),
                "wit/deps/core.wit" => fs::write(
                    path.join(file),
                    include_bytes!("../../../crates/tobkiri-wit/wit/core-v1-2/core.wit"),
                )
                .unwrap(),
                _ => {
                    fs::copy(source.join(file), path.join(file)).unwrap();
                }
            }
        }
        let verify = |draft: &serde_json::Value| {
            let manifest = generate_manifest(&path, &serde_json::to_vec(draft).unwrap()).unwrap();
            fs::write(path.join("pack.json"), manifest).unwrap();
            verify_directory(&path, &Default::default()).unwrap()
        };
        let current = verify(&draft);
        assert!(observe_model_access_projection_declaration(&current).unwrap());
        assert!(observe_provider_health_declaration(&current).unwrap());
        assert!(observe_provider_snapshot_declaration(&current).unwrap());

        let health = "local.provider-registry.legacy-health";
        let mut malformed_health = draft.clone();
        malformed_health["ports"]["provides"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|port| port["shape"] == health)
            .unwrap()["optional"] = serde_json::json!(true);
        let malformed_health = verify(&malformed_health);
        assert!(observe_provider_health_declaration(&malformed_health).is_err());
        assert!(observe_provider_snapshot_declaration(&malformed_health).unwrap());

        let mut no_snapshot = draft.clone();
        let snapshot = "local.provider-registry.legacy-resource";
        no_snapshot["ports"]["provides"]
            .as_array_mut()
            .unwrap()
            .retain(|port| port["shape"] != snapshot);
        no_snapshot["shapes"]
            .as_array_mut()
            .unwrap()
            .retain(|shape| shape["name"] != snapshot);
        let no_snapshot = verify(&no_snapshot);
        assert!(observe_provider_health_declaration(&no_snapshot).unwrap());
        assert!(!observe_provider_snapshot_declaration(&no_snapshot).unwrap());

        let projection = "local.provider-registry.model-access-projection";
        let mut malformed = draft.clone();
        malformed["ports"]["provides"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|port| port["shape"] == projection)
            .unwrap()["optional"] = serde_json::json!(true);
        assert!(observe_model_access_projection_declaration(&verify(&malformed)).is_err());

        malformed["ports"]["provides"]
            .as_array_mut()
            .unwrap()
            .retain(|port| port["shape"] != projection);
        assert!(observe_model_access_projection_declaration(&verify(&malformed)).is_err());

        let mut previous = malformed;
        previous["ports"]["provides"]
            .as_array_mut()
            .unwrap()
            .retain(|port| port["shape"] != health);
        previous["shapes"]
            .as_array_mut()
            .unwrap()
            .retain(|shape| shape["name"] != projection && shape["name"] != health);
        let previous = verify(&previous);
        assert!(!observe_model_access_projection_declaration(&previous).unwrap());
        assert!(!observe_provider_health_declaration(&previous).unwrap());
        assert!(observe_provider_snapshot_declaration(&previous).unwrap());

        let wit_path = path.join("wit/pack.wit");
        let wit = fs::read_to_string(&wit_path).unwrap();
        let malformed_wit = wit.replace(
            "health: func(document: option<string>)",
            "health: func(document: string)",
        );
        assert_ne!(malformed_wit, wit);
        fs::write(wit_path, malformed_wit).unwrap();
        let wrong_abi = verify(&draft);
        assert!(observe_provider_health_declaration(&wrong_abi).is_err());
        assert!(observe_provider_snapshot_declaration(&wrong_abi).unwrap());
    }

    #[test]
    fn conversation_append_evidence_is_independent_of_other_grants() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let append_callers = [
            ConversationAppendCaller::SavedTurn,
            ConversationAppendCaller::SideChat,
        ];
        for granted in append_callers {
            let proof = VerifiedRegistryLaunch::test_conversation_append_proof_for(
                identity.clone(),
                "catalog:fixture",
                granted,
            );
            for caller in append_callers {
                assert_eq!(
                    proof.permits_conversation_append_for(caller),
                    caller == granted
                );
            }
            for role in OriginalSavedReceiptRole::ALL {
                assert!(!proof.permits_saved_receipt(role));
                assert!(!proof.permits_conversation_confirmation(role));
            }
            for caller in [
                ConversationReadCaller::Shell,
                ConversationReadCaller::SavedTurn,
                ConversationReadCaller::SavedOrchestration,
            ] {
                assert!(!proof.permits_conversation_read(caller));
            }
            for role in SettlementCaller::ALL {
                assert!(!proof.permits_turn_settlement(role));
            }
        }
    }

    #[test]
    fn conversation_confirmation_evidence_keeps_roles_read_and_receipt_independent() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let revision = "catalog:fixture";
        for granted in OriginalSavedReceiptRole::RECEIPT_CALLERS {
            let receipt = VerifiedRegistryLaunch::test_saved_receipt_proof(
                identity.clone(),
                revision,
                granted,
            );
            let proof = VerifiedRegistryLaunch::test_conversation_confirmation_proof(
                identity.clone(),
                revision,
                granted,
            );
            let turn =
                VerifiedRegistryLaunch::test_turn_read_proof(identity.clone(), revision, granted);
            for caller in [
                ConversationAppendCaller::SavedTurn,
                ConversationAppendCaller::SideChat,
            ] {
                assert!(!receipt.permits_conversation_append_for(caller));
                assert!(!proof.permits_conversation_append_for(caller));
            }
            for role in OriginalSavedReceiptRole::RECEIPT_CALLERS {
                assert!(!receipt.permits_conversation_confirmation(role));
                assert_eq!(
                    proof.permits_conversation_confirmation(role),
                    role == granted
                );
                assert!(!proof.permits_saved_receipt(role));
                assert!(!proof.permits_turn_read(role));
                assert!(!turn.permits_conversation_confirmation(role));
                assert!(!turn.permits_saved_receipt(role));
                assert_eq!(turn.permits_turn_read(role), role == granted);
            }
            for caller in [
                ConversationReadCaller::Shell,
                ConversationReadCaller::SavedTurn,
                ConversationReadCaller::SavedOrchestration,
            ] {
                assert!(!proof.permits_conversation_read(caller));
            }
            for role in SettlementCaller::ALL {
                assert!(!proof.permits_turn_settlement(role));
            }
        }
    }

    #[test]
    fn turn_start_evidence_is_specific_to_its_caller_and_operation() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let revision = "catalog:fixture";
        let callers = [
            SavedTurnStartCaller::SavedTurn,
            SavedTurnStartCaller::SideChat,
        ];
        for caller in callers {
            let start = VerifiedRegistryLaunch::test_turn_start_proof_for(
                identity.clone(),
                revision,
                caller,
            );
            for requested in callers {
                assert_eq!(start.permits_turn_start_for(requested), requested == caller);
            }
            assert!(!start.permits_saved_orchestration());
            assert!(!VerifiedRegistryLaunch::test_saved_orchestration_proof(
                identity.clone(),
                revision
            )
            .permits_turn_start_for(caller));
            for role in SettlementCaller::ALL {
                assert!(!start.permits_turn_settlement(role));
                assert!(!VerifiedRegistryLaunch::test_turn_settlement_proof(
                    identity.clone(),
                    revision,
                    role
                )
                .permits_turn_start_for(caller));
            }
            for role in OriginalSavedReceiptRole::RECEIPT_CALLERS {
                assert!(!start.permits_turn_read(role));
                assert!(!VerifiedRegistryLaunch::test_turn_read_proof(
                    identity.clone(),
                    revision,
                    role
                )
                .permits_turn_start_for(caller));
            }
            for append in [
                ConversationAppendCaller::SavedTurn,
                ConversationAppendCaller::SideChat,
            ] {
                assert!(!start.permits_conversation_append_for(append));
                assert!(!VerifiedRegistryLaunch::test_conversation_append_proof_for(
                    identity.clone(),
                    revision,
                    append
                )
                .permits_turn_start_for(caller));
            }
        }
    }

    #[test]
    fn turn_settlement_evidence_requires_the_original_grant_for_each_role() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let revision = "catalog:fixture";
        for granted in OriginalSavedReceiptRole::ALL {
            let receipt = VerifiedRegistryLaunch::test_saved_receipt_proof(
                identity.clone(),
                revision,
                granted,
            );
            for role in SettlementCaller::ALL {
                assert!(!receipt.permits_turn_settlement(role));
            }
        }
        for granted in SettlementCaller::ALL {
            let proof = VerifiedRegistryLaunch::test_turn_settlement_proof(
                identity.clone(),
                revision,
                granted,
            );
            for role in SettlementCaller::ALL {
                assert_eq!(proof.permits_turn_settlement(role), role == granted);
            }
            for role in OriginalSavedReceiptRole::ALL {
                assert!(!proof.permits_saved_receipt(role));
            }
            assert!(!proof.permits_conversation_read(ConversationReadCaller::SavedTurn));
        }
    }

    #[test]
    fn conversation_read_evidence_is_specific_to_the_selected_caller() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let callers = [
            ConversationReadCaller::Shell,
            ConversationReadCaller::SavedTurn,
            ConversationReadCaller::SavedOrchestration,
        ];
        let request = |caller| NativeRegistryRequest::ConversationGet {
            caller,
            conversation_id: "fixture".into(),
        };
        let provider = VerifiedRegistryLaunch::test_proof(identity.clone(), "catalog:fixture");
        let model = VerifiedRegistryLaunch::test_model_proof(identity.clone(), "catalog:fixture");
        let save =
            VerifiedRegistryLaunch::test_model_save_proof(identity.clone(), "catalog:fixture");
        for caller in callers {
            for proof in [&provider, &model, &save] {
                assert!(!request(caller).has_launch_proof(proof));
            }
        }
        let mut proof = save;
        proof.model_save = None;
        for selected_caller in callers {
            let mut source = VerifiedConversationSource {
                shell: None,
                saved_turn: None,
                saved_orchestration: None,
                linked_context_declared: false,
                list_declared: false,
                saved_receipt: [None, None],
                side_chat_saved_receipt: [None, None],
                confirmation: VerifiedConversationConfirmationRoles::default(),
                append: VerifiedConversationAppendRoles::default(),
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            };
            let role = match selected_caller {
                ConversationReadCaller::Shell => &mut source.shell,
                ConversationReadCaller::SavedTurn => &mut source.saved_turn,
                ConversationReadCaller::SavedOrchestration => &mut source.saved_orchestration,
            };
            *role = Some(VerifiedLegacyRoutePin::fixture());
            proof.conversation = Some(source);
            assert!(!proof.permits_linked_conversation_context());
            for caller in callers {
                assert_eq!(
                    request(caller).has_launch_proof(&proof),
                    caller == selected_caller
                );
            }
            for role in OriginalSavedReceiptRole::ALL {
                assert!(!proof.permits_saved_receipt(role));
            }
            assert!(!NativeRegistryRequest::ModelList.has_launch_proof(&proof));
            proof.conversation.as_mut().unwrap().linked_context_declared = true;
            assert_eq!(
                proof.permits_linked_conversation_context(),
                selected_caller == ConversationReadCaller::SavedOrchestration
            );
            for caller in callers {
                assert_eq!(
                    request(caller).has_launch_proof(&proof),
                    caller == selected_caller
                );
            }
        }
        let linked = VerifiedRegistryLaunch::test_linked_conversation_context_proof(
            identity.clone(),
            "catalog:fixture",
        );
        assert!(linked.permits_linked_conversation_context());
        for role in OriginalSavedReceiptRole::ALL {
            assert!(!linked.permits_saved_receipt(role));
        }
        for granted in OriginalSavedReceiptRole::RECEIPT_CALLERS {
            let proof = VerifiedRegistryLaunch::test_saved_receipt_proof(
                identity.clone(),
                "catalog:fixture",
                granted,
            );
            for role in OriginalSavedReceiptRole::RECEIPT_CALLERS {
                assert_eq!(proof.permits_saved_receipt(role), role == granted);
                assert!(!proof.permits_conversation_confirmation(role));
                assert!(!proof.permits_turn_read(role));
            }
            for caller in callers {
                assert!(!proof.permits_conversation_read(caller));
            }
        }
    }
}
