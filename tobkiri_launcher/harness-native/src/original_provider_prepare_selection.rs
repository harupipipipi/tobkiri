//! Select legacy Pack routes from authenticated v5 records.
use crate::OriginalActiveProfile;
use sha2::{Digest as _, Sha256};
use std::{fmt, sync::Arc};
use tobkiri_canon::{
    AllocationBudget, AllocationKind, AllocationRequest, CoreRef, CoreValue, Digest,
};

const PIN_FIELDS: [&str; 10] = [
    "pack_id",
    "variant_id",
    "artifact_digest",
    "executable_catalog_digest",
    "platform",
    "architecture",
    "runtime_abi",
    "backend",
    "execution_kind",
    "domain_kind",
];

const PLATFORM: &str = "any";
const ARCHITECTURE: &str = "any";
const RUNTIME_ABI: &str = "python3.13";
#[derive(Clone, Copy)]
struct Execution {
    backend: &'static str,
    kind: &'static str,
    domain: &'static str,
}

const PYTHON_HOST: Execution = Execution {
    backend: "tobkiri.python-host-v4",
    kind: "host_extension",
    domain: "dedicated_process",
};

const PYTHON_PACK_VM: Execution = Execution {
    backend: "tobkiri.python-pack-v4",
    kind: "pack_vm",
    domain: "pack_vm",
};

struct Route {
    pack: &'static str,
    function: &'static str,
    contract: &'static str,
    operation: &'static str,
    variant: &'static str,
    execution: Execution,
}

impl Route {
    fn execution_fields(&self) -> [(&'static str, &'static str); 6] {
        [
            ("platform", PLATFORM),
            ("architecture", ARCHITECTURE),
            ("runtime_abi", RUNTIME_ABI),
            ("backend", self.execution.backend),
            ("execution_kind", self.execution.kind),
            ("domain_kind", self.execution.domain),
        ]
    }
}

static COORDINATOR: Route = Route {
    pack: "rumi_host_authority_bridge_pack",
    function: "rumi_host_authority_bridge_pack.host-authority.interactive-effect",
    contract: "tobkiri.service.interactive-effect.v1",
    operation: "interactive_effect.manage",
    variant: "rumi_host_authority_bridge_pack.host-authority.interactive-effect.python",
    execution: PYTHON_HOST,
};

static PROVIDER_PREPARE: Route = Route {
    pack: "rumi_provider_registry_pack",
    function: "rumi_provider_registry_pack.provider-configure.prepare",
    contract: "tobkiri.action.ai.provider.registry.manage.v1",
    operation: "rumi_provider_registry_pack.provider-configure-prepare",
    variant: "rumi_provider_registry_pack.provider-configure.prepare.python",
    execution: PYTHON_HOST,
};

static MODEL_LIST: Route = Route {
    pack: "rumi_model_registry_pack",
    function: "rumi_model_registry_pack.model-registry.profile",
    contract: "tobkiri.resource.ai.model.profile.v1",
    operation: "rumi_model_registry_pack.model-profile-resource",
    variant: "rumi_model_registry_pack.model-registry.profile.python",
    execution: PYTHON_HOST,
};

static MODEL_RESOLVE_GENERATE: Route = Route {
    operation: "rumi_model_registry_pack.model-profile-resource.generate",
    ..MODEL_LIST
};

static MODEL_RESOLVE_STREAM: Route = Route {
    operation: "rumi_model_registry_pack.model-profile-resource.stream",
    ..MODEL_LIST
};

static MODEL_MANAGE: Route = Route {
    pack: "rumi_model_registry_pack",
    function: "rumi_model_registry_pack.model-registry.manage",
    contract: "tobkiri.action.ai.model.profile.manage.v1",
    operation: "rumi_model_registry_pack.model-profile-manage",
    variant: "rumi_model_registry_pack.model-registry.manage.python",
    execution: PYTHON_HOST,
};

static PROVIDER_READ: Route = Route {
    pack: "rumi_provider_registry_pack",
    function: "rumi_provider_registry_pack.provider-registry.resource",
    contract: "tobkiri.resource.ai.provider.registry.v1",
    operation: "rumi_provider_registry_pack.provider-registry-resource",
    variant: "rumi_provider_registry_pack.provider-registry.resource.python",
    execution: PYTHON_HOST,
};

static PROVIDER_READ_GENERATE: Route = Route {
    operation: "rumi_provider_registry_pack.provider-registry-resource.generate",
    ..PROVIDER_READ
};

static PROVIDER_READ_STREAM: Route = Route {
    operation: "rumi_provider_registry_pack.provider-registry-resource.stream",
    ..PROVIDER_READ
};

static CONVERSATION_READ: Route = Route {
    pack: "rumi_conversation_store_pack",
    function: "rumi_conversation_store_pack.conversation-store.resource",
    contract: "tobkiri.resource.conversation.v1",
    operation: "rumi_conversation_store_pack.conversation-resource",
    variant: "rumi_conversation_store_pack.conversation-store.resource.python",
    execution: PYTHON_HOST,
};

static CONVERSATION_CONFIRMATION: Route = Route {
    pack: "rumi_conversation_store_pack",
    function: "rumi_conversation_store_pack.conversation-store.completion",
    contract: "tobkiri.action.conversation.completion.v1",
    operation: "rumi_conversation_store_pack.conversation-completion",
    variant: "rumi_conversation_store_pack.conversation-store.completion.python",
    execution: PYTHON_HOST,
};

const SAVED_TURN_CALLER: &str = "rumi_turn_runtime_pack.turn-runtime.saved";
const SAVED_RECONCILE_CALLER: &str = "rumi_turn_runtime_pack.turn-runtime.reconcile";
const TURN_RUNTIME_PACK: &str = "rumi_turn_runtime_pack";
const TURN_SHELL_CALLER: &str = "shell.tauri.default";

static SAVED_TURN: Route = Route {
    pack: TURN_RUNTIME_PACK,
    function: SAVED_TURN_CALLER,
    contract: "tobkiri.action.turn.saved.v1",
    operation: "rumi_turn_runtime_pack.turn-saved",
    variant: "rumi_turn_runtime_pack.turn-runtime.saved.python",
    execution: PYTHON_HOST,
};

static TURN_LIFECYCLE: Route = Route {
    pack: TURN_RUNTIME_PACK,
    function: "rumi_turn_runtime_pack.turn-runtime.lifecycle",
    contract: "tobkiri.action.turn.lifecycle.v1",
    operation: "rumi_turn_runtime_pack.turn-lifecycle",
    variant: "rumi_turn_runtime_pack.turn-runtime.lifecycle.python",
    execution: PYTHON_HOST,
};

static SAVED_RECONCILE: Route = Route {
    pack: TURN_RUNTIME_PACK,
    function: SAVED_RECONCILE_CALLER,
    contract: "tobkiri.action.turn.reconcile.v1",
    operation: "rumi_turn_runtime_pack.turn-reconcile",
    variant: "rumi_turn_runtime_pack.turn-runtime.reconcile.python",
    execution: PYTHON_HOST,
};

static TURN_GET: Route = Route {
    pack: TURN_RUNTIME_PACK,
    function: "rumi_turn_runtime_pack.turn-runtime.resource",
    contract: "tobkiri.resource.turn.v1",
    operation: "rumi_turn_runtime_pack.turn-resource",
    variant: "rumi_turn_runtime_pack.turn-runtime.resource.python",
    execution: PYTHON_HOST,
};

const SAVED_ORCHESTRATION_CALLER: &str = "tobkiri_conversation_orchestration_pack.saved";
static SAVED_ORCHESTRATION: Route = Route {
    pack: "tobkiri_conversation_orchestration_pack",
    function: SAVED_ORCHESTRATION_CALLER,
    contract: "conversation.saved-turn.v1",
    operation: "saved_complete",
    variant: "tobkiri_conversation_orchestration_pack.saved.python",
    execution: PYTHON_PACK_VM,
};

static CONVERSATION_MESSAGE_MANAGE: Route = Route {
    pack: "rumi_conversation_store_pack",
    function: "rumi_conversation_store_pack.conversation-store.message-manage",
    contract: "tobkiri.action.message.manage.v1",
    operation: "rumi_conversation_store_pack.message-manage",
    variant: "rumi_conversation_store_pack.conversation-store.message-manage.python",
    execution: PYTHON_HOST,
};

const GATEWAY_QUOTE_CALLER: &str = "rumi_ai_gateway_pack.ai-gateway.route-quote";
const GATEWAY_GENERATE_CALLER: &str = "rumi_ai_gateway_pack.ai-gateway.generate";
const GATEWAY_STREAM_CALLER: &str = "rumi_ai_gateway_pack.ai-gateway.stream";

static GATEWAY_QUOTE: Route = Route {
    pack: "rumi_ai_gateway_pack",
    function: GATEWAY_QUOTE_CALLER,
    contract: "tobkiri.resource.ai.route.quote.v1",
    operation: GATEWAY_QUOTE_CALLER,
    variant: "rumi_ai_gateway_pack.ai-gateway.route-quote.python",
    execution: PYTHON_HOST,
};

static GATEWAY_GENERATE: Route = Route {
    pack: "rumi_ai_gateway_pack",
    function: GATEWAY_GENERATE_CALLER,
    contract: "tobkiri.service.ai.generate.v1",
    operation: GATEWAY_GENERATE_CALLER,
    variant: "rumi_ai_gateway_pack.ai-gateway.generate.python",
    execution: PYTHON_HOST,
};

static GATEWAY_STREAM: Route = Route {
    pack: "rumi_ai_gateway_pack",
    function: GATEWAY_STREAM_CALLER,
    contract: "tobkiri.service.ai.stream.v1",
    operation: GATEWAY_STREAM_CALLER,
    variant: "rumi_ai_gateway_pack.ai-gateway.stream.python",
    execution: PYTHON_HOST,
};

static AI_ROUTING_GENERATE: Route = Route {
    pack: "rumi_ai_routing_pack",
    function: "rumi_ai_routing_pack.ai-routing.default",
    contract: "tobkiri.service.ai.route.v1",
    operation: "rumi_ai_routing_pack.ai-route.generate",
    variant: "rumi_ai_routing_pack.ai-routing.default.python",
    execution: PYTHON_HOST,
};

static AI_ROUTING_STREAM: Route = Route {
    operation: "rumi_ai_routing_pack.ai-route.stream",
    ..AI_ROUTING_GENERATE
};

static AI_STREAM_NORMALIZE: Route = Route {
    pack: "rumi_ai_stream_pack",
    function: "rumi_ai_stream_pack.ai-stream.normalize",
    contract: "tobkiri.service.ai.stream.normalize.v1",
    operation: "rumi_ai_stream_pack.ai-stream-normalize",
    variant: "rumi_ai_stream_pack.ai-stream.normalize.python",
    execution: PYTHON_HOST,
};

static AI_USAGE_COST_GENERATE: Route = Route {
    pack: "rumi_ai_usage_pack",
    function: "rumi_ai_usage_pack.ai-usage.cost",
    contract: "tobkiri.service.ai.usage.cost.v1",
    operation: "rumi_ai_usage_pack.ai-usage-cost.generate",
    variant: "rumi_ai_usage_pack.ai-usage.cost.python",
    execution: PYTHON_HOST,
};

static AI_USAGE_COST_STREAM: Route = Route {
    operation: "rumi_ai_usage_pack.ai-usage-cost.stream",
    ..AI_USAGE_COST_GENERATE
};

static PROVIDER_GENERATE: Route = Route {
    pack: "rumi_provider_adapters_pack",
    function: "rumi_provider_adapters_pack.provider.compatibility.generate",
    contract: "tobkiri.service.ai.provider.generate.v1",
    operation: "rumi_provider_adapters_pack.provider-generate",
    variant: "rumi_provider_adapters_pack.provider.compatibility.generate.python",
    execution: PYTHON_HOST,
};

static PROVIDER_STREAM: Route = Route {
    pack: "rumi_provider_adapters_pack",
    function: "rumi_provider_adapters_pack.provider.compatibility.stream",
    contract: "tobkiri.service.ai.provider.stream.v1",
    operation: "rumi_provider_adapters_pack.provider-stream",
    variant: "rumi_provider_adapters_pack.provider.compatibility.stream.python",
    execution: PYTHON_HOST,
};

/// The authenticated route is inconsistent or its verification budget is unavailable.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalRouteSelectionError {
    /// An exact route disagrees across its current Profile, Plan, or Lock.
    Inconsistent(&'static str),
    /// The current owner could not fund the record checks.
    Capacity,
}

impl fmt::Display for OriginalRouteSelectionError {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Inconsistent(reason) => {
                write!(output, "old settings route selection: {reason}")
            }
            Self::Capacity => output.write_str("old settings route selection exceeded its budget"),
        }
    }
}
impl std::error::Error for OriginalRouteSelectionError {}

use OriginalRouteSelectionError::{Capacity, Inconsistent};
type Result<T> = std::result::Result<T, OriginalRouteSelectionError>;

/// Pins for a selected function, backed by the original authenticated record storage.
pub struct OriginalRoutePin {
    route: &'static Route,
    caller: CoreValue,
    artifact_digest: CoreValue,
    executable_catalog_digest: CoreValue,
    contract_revision_digest: CoreValue,
    function_implementation_digest: CoreValue,
    authority_reference: CoreValue,
    requested_scope_digest: Digest,
}

impl OriginalRoutePin {
    /// Selected old Pack identity.
    pub fn pack_id(&self) -> &'static str {
        self.route.pack
    }

    /// Digest of the selected old Pack artifact.
    pub fn artifact_digest(&self) -> &str {
        verified_text(&self.artifact_digest)
    }

    /// Digest of the selected old executable catalog.
    pub fn executable_catalog_digest(&self) -> &str {
        verified_text(&self.executable_catalog_digest)
    }

    /// Selected legacy executable variant.
    pub fn variant_id(&self) -> &'static str {
        self.route.variant
    }

    /// Selected platform pin.
    pub fn platform(&self) -> &'static str {
        PLATFORM
    }

    /// Selected architecture pin.
    pub fn architecture(&self) -> &'static str {
        ARCHITECTURE
    }

    /// Selected runtime ABI pin.
    pub fn runtime_abi(&self) -> &'static str {
        RUNTIME_ABI
    }

    /// Selected runtime backend pin.
    pub fn backend(&self) -> &'static str {
        self.route.execution.backend
    }

    /// Selected execution kind pin.
    pub fn execution_kind(&self) -> &'static str {
        self.route.execution.kind
    }

    /// Selected isolation domain pin.
    pub fn domain_kind(&self) -> &'static str {
        self.route.execution.domain
    }

    /// Caller function named by the exact Profile edge.
    pub fn caller_function_id(&self) -> &str {
        verified_text(&self.caller)
    }

    /// Selected target function.
    pub fn function_id(&self) -> &'static str {
        self.route.function
    }

    /// Contract on the exact Profile edge.
    pub fn contract_id(&self) -> &'static str {
        self.route.contract
    }

    /// Operation on the exact Profile edge.
    pub fn operation_id(&self) -> &'static str {
        self.route.operation
    }

    /// Contract semantics committed by the function principal and scope.
    pub fn contract_revision_digest(&self) -> &str {
        verified_text(&self.contract_revision_digest)
    }

    /// Implementation digest committed by the function principal.
    pub fn function_implementation_digest(&self) -> &str {
        verified_text(&self.function_implementation_digest)
    }

    /// Profile Authority reference for this exact edge and snapshot.
    pub fn authority_reference(&self) -> &str {
        verified_text(&self.authority_reference)
    }

    /// Digest of the exact operation invocation scope.
    pub fn requested_scope_digest(&self) -> &str {
        self.requested_scope_digest.as_str()
    }
}

/// Immutable metadata for the selected Shell's direct Model List grant.
pub struct OriginalModelSettingsSelection<'a> {
    _active: &'a OriginalActiveProfile,
    model_list: OriginalRoutePin,
}

impl<'a> OriginalModelSettingsSelection<'a> {
    /// Select the exact signed Python Model List route from the active records.
    ///
    /// An absent or unsupported route yields `None`. Inconsistent records and
    /// exhausted budget produce errors. Verify the returned pin independently
    /// against the packaged catalog before using it.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Ok(
            select_model_list_records(active.records(), budget)?.map(|model_list| Self {
                _active: active,
                model_list,
            }),
        )
    }

    /// The selected Shell-to-Model List grant target.
    pub fn model_list(&self) -> &OriginalRoutePin {
        &self.model_list
    }
}

/// Immutable metadata for the two original Profile grant edges used by Model Save.
pub struct OriginalModelSaveSelection<'a> {
    _active: &'a OriginalActiveProfile,
    model_manage: OriginalRoutePin,
    provider_read: OriginalRoutePin,
}

impl<'a> OriginalModelSaveSelection<'a> {
    /// Select the exact Shell Model Manage route and its Provider Registry read.
    ///
    /// An absent or unsupported route yields `None`. Inconsistent records and
    /// exhausted budget produce errors. Verify both returned pins independently
    /// against the packaged catalogs before using them.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let Some((model_manage, provider_read)) =
            select_route_chain(active.records(), &MODEL_MANAGE, &PROVIDER_READ, budget)?
        else {
            return Ok(None);
        };
        Ok(Some(Self {
            _active: active,
            model_manage,
            provider_read,
        }))
    }

    /// The selected Shell-to-Model Manage grant target.
    pub fn model_manage(&self) -> &OriginalRoutePin {
        &self.model_manage
    }

    /// The selected Model Manage-to-Provider Registry read grant target.
    pub fn provider_read(&self) -> &OriginalRoutePin {
        &self.provider_read
    }
}

/// The original conversation-owner read grants for the three supported callers.
pub struct OriginalConversationReadSelection<'a> {
    _active: &'a OriginalActiveProfile,
    shell: Option<OriginalRoutePin>,
    saved_turn: Option<OriginalRoutePin>,
    saved_orchestration: Option<OriginalRoutePin>,
}

impl<'a> OriginalConversationReadSelection<'a> {
    /// Select each caller's exact signed Python route independently.
    ///
    /// An absent or unsupported role has no pin. Verify each selected pin
    /// independently against the packaged catalog before using it.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let [shell, saved_turn, saved_orchestration] =
            select_conversation_read_records(active.records(), budget)?;
        if shell.is_none() && saved_turn.is_none() && saved_orchestration.is_none() {
            return Ok(None);
        }
        Ok(Some(Self {
            _active: active,
            shell,
            saved_turn,
            saved_orchestration,
        }))
    }

    /// The selected Shell's direct conversation-owner read grant.
    pub fn shell(&self) -> Option<&OriginalRoutePin> {
        self.shell.as_ref()
    }

    /// The Saved Turn function's nested conversation-owner read grant.
    pub fn saved_turn(&self) -> Option<&OriginalRoutePin> {
        self.saved_turn.as_ref()
    }

    /// The Saved Orchestration function's nested conversation-owner read grant.
    pub fn saved_orchestration(&self) -> Option<&OriginalRoutePin> {
        self.saved_orchestration.as_ref()
    }
}

/// Turn Runtime caller of the private Conversation receipt and confirmation ports.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalSavedReceiptRole {
    /// The original Saved Turn function.
    SavedTurn,
    /// The original Saved Reconcile function.
    SavedReconcile,
}

impl OriginalSavedReceiptRole {
    /// The roles that may use these Conversation owner ports.
    pub const ALL: [Self; 2] = [Self::SavedTurn, Self::SavedReconcile];

    /// The function identity required on the original Profile grant.
    pub const fn caller_function_id(self) -> &'static str {
        match self {
            Self::SavedTurn => SAVED_TURN_CALLER,
            Self::SavedReconcile => SAVED_RECONCILE_CALLER,
        }
    }

    const fn index(self) -> usize {
        match self {
            Self::SavedTurn => 0,
            Self::SavedReconcile => 1,
        }
    }
}

/// The signed Conversation owner edges usable for the private saved receipt read.
pub struct OriginalSavedReceiptSelection<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [Option<OriginalRoutePin>; 2],
}

impl<'a> OriginalSavedReceiptSelection<'a> {
    /// Select each exact signed Python caller edge from the active Profile.
    /// The legacy owner packs GET and saved receipt into the same operation.
    /// Verify the selected pins against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let pins = select_saved_receipt_records(active.records(), budget)?;
        Ok(pins.iter().any(Option::is_some).then_some(Self {
            _active: active,
            pins,
        }))
    }

    /// The grant from this exact Turn Runtime caller to the Conversation owner.
    pub fn pin(&self, role: OriginalSavedReceiptRole) -> Option<&OriginalRoutePin> {
        self.pins[role.index()].as_ref()
    }
}

/// The signed Shell, Turn Runtime, and Conversation owner chain for confirmation.
pub struct OriginalConversationConfirmationChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 3],
}

impl<'a> OriginalConversationConfirmationChoice<'a> {
    /// Select the role's original Shell grant, completion grant, and Turn resource grant.
    /// Verify the selected pins against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalSavedReceiptRole,
    ) -> Result<Option<Self>> {
        Ok(
            select_conversation_confirmation_records(active.records(), budget, role)?.map(|pins| {
                Self {
                    _active: active,
                    pins,
                }
            }),
        )
    }

    /// The original Shell-to-Turn grant for this role.
    pub fn turn_caller(&self) -> &OriginalRoutePin {
        &self.pins[0]
    }

    /// The Turn-to-Conversation confirmation grant.
    pub fn completion(&self) -> &OriginalRoutePin {
        &self.pins[1]
    }

    /// The Conversation owner's nested Turn resource grant used for GET.
    pub fn turn_get(&self) -> &OriginalRoutePin {
        &self.pins[2]
    }
}

/// The signed chain from the default Shell through Saved Turn to Turn Lifecycle.
pub struct OriginalSavedTurnStartChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 2],
}

impl<'a> OriginalSavedTurnStartChoice<'a> {
    /// Select the two exact ordinary saved grants. Verify both pins against the signed Bundle.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Ok(
            select_saved_turn_start_records(active.records(), budget)?.map(|pins| Self {
                _active: active,
                pins,
            }),
        )
    }

    /// The Shell-to-Saved Turn grant followed by the Saved Turn-to-Lifecycle grant.
    pub fn pins(&self) -> &[OriginalRoutePin; 2] {
        &self.pins
    }
}

/// The signed chain from the default Shell to Saved Turn and Saved Orchestration.
pub struct OriginalSavedOrchestrationChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 2],
}

impl<'a> OriginalSavedOrchestrationChoice<'a> {
    /// Select the exact ordinary saved grants for orchestration computation.
    /// Verify both returned pins against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let Some(records) = selected_shell_records(active.records(), budget)? else {
            return Ok(None);
        };
        Ok(
            select_ordinary_saved_chain(&records, &SAVED_ORCHESTRATION)?.map(|pins| Self {
                _active: active,
                pins,
            }),
        )
    }

    /// The original Shell-to-Turn grant and the Turn-to-Saved Orchestration grant.
    pub fn pins(&self) -> &[OriginalRoutePin; 2] {
        &self.pins
    }
}

/// The saved chat role used for Gateway, Provider Adapter, and Registry selection.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalSavedLocalChatRole {
    /// The ordinary saved generate chain, including the Registry's generate child operation.
    Generate,
    /// The ordinary saved stream chain, including the Registry's stream child operation.
    Stream,
}

impl OriginalSavedLocalChatRole {
    fn routes(self) -> [&'static Route; 3] {
        match self {
            Self::Generate => [
                &GATEWAY_GENERATE,
                &PROVIDER_GENERATE,
                &PROVIDER_READ_GENERATE,
            ],
            Self::Stream => [&GATEWAY_STREAM, &PROVIDER_STREAM, &PROVIDER_READ_STREAM],
        }
    }
}

/// The signed grant chain for one ordinary saved local chat role.
pub struct OriginalSavedLocalChatChoice<'a> {
    _active: &'a OriginalActiveProfile,
    role: OriginalSavedLocalChatRole,
    pins: [OriginalRoutePin; 5],
}

impl<'a> OriginalSavedLocalChatChoice<'a> {
    /// Select one complete role from the authenticated active Profile, Lock, and Plan.
    ///
    /// An absent or unsupported grant yields `None`. Verify every returned pin
    /// against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalSavedLocalChatRole,
    ) -> Result<Option<Self>> {
        Ok(
            select_saved_local_chat_records(active.records(), budget, role)?.map(|pins| Self {
                _active: active,
                role,
                pins,
            }),
        )
    }

    /// The saved local chat role attested by all five grants.
    pub fn role(&self) -> OriginalSavedLocalChatRole {
        self.role
    }

    /// The chain from Shell through Saved Turn, Saved Orchestration, Gateway,
    /// and Provider Adapter to the original Provider Registry.
    pub fn pins(&self) -> &[OriginalRoutePin; 5] {
        &self.pins
    }
}

/// The signed chain from the default Shell through Saved Orchestration to Message Manage.
pub struct OriginalConversationAppendChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 3],
}

impl<'a> OriginalConversationAppendChoice<'a> {
    /// Select the exact grants for an ordinary saved turn's message append.
    /// Verify every returned pin against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Ok(
            select_conversation_append_records(active.records(), budget)?.map(|pins| Self {
                _active: active,
                pins,
            }),
        )
    }

    /// The Shell-to-Turn saved grant, followed by the Turn-to-Saved Orchestration grant
    /// and the Saved Orchestration-to-Message Manage grant.
    pub fn pins(&self) -> &[OriginalRoutePin; 3] {
        &self.pins
    }
}

/// The original Shell grant under which a Turn owner may settle a saved receipt.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalSavedTurnSettlementRole {
    /// The saved execution path after validating the owner result.
    SavedTurn,
    /// The explicit reconciliation path after validating the saved receipt.
    SavedReconcile,
}

impl OriginalSavedTurnSettlementRole {
    /// The distinct original Turn Runtime grants used for settlement.
    pub const ALL: [Self; 2] = [Self::SavedTurn, Self::SavedReconcile];

    fn route(self) -> &'static Route {
        match self {
            Self::SavedTurn => &SAVED_TURN,
            Self::SavedReconcile => &SAVED_RECONCILE,
        }
    }
}

/// The exact Profile grant from the selected default Shell to the Turn owner.
pub struct OriginalSavedTurnSettlementChoice<'a> {
    _active: &'a OriginalActiveProfile,
    role: OriginalSavedTurnSettlementRole,
    pin: OriginalRoutePin,
}

impl OriginalSavedTurnSettlementChoice<'_> {
    /// The settlement role selected by the original Shell grant.
    pub fn role(&self) -> OriginalSavedTurnSettlementRole {
        self.role
    }

    /// The exact grant and Python execution pins for the Turn owner.
    pub fn pin(&self) -> &OriginalRoutePin {
        &self.pin
    }
}

/// Select one original Shell-to-Turn grant from the active Profile, Lock, and Plan.
///
/// An absent or unsupported grant yields `None`. The packaged catalog must
/// independently attest the returned pin and its privileged effect class.
pub fn select_original_saved_turn_settlement<'a>(
    active: &'a OriginalActiveProfile,
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalSavedTurnSettlementRole,
) -> Result<Option<OriginalSavedTurnSettlementChoice<'a>>> {
    Ok(
        select_saved_turn_settlement_records(active.records(), budget, role)?.map(|pin| {
            OriginalSavedTurnSettlementChoice {
                _active: active,
                role,
                pin,
            }
        }),
    )
}

/// A Gateway caller and Model Registry Resolve child operation.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalModelResolveRole {
    /// Route Quote reading the Generate child operation.
    QuoteGenerate,
    /// Route Quote reading the Stream child operation.
    QuoteStream,
    /// Generate reading the Generate child operation.
    Generate,
    /// Stream reading the Stream child operation.
    Stream,
}

impl OriginalModelResolveRole {
    /// The four Gateway caller and operation combinations on the ordinary saved path.
    pub const ALL: [Self; 4] = [
        Self::QuoteGenerate,
        Self::QuoteStream,
        Self::Generate,
        Self::Stream,
    ];

    const fn index(self) -> usize {
        match self {
            Self::QuoteGenerate => 0,
            Self::QuoteStream => 1,
            Self::Generate => 2,
            Self::Stream => 3,
        }
    }

    const fn gateway_route(self) -> &'static Route {
        match self {
            Self::QuoteGenerate | Self::QuoteStream => &GATEWAY_QUOTE,
            Self::Generate => &GATEWAY_GENERATE,
            Self::Stream => &GATEWAY_STREAM,
        }
    }
}

/// Signed Model Registry Resolve grants for the four supported Gateway routes.
pub struct OriginalModelResolveSelection<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [Option<OriginalRoutePin>; 4],
}

impl<'a> OriginalModelResolveSelection<'a> {
    /// Select each exact signed Python child route independently.
    ///
    /// Absent or unsupported roles have no pin. Record inconsistencies or
    /// capacity failures prevent selection. Verify each pin independently
    /// against the packaged catalog before using it.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let pins = select_model_resolve_records(active.records(), budget)?;
        Ok(pins.iter().any(Option::is_some).then_some(Self {
            _active: active,
            pins,
        }))
    }

    /// The exact signed grant for this Gateway caller and child operation.
    pub fn pin(&self, role: OriginalModelResolveRole) -> Option<&OriginalRoutePin> {
        self.pins[role.index()].as_ref()
    }
}

/// Signed AI Routing grants on the ordinary saved path, selected independently by Gateway role.
pub struct OriginalAiRoutingSelection<'a> {
    _active: &'a OriginalActiveProfile,
    routes: [Option<[OriginalRoutePin; 4]>; 4],
}

impl<'a> OriginalAiRoutingSelection<'a> {
    /// Capture complete Shell, Turn, Orchestration, and Gateway grants for each available role.
    /// Verify their Pack and executable pins against the signed catalog before using them.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let routes = select_ai_routing_records(active.records(), budget)?;
        Ok(routes.iter().any(Option::is_some).then_some(Self {
            _active: active,
            routes,
        }))
    }

    /// Ordered grants from Shell to the selected original Routing operation.
    pub fn pins(&self, role: OriginalModelResolveRole) -> Option<&[OriginalRoutePin; 4]> {
        self.routes[role.index()].as_ref()
    }
}

/// The signed Stream Normalizer grant on the ordinary saved path.
pub struct OriginalStreamResultSelection<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 4],
}

impl<'a> OriginalStreamResultSelection<'a> {
    /// Capture the exact Gateway Stream chain and its independent normalization grant.
    /// Verify the Pack and executable pins against the signed catalog before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Ok(
            select_stream_result_records(active.records(), budget)?.map(|pins| Self {
                _active: active,
                pins,
            }),
        )
    }

    /// Ordered grants from Shell to the original Stream Normalizer operation.
    pub fn pins(&self) -> &[OriginalRoutePin; 4] {
        &self.pins
    }
}

/// The signed ordinary saved grants to one original Gateway usage-cost operation.
pub struct OriginalUsageCostChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 4],
}

impl<'a> OriginalUsageCostChoice<'a> {
    /// Select the complete Generate or Stream chain from the authenticated records.
    /// Verify every returned pin against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalModelResolveRole,
    ) -> Result<Option<Self>> {
        Ok(
            select_usage_cost_records(active.records(), budget, role)?.map(|pins| Self {
                _active: active,
                pins,
            }),
        )
    }

    /// Ordered grants from Shell to the selected original usage-cost operation.
    pub fn pins(&self) -> &[OriginalRoutePin; 4] {
        &self.pins
    }
}

/// Immutable metadata for the two Profile grant edges to old Provider prepare.
pub struct OriginalProviderPrepareSelection<'a> {
    _active: &'a OriginalActiveProfile,
    coordinator: OriginalRoutePin,
    provider: OriginalRoutePin,
}

impl<'a> OriginalProviderPrepareSelection<'a> {
    /// Select the legacy route from the authenticated active Profile.
    ///
    /// An absent or unsupported route yields `None`. Inconsistent records and
    /// exhausted budget produce errors. The resulting pins require independent
    /// verification against the packaged catalogs.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let Some((coordinator, provider)) =
            select_route_chain(active.records(), &COORDINATOR, &PROVIDER_PREPARE, budget)?
        else {
            return Ok(None);
        };
        Ok(Some(Self {
            _active: active,
            coordinator,
            provider,
        }))
    }

    /// The selected Shell-to-coordinator grant target.
    pub fn coordinator(&self) -> &OriginalRoutePin {
        &self.coordinator
    }

    /// The selected coordinator-to-Provider-prepare grant target.
    pub fn provider(&self) -> &OriginalRoutePin {
        &self.provider
    }
}

struct Records<'a> {
    shell_caller: CoreValue,
    edges: CoreValue,
    references: CoreValue,
    bindings: CoreValue,
    pack_pins: CoreValue,
    variant_pins: CoreValue,
    snapshot: CoreValue,
    budget: &'a Arc<dyn AllocationBudget>,
}

fn select_route_chain(
    active: (&CoreValue, &CoreValue, &CoreValue),
    shell_target: &'static Route,
    nested_target: &'static Route,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<(OriginalRoutePin, OriginalRoutePin)>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some(first) = records.select_shell_route(shell_target)? else {
        return Ok(None);
    };
    Ok(records
        .select_route(first.function_id(), nested_target)?
        .map(|second| (first, second)))
}

fn select_model_list_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<OriginalRoutePin>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    records.select_shell_route(&MODEL_LIST)
}

fn select_conversation_read_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<[Option<OriginalRoutePin>; 3]> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok([None, None, None]);
    };
    Ok([
        records.select_shell_route(&CONVERSATION_READ)?,
        records.select_route(SAVED_TURN_CALLER, &CONVERSATION_READ)?,
        records.select_route(SAVED_ORCHESTRATION_CALLER, &CONVERSATION_READ)?,
    ])
}

fn select_saved_receipt_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<[Option<OriginalRoutePin>; 2]> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok([None, None]);
    };
    let mut pins = [None, None];
    for role in OriginalSavedReceiptRole::ALL {
        pins[role.index()] = records.select_route(role.caller_function_id(), &CONVERSATION_READ)?;
    }
    Ok(pins)
}

fn select_conversation_confirmation_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalSavedReceiptRole,
) -> Result<Option<[OriginalRoutePin; 3]>> {
    let turn = match role {
        OriginalSavedReceiptRole::SavedTurn => &SAVED_TURN,
        OriginalSavedReceiptRole::SavedReconcile => &SAVED_RECONCILE,
    };
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    if verified_text(&records.shell_caller) != TURN_SHELL_CALLER {
        return Ok(None);
    }
    let Some(turn_caller) = records.select_shell_route(turn)? else {
        return Ok(None);
    };
    let Some(completion) =
        records.select_route(turn_caller.function_id(), &CONVERSATION_CONFIRMATION)?
    else {
        return Ok(None);
    };
    Ok(records
        .select_route(completion.function_id(), &TURN_GET)?
        .map(|turn_get| [turn_caller, completion, turn_get]))
}

fn select_conversation_append_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<[OriginalRoutePin; 3]>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some([turn, orchestration]) = select_ordinary_saved_chain(&records, &SAVED_ORCHESTRATION)?
    else {
        return Ok(None);
    };
    Ok(records
        .select_route(orchestration.function_id(), &CONVERSATION_MESSAGE_MANAGE)?
        .map(|manage| [turn, orchestration, manage]))
}

fn select_saved_local_chat_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalSavedLocalChatRole,
) -> Result<Option<[OriginalRoutePin; 5]>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some([turn, orchestration]) = select_ordinary_saved_chain(&records, &SAVED_ORCHESTRATION)?
    else {
        return Ok(None);
    };
    let [gateway_route, adapter_route, registry_route] = role.routes();
    let Some(gateway) = records.select_route(orchestration.function_id(), gateway_route)? else {
        return Ok(None);
    };
    let Some(adapter) = records.select_route(gateway.function_id(), adapter_route)? else {
        return Ok(None);
    };
    Ok(records
        .select_route(adapter.function_id(), registry_route)?
        .map(|registry| [turn, orchestration, gateway, adapter, registry]))
}

fn select_ordinary_saved_chain(
    records: &Records<'_>,
    nested_target: &'static Route,
) -> Result<Option<[OriginalRoutePin; 2]>> {
    if verified_text(&records.shell_caller) != TURN_SHELL_CALLER {
        return Ok(None);
    }
    let Some(turn) = records.select_shell_route(&SAVED_TURN)? else {
        return Ok(None);
    };
    Ok(records
        .select_route(turn.function_id(), nested_target)?
        .map(|nested| [turn, nested]))
}

fn select_saved_turn_start_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<[OriginalRoutePin; 2]>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    select_ordinary_saved_chain(&records, &TURN_LIFECYCLE)
}

fn select_saved_turn_settlement_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalSavedTurnSettlementRole,
) -> Result<Option<OriginalRoutePin>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    if verified_text(&records.shell_caller) != TURN_SHELL_CALLER {
        return Ok(None);
    }
    records.select_shell_route(role.route())
}

fn select_model_resolve_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<[Option<OriginalRoutePin>; 4]> {
    let mut pins = [None, None, None, None];
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(pins);
    };
    for role in OriginalModelResolveRole::ALL {
        let route = match role {
            OriginalModelResolveRole::QuoteGenerate | OriginalModelResolveRole::Generate => {
                &MODEL_RESOLVE_GENERATE
            }
            OriginalModelResolveRole::QuoteStream | OriginalModelResolveRole::Stream => {
                &MODEL_RESOLVE_STREAM
            }
        };
        pins[role.index()] = records.select_route(role.gateway_route().function, route)?;
    }
    Ok(pins)
}

fn select_ai_routing_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<[Option<[OriginalRoutePin; 4]>; 4]> {
    let mut routes = [None, None, None, None];
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(routes);
    };
    for role in OriginalModelResolveRole::ALL {
        let Some(saved) = select_ordinary_saved_chain(&records, &SAVED_ORCHESTRATION)? else {
            return Ok(routes);
        };
        let routing = match role {
            OriginalModelResolveRole::QuoteGenerate | OriginalModelResolveRole::Generate => {
                &AI_ROUTING_GENERATE
            }
            OriginalModelResolveRole::QuoteStream | OriginalModelResolveRole::Stream => {
                &AI_ROUTING_STREAM
            }
        };
        routes[role.index()] =
            select_saved_gateway_child(&records, saved, role.gateway_route(), routing)?;
    }
    Ok(routes)
}

fn select_stream_result_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<[OriginalRoutePin; 4]>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some(saved) = select_ordinary_saved_chain(&records, &SAVED_ORCHESTRATION)? else {
        return Ok(None);
    };
    select_saved_gateway_child(&records, saved, &GATEWAY_STREAM, &AI_STREAM_NORMALIZE)
}

fn select_usage_cost_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalModelResolveRole,
) -> Result<Option<[OriginalRoutePin; 4]>> {
    let cost = match role {
        OriginalModelResolveRole::Generate => &AI_USAGE_COST_GENERATE,
        OriginalModelResolveRole::Stream => &AI_USAGE_COST_STREAM,
        OriginalModelResolveRole::QuoteGenerate | OriginalModelResolveRole::QuoteStream => {
            return Ok(None);
        }
    };
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some(saved) = select_ordinary_saved_chain(&records, &SAVED_ORCHESTRATION)? else {
        return Ok(None);
    };
    select_saved_gateway_child(&records, saved, role.gateway_route(), cost)
}

fn select_saved_gateway_child(
    records: &Records<'_>,
    [turn, orchestration]: [OriginalRoutePin; 2],
    gateway: &'static Route,
    child: &'static Route,
) -> Result<Option<[OriginalRoutePin; 4]>> {
    let Some(gateway) = records.select_route(orchestration.function_id(), gateway)? else {
        return Ok(None);
    };
    Ok(records
        .select_route(gateway.function_id(), child)?
        .map(|child| [turn, orchestration, gateway, child]))
}

fn selected_shell_records<'a>(
    (profile, lock, plan): (&CoreValue, &CoreValue, &CoreValue),
    budget: &'a Arc<dyn AllocationBudget>,
) -> Result<Option<Records<'a>>> {
    let shell = required(profile, "shell")?;
    if !shell
        .eq_guest(&required(lock, "shell")?, budget)
        .map_err(|_| Capacity)?
    {
        return Err(Inconsistent("selected Shell differs across records"));
    }
    // Plan carries a definition digest, while Profile and Lock record the
    // definition revision and platform. Compare their shared Shell identity.
    let plan_shell = required(plan, "shell")?;
    for field in [
        "provider_id",
        "pack_id",
        "artifact_digest",
        "executable_artifact_digest",
        "contract_id",
    ] {
        let expected = text_field(&shell, field)?;
        if !is_text(&plan_shell, field, verified_text(&expected)) {
            return Err(Inconsistent("selected Shell differs across records"));
        }
    }
    if !is_text(&shell, "contract_id", "app.shell.v1") {
        return Ok(None);
    }
    Ok(Some(Records {
        shell_caller: text_field(&shell, "provider_id")?,
        edges: required(profile, "requested_edges")?,
        references: required(profile, "authority_references")?,
        bindings: required(plan, "bindings")?,
        pack_pins: required(profile, "packs")?,
        variant_pins: required(lock, "variant_pins")?,
        snapshot: digest_field(profile, "profile_authority_snapshot_digest")?,
        budget,
    }))
}

impl Records<'_> {
    fn select_shell_route(&self, route: &'static Route) -> Result<Option<OriginalRoutePin>> {
        self.select_route(verified_text(&self.shell_caller), route)
    }

    fn select_route(
        &self,
        caller: &str,
        route: &'static Route,
    ) -> Result<Option<OriginalRoutePin>> {
        let matches_route = |value: &CoreValue| {
            is_text(value, "caller_function_id", caller)
                && is_text(value, "contract_id", route.contract)
                && is_text(value, "operation_id", route.operation)
        };
        let edge = self.unique(&self.edges, |edge| {
            matches_route(edge) && is_text(edge, "target_provider_id", route.function)
        })?;
        let binding = self.unique(&self.bindings, |binding| {
            matches_route(binding)
                && binding
                    .get_key("function_principal")
                    .is_some_and(|principal| is_text(&principal, "function_id", route.function))
        })?;
        let (edge, binding) = match (edge, binding) {
            (None, None) => return Ok(None),
            (Some(edge), Some(binding)) => (edge, binding),
            _ => return Err(Inconsistent("the Profile edge and Plan binding differ")),
        };

        let grant = is_profile_grant(&edge)?;
        if grant != is_profile_grant(&binding)? {
            return Err(Inconsistent(
                "the Profile edge and Plan Authority modes differ",
            ));
        }
        let reference = text_field(&edge, "authority_reference")?;
        let expected_reference =
            authority_reference(caller, route, verified_text(&self.snapshot), self.budget)?;
        let reference_text = verified_text(&reference);
        if reference_text.strip_prefix("authority-ref:") != Some(expected_reference.hex())
            || !is_text(&binding, "authority_reference", reference_text)
            || self
                .unique(&self.references, |value| {
                    value.as_str() == Some(reference_text)
                })?
                .is_none()
        {
            return Err(Inconsistent(
                "the Authority reference differs from the Profile edge",
            ));
        }

        let artifact = digest_field(&binding, "artifact_digest")?;
        let catalog = digest_field(&binding, "executable_catalog_digest")?;
        let principal = required(&binding, "function_principal")?;
        let implementation = digest_field(&principal, "function_implementation_digest")?;
        let revision = digest_field(&principal, "contract_revision_digest")?;
        if !is_text(&binding, "pack_id", route.pack)
            || !is_text(&principal, "operation_id", route.operation)
            || !is_text(
                &principal,
                "parent_artifact_digest",
                verified_text(&artifact),
            )
        {
            return Err(Inconsistent(
                "the selected function principal differs from its route",
            ));
        }
        let pack = self
            .unique(&self.pack_pins, |pack| is_text(pack, "pack_id", route.pack))?
            .ok_or(Inconsistent(
                "the selected Pack is missing from the Profile",
            ))?;
        if !is_text(&pack, "artifact_digest", verified_text(&artifact)) {
            return Err(Inconsistent(
                "the selected Pack artifact differs from the Profile",
            ));
        }

        let variant = text_field(&binding, "variant_id")?;
        let pin = self
            .unique(&self.variant_pins, |pin| {
                is_text(pin, "pack_id", route.pack)
                    && is_text(pin, "variant_id", verified_text(&variant))
            })?
            .ok_or(Inconsistent(
                "the selected variant is missing from the Lock",
            ))?;
        if pin.object().is_none_or(|pin| pin.len() != PIN_FIELDS.len())
            || PIN_FIELDS.into_iter().any(|field| {
                !binding
                    .get_key(field)
                    .and_then(|value| value.as_str().map(|text| is_text(&pin, field, text)))
                    .unwrap_or(false)
            })
        {
            return Err(Inconsistent("the Lock variant pin differs from the Plan"));
        }

        let scope = required(&edge, "requested_scope_template")?;
        let requested_scope_digest = scope.digest_guest(self.budget).map_err(|_| Capacity)?;
        if !is_text(
            &binding,
            "requested_scope_digest",
            requested_scope_digest.as_str(),
        ) {
            return Err(Inconsistent(
                "the Plan scope digest differs from the Profile edge",
            ));
        }
        let adapters = required(&binding, "adapter_digests")?;
        let adapters = adapters.array().ok_or(Inconsistent("adapter_digests"))?;
        if !grant
            || verified_text(&variant) != route.variant
            || route
                .execution_fields()
                .into_iter()
                .any(|(field, expected)| !is_text(&binding, field, expected))
            || !adapters.is_empty()
            || !exact_scope(&scope, route, verified_text(&revision))?
        {
            return Ok(None);
        }
        Ok(Some(OriginalRoutePin {
            route,
            caller: text_field(&binding, "caller_function_id")?,
            artifact_digest: artifact,
            executable_catalog_digest: catalog,
            contract_revision_digest: revision,
            function_implementation_digest: implementation,
            authority_reference: reference,
            requested_scope_digest,
        }))
    }

    fn unique(
        &self,
        rows: &CoreValue,
        matches: impl Fn(&CoreValue) -> bool,
    ) -> Result<Option<CoreValue>> {
        let rows = rows.array().ok_or(Inconsistent("record list is invalid"))?;
        // A match probes only fixed keys and compares against the already
        // selected identities. No row storage is copied or reallocated.
        charge(self.budget, rows.len().saturating_mul(8))?;
        let mut matches = rows.iter().filter(matches);
        let first = matches.next();
        if matches.next().is_some() {
            return Err(Inconsistent("the selected record is duplicated"));
        }
        Ok(first)
    }
}

fn is_profile_grant(value: &CoreValue) -> Result<bool> {
    match value.get_key("authority_mode") {
        // The old resolver omits the default mode in the Plan.
        None => Ok(true),
        Some(mode) if mode.as_str() == Some("profile_grant") => Ok(true),
        Some(mode) if mode.as_str() == Some("interactive_only") => Ok(false),
        Some(_) => Err(Inconsistent("the selected Authority mode is invalid")),
    }
}

fn authority_reference(
    caller: &str,
    route: &Route,
    snapshot: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Digest> {
    // profile_capture._authority_reference commits the exact four-part edge
    // and the current snapshot in canonical JSON. Profile identifiers cannot
    // contain JSON escapes or the edge separator.
    require_identifier(caller, budget)?;
    let parts = [
        "{\"edge\":\"",
        caller,
        "|",
        route.function,
        "|",
        route.contract,
        "|",
        route.operation,
        "\",\"profile_authority_snapshot_digest\":\"",
        snapshot,
        "\",\"schema\":\"io.tobkiri.profile-authority-edge.v1\"}",
    ];
    charge(budget, parts.into_iter().map(str::len).sum())?;
    let mut hash = Sha256::new();
    for part in parts {
        hash.update(part.as_bytes());
    }
    Ok(Digest::from_sha256(hash.finalize().into()))
}

fn require_identifier(caller: &str, budget: &Arc<dyn AllocationBudget>) -> Result<()> {
    charge(budget, caller.len())?;
    if caller
        .bytes()
        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte))
        && !caller.is_empty()
    {
        Ok(())
    } else {
        Err(Inconsistent("the selected Shell identifier is invalid"))
    }
}

fn exact_scope(scope: &CoreValue, route: &Route, revision: &str) -> Result<bool> {
    let dimensions = required(scope, "dimensions")?;
    let matches_dimension = |field, expected| {
        dimensions
            .get_key(field)
            .and_then(|value| {
                let values = value.array()?;
                values.get(0).filter(|_| values.len() == 1)
            })
            .is_some_and(|value| value.as_str() == Some(expected))
    };
    Ok(scope.object().is_some_and(|object| object.len() == 6)
        && is_text(scope, "capability", "operation.invoke")
        && is_text(scope, "semantics_digest", revision)
        && dimensions.object().is_some_and(|object| object.len() == 2)
        && matches_dimension("contract", route.contract)
        && matches_dimension("operation", route.operation)
        && required(scope, "quotas")?
            .object()
            .is_some_and(|quotas| quotas.is_empty())
        && required(scope, "opaque")?.as_bool() == Some(false)
        && matches!(
            required(scope, "exact_request_digest")?.inspect(),
            CoreRef::Null
        ))
}

fn required(value: &CoreValue, field: &'static str) -> Result<CoreValue> {
    value.get_key(field).ok_or(Inconsistent(field))
}

fn is_text(value: &CoreValue, field: &str, expected: &str) -> bool {
    value
        .get_key(field)
        .is_some_and(|value| value.as_str() == Some(expected))
}

fn text_field(value: &CoreValue, field: &'static str) -> Result<CoreValue> {
    let value = required(value, field)?;
    if value.as_str().is_none_or(str::is_empty) {
        return Err(Inconsistent(field));
    }
    Ok(value)
}

fn digest_field(value: &CoreValue, field: &'static str) -> Result<CoreValue> {
    let value = text_field(value, field)?;
    let digest = verified_text(&value);
    if digest.strip_prefix("sha256:").is_none_or(|hex| {
        hex.len() != 64
            || !hex
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    }) {
        return Err(Inconsistent(field));
    }
    Ok(value)
}

fn verified_text(value: &CoreValue) -> &str {
    value.as_str().expect("verified selection string")
}

fn charge(budget: &Arc<dyn AllocationBudget>, work: usize) -> Result<()> {
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: work as u64,
        })
        .map_err(|_| Capacity)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};
    use tobkiri_canon::AllocationError;

    struct TestBudget;

    impl AllocationBudget for TestBudget {
        fn reserve(&self, _: AllocationRequest) -> std::result::Result<(), AllocationError> {
            Ok(())
        }

        fn release(&self, _: u64) {}
    }

    // Use the snapshot and exact edge/scope digests for the Registry routes.
    // The rest of the original activation closure is omitted.
    fn records() -> Value {
        const SHELL: &str = "shell.tauri.default";
        let mut shell = json!({
            "contract_id": "app.shell.v1",
            "provider_id": SHELL,
            "pack_id": SHELL,
            "artifact_digest": Digest::hash(b"shell").as_str(),
            "executable_artifact_digest": Digest::hash(b"executable").as_str()
        });
        let mut plan_shell = shell.clone();
        plan_shell["definition_digest"] = json!(Digest::hash(b"definition").as_str());
        shell["definition_revision"] = json!(Digest::hash(b"revision").as_str());
        shell["architecture"] = json!("arm64");
        shell["platform"] = json!("macos");
        let mut records = json!({
            "profile": {
                "shell": shell,
                "packs": [],
                "requested_edges": [],
                "authority_references": [],
                "profile_authority_snapshot_digest":
                    "sha256:0df912a62eed85abe0cadd3f18e5e419d2b011f7be059759fe71537879bdb276"
            },
            "lock": {"shell": shell, "variant_pins": []},
            "plan": {"shell": plan_shell, "bindings": []}
        });
        let rows = [
            (
                &COORDINATOR,
                SHELL,
                "authority-ref:8a8b1444d963a1da5fd8a89b9486c7c575994bd2d20e7f85c290ae59e7271911",
                "sha256:87f5d2c6e9ee6430c26ba87fe91e05bf0c21bc34dbe553ca81971154eaa45242",
                "sha256:e4783f46c56c558c2435d944ef2662d3a91b3d102226dbc151feff009ef821ed",
            ),
            (
                &PROVIDER_PREPARE,
                COORDINATOR.function,
                "authority-ref:33b0249506c3d5ae1b94c9aa40fa766708ec93a2ff1e24b9cc9e7ff11de7c35f",
                "sha256:f4d8e87f748eee562a010984e56839084ac012ad6bd7ceec5f55f2e30bf6adb9",
                "sha256:5e79aeb456ca085625c28d9f4d8fe219d1c1a0a621bdcabd66ba8b8eec01a5e3",
            ),
            (
                &MODEL_LIST,
                SHELL,
                "authority-ref:805eb0ebcb6d6d912e6f9080be3a75c5d16ff2f0ffaf20f7fe8dc5bce6eafac3",
                "sha256:2604f5c72ce089f2cfcc4c4561c4b7ac04119c64b52ac1f495ac013d1a946658",
                "sha256:64fa11ae6949412f39039e7069da0a614e78401c55071be40755f2920b28932e",
            ),
            (
                &MODEL_MANAGE,
                SHELL,
                "authority-ref:370c601554aeff4cfeea4302eb9e9a53652f6ff5f801936d26b558a122899a2b",
                "sha256:e6a4c0eb6f5dd04d117aa8343af2319b3d5d06a502b14ecebfbba08e0b2dd052",
                "sha256:7e5d3aa5434e2f9a7d23ed7df2bab9df80d2bd8bcf5811ef9af33aa1bada567b",
            ),
            (
                &PROVIDER_READ,
                MODEL_MANAGE.function,
                "authority-ref:e432e888768241f9db182dfbe3b5278b04787a3d6e16f6856dd512a5af3654a7",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:1b58a95d7c67e6f789b5e17a10a61c360c706443adbbe3026502e7e29395bd6f",
            ),
            (
                &PROVIDER_READ,
                SHELL,
                "authority-ref:c0b9128e7e44426c9df7b301c4903249948d782b264fdf9c33e9d3b9aebbe310",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:1b58a95d7c67e6f789b5e17a10a61c360c706443adbbe3026502e7e29395bd6f",
            ),
            (
                &CONVERSATION_READ,
                SHELL,
                "authority-ref:1ac460445bfec95ce8451f251740514d6e4275fb9fdb3bbe3f60a1df9ad41b83",
                "sha256:254d3ee4bdbe11bf21f658bbb3b89a1d6510ef7dbf77fb9108dcc4b426496156",
                "sha256:d0069dfedd4def12d6921155433e7f280b2e2fb4842b9c32d07062f913dfaa8c",
            ),
            (
                &CONVERSATION_READ,
                SAVED_TURN_CALLER,
                "authority-ref:102d72ffc48697e9c5aa19b70b82a502237abf81b823ce3bf6fc65ff34729462",
                "sha256:254d3ee4bdbe11bf21f658bbb3b89a1d6510ef7dbf77fb9108dcc4b426496156",
                "sha256:d0069dfedd4def12d6921155433e7f280b2e2fb4842b9c32d07062f913dfaa8c",
            ),
            (
                &CONVERSATION_READ,
                SAVED_ORCHESTRATION_CALLER,
                "authority-ref:5d9e74a9f4f8824cdef4dafb34937f7721fd09730df71eb54c14c48a3634b80d",
                "sha256:254d3ee4bdbe11bf21f658bbb3b89a1d6510ef7dbf77fb9108dcc4b426496156",
                "sha256:d0069dfedd4def12d6921155433e7f280b2e2fb4842b9c32d07062f913dfaa8c",
            ),
            (
                &MODEL_RESOLVE_GENERATE,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:a6fedd87679222f2f377774171406f2c65ad5d1f643ba367022ca6a67e30964f",
                "sha256:2604f5c72ce089f2cfcc4c4561c4b7ac04119c64b52ac1f495ac013d1a946658",
                "sha256:e0adca96baa7c877bc049187d16aee8eee3dbc00c5a06f5160e7c92baa7be0f6",
            ),
            (
                &MODEL_RESOLVE_STREAM,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:d8d87c62e86763d17ca93bd0d95669062aa8da60c069294e24c56ae3b15a0c59",
                "sha256:2604f5c72ce089f2cfcc4c4561c4b7ac04119c64b52ac1f495ac013d1a946658",
                "sha256:f5746a0621a144db146e8e05bd24564378fb7c72fd8d3b4d07ecf7d9a4369472",
            ),
            (
                &MODEL_RESOLVE_GENERATE,
                GATEWAY_GENERATE_CALLER,
                "authority-ref:08510fb1f887bd4e1af388b73f30f0850b2392f6fb174d5bd4a948403b5a9bde",
                "sha256:2604f5c72ce089f2cfcc4c4561c4b7ac04119c64b52ac1f495ac013d1a946658",
                "sha256:e0adca96baa7c877bc049187d16aee8eee3dbc00c5a06f5160e7c92baa7be0f6",
            ),
            (
                &MODEL_RESOLVE_STREAM,
                GATEWAY_STREAM_CALLER,
                "authority-ref:a180b8c58d6bea4e5521b0cdc85924aece477562653a7a3e27a428ef8bc739b5",
                "sha256:2604f5c72ce089f2cfcc4c4561c4b7ac04119c64b52ac1f495ac013d1a946658",
                "sha256:f5746a0621a144db146e8e05bd24564378fb7c72fd8d3b4d07ecf7d9a4369472",
            ),
            (
                &CONVERSATION_READ,
                SAVED_RECONCILE_CALLER,
                "authority-ref:5af46c5d5d3b05cb0e6d48164c33d648e2ac815188cc9c06914b0a3672825ef0",
                "sha256:254d3ee4bdbe11bf21f658bbb3b89a1d6510ef7dbf77fb9108dcc4b426496156",
                "sha256:d0069dfedd4def12d6921155433e7f280b2e2fb4842b9c32d07062f913dfaa8c",
            ),
            (
                &SAVED_TURN,
                SHELL,
                "authority-ref:21481aa6225da5be0e04f67071df262275cb65368e69915a6bb1460f98e8bd5f",
                "sha256:b7dda70b162f84a9af42022f3025059c074d6a66dc5b7b2b7ef8b153d48fc8c9",
                "sha256:def47bb0d27a6a624fd7ac61b770fa148a287173f52e79416a0476927a17b930",
            ),
            (
                &SAVED_RECONCILE,
                SHELL,
                "authority-ref:77f95502f06a8b3ef0acb863f32cd7d7056c2d90579509f7c1ca3aa298c01dc6",
                "sha256:4206c74719c2a3e9a033544695736ef6273747c2c9a186ec7330a57fca283070",
                "sha256:ec01327293949904e663230af519789526a68e3acafcef188a7c01a7d70bfc2c",
            ),
            (
                &CONVERSATION_CONFIRMATION,
                SAVED_TURN_CALLER,
                "authority-ref:bb6b90a4e783214c6ba9541b250150e793242d860ba60a0e36b530dee2b7a076",
                "sha256:577b2279c630300e752f95ce310c299305d73ffc80ca92c21fbc7b6a32a43086",
                "sha256:96f6283a9ab03e11ede03f0866094bca5c053504c679cf39c4878f3e509e7618",
            ),
            (
                &CONVERSATION_CONFIRMATION,
                SAVED_RECONCILE_CALLER,
                "authority-ref:04ae59d737bb5eda074170713a1bca1c3d75ec64e0c58873d64685994a7eefab",
                "sha256:577b2279c630300e752f95ce310c299305d73ffc80ca92c21fbc7b6a32a43086",
                "sha256:96f6283a9ab03e11ede03f0866094bca5c053504c679cf39c4878f3e509e7618",
            ),
            (
                &TURN_GET,
                CONVERSATION_CONFIRMATION.function,
                "authority-ref:7676c7efcc2b7b924b2eb95cf490b39d8f375adb1fbfd2b84e2c4d8b8f1fdfc5",
                "sha256:acf98c5bf82405666c710bdd2a4b6199d108ffdc70deaed0157c1b0f6e9f450a",
                "sha256:f4c5dd96f70e7e108a5b46b9adbf244990326628e62592b618ba77b2fb92de3d",
            ),
            (
                &SAVED_ORCHESTRATION,
                SAVED_TURN_CALLER,
                "authority-ref:fcdb3a500a3174f9aeb463d61b569e894a1dc0bc5637b8b504320f0db6856996",
                "sha256:c8f748e29c75e01a3ea58533a66d84a4debc410808ba7bd45c30e04fb81efc50",
                "sha256:0fd9eb1e037d917b1b1a2d6d351870c00465a70539e341e27e46d0e45b6125cf",
            ),
            (
                &CONVERSATION_MESSAGE_MANAGE,
                SAVED_ORCHESTRATION_CALLER,
                "authority-ref:109e313f6f0b1f266b39c69b090f59c012ea090c8eb72332ee11603e90c9a168",
                "sha256:9dd8f4b40122ccebecee571c81ad10a71b07bc791baee7dedfa96ed8214e5fc4",
                "sha256:f3ab7b69d73b0e79b262bc2900c652da5f92e15d13c44c160eb8e7f8b2f9f3e3",
            ),
            (
                &TURN_LIFECYCLE,
                SAVED_TURN_CALLER,
                "authority-ref:d72a9902e8d29286524595021657b7031e8a3377f578468e34624047478ff616",
                "sha256:a7cf23d352ca9838379fd3acc501c52fb5cb7e4b0b708d4d0c5c637a3bb680f5",
                "sha256:812396986fe5f82a4d98677f28cdb70e74759d81fdea3bb3887695f3721555eb",
            ),
            (
                &GATEWAY_GENERATE,
                SAVED_ORCHESTRATION_CALLER,
                "authority-ref:00942f218466c4f67a5d9477359f3806ef90503f6ce3888dc5ae4dd01f6e98a7",
                "sha256:39eddd13b6cfae773c5f2e31705f8f073b42e54d0160f49918f87b8eeb63d768",
                "sha256:aed4252c92a298de9bed574c81b5e421fa7f592541ce146b0e77b0c085f42370",
            ),
            (
                &PROVIDER_GENERATE,
                GATEWAY_GENERATE_CALLER,
                "authority-ref:84c54fcdf4984c8a65048eccb3bc4796c7392c919e12a8a9318f5ccac91a93aa",
                "sha256:729e08de33262fe2c1b9269ed22d4090f0fcd29321301c46d093c808c9211e95",
                "sha256:cc0b26b4271cc311db4b05c068ee4b617706c3bc8e9cc22d0941511ac836dde9",
            ),
            (
                &PROVIDER_READ_GENERATE,
                PROVIDER_GENERATE.function,
                "authority-ref:d764a130d302fa9d8cf4824a35f6d1db94e7ffe949edfb18d304b7defab5a18f",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:01dea66907528b42fe211fda67bf9c09aeeecf6bf092de43f15317a0f37c6184",
            ),
            (
                &GATEWAY_STREAM,
                SAVED_ORCHESTRATION_CALLER,
                "authority-ref:ca32a2e15ecf8b67f5b5a602daac59a9af9916e44cd3fc35923165a97400318d",
                "sha256:487608b15370de52d2d22aef5b42ee1947a37476b3588c3ee8ce60bf35ee5597",
                "sha256:407fbb8fc1f1900189e702154de234f1331aae8af49db8a95e7729e57a206881",
            ),
            (
                &PROVIDER_STREAM,
                GATEWAY_STREAM_CALLER,
                "authority-ref:d2e09001de0a825ab0972538092b7eaff52576f0a263df4eea7fa62ab167baaa",
                "sha256:8fd2fa8b32bf0d21ffbf0ca8976feeefe042ed54a3e8c6ef93bf00bb9cba971d",
                "sha256:4b677e26ac0d03b73e6b01e4225462d2af517b0561bd9ff56faeef1c1f677f4a",
            ),
            (
                &PROVIDER_READ_STREAM,
                PROVIDER_STREAM.function,
                "authority-ref:a9caaf6ac6edc7bc9fc015e4ba67991d11a11e8d701c04a6155639c0e0692837",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:ba31ddc2fe6a194376c0130d1cce5b2bd6af8c36c5c7353584443247786bf883",
            ),
            (
                &GATEWAY_QUOTE,
                SAVED_ORCHESTRATION_CALLER,
                "authority-ref:10f5f34aede0c4c5358a45de469203685618b642f69c6862fd437bc896afee88",
                "sha256:fd30d5ee7dc57aae6f8a968a52015109157bef92bc516ec83b53ec3edaf56038",
                "sha256:88f5ab50ea4d55c1c824fd011bbf1856582fef634a19983f163bc14da7b760c8",
            ),
            (
                &AI_ROUTING_GENERATE,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:85be80c40b116f74bf1f10c7278270e516401ebfba0dd7537e2590872c9f4b4f",
                "sha256:4d1d08cfdfacb74c67488e417daf2a56cfa51a981e216d3f1a49c734dd6ced4c",
                "sha256:113b941fe721b9bb4036199df0dcb16d73222867a621820fe246d1214249eff7",
            ),
            (
                &AI_ROUTING_STREAM,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:6aff2449611187e4113a479af443f7df8100c5b7f4edc07b26ab44566c3e70aa",
                "sha256:4d1d08cfdfacb74c67488e417daf2a56cfa51a981e216d3f1a49c734dd6ced4c",
                "sha256:962c6a7307c5fb116de10536d02bbb37b9b504dc28cfa016cafeb0cb797a215e",
            ),
            (
                &AI_ROUTING_GENERATE,
                GATEWAY_GENERATE_CALLER,
                "authority-ref:1050c40bbacd533e78039af2311e7651bca548ca36edc52f0189e077d1fc93f1",
                "sha256:4d1d08cfdfacb74c67488e417daf2a56cfa51a981e216d3f1a49c734dd6ced4c",
                "sha256:113b941fe721b9bb4036199df0dcb16d73222867a621820fe246d1214249eff7",
            ),
            (
                &AI_ROUTING_STREAM,
                GATEWAY_STREAM_CALLER,
                "authority-ref:1a847c2f3b3ba831ddab7efd0869b725268fc9e9f849a5fe4fb41d34a5aef428",
                "sha256:4d1d08cfdfacb74c67488e417daf2a56cfa51a981e216d3f1a49c734dd6ced4c",
                "sha256:962c6a7307c5fb116de10536d02bbb37b9b504dc28cfa016cafeb0cb797a215e",
            ),
            (
                &AI_STREAM_NORMALIZE,
                GATEWAY_STREAM_CALLER,
                "authority-ref:9fc03c89152fa46ceb79a7566a1e984a542bdab24d543756d82ebc928edeecba",
                "sha256:88929a94dedfbbc04a8ca78a3c88c01bcb2703a480402e8ad0afbfea8b62d6ad",
                "sha256:7c04f898ad54e5cb797d8d02678a523bfd8dbcf577c4fca740032bd4c14ae663",
            ),
            (
                &AI_USAGE_COST_GENERATE,
                GATEWAY_GENERATE_CALLER,
                "authority-ref:0f711ab17a49e69b96a0b18a233e30221f9a6a325edee143fcad89f6c8f6e9c0",
                "sha256:04a8f126370dafe88c38fe8b465b97e666097e515823d55921d98f0c588d68fc",
                "sha256:a4c757563e7aa712b28f51b819a42e8d640385b914d6a27bc6410a3a0c83b88b",
            ),
            (
                &AI_USAGE_COST_STREAM,
                GATEWAY_STREAM_CALLER,
                "authority-ref:2d8ac0a3647b5cb361a5b80047d586b32b4dbc7486624b8a68312c1f1c495c92",
                "sha256:04a8f126370dafe88c38fe8b465b97e666097e515823d55921d98f0c588d68fc",
                "sha256:742b951d24cb5133eb32c76ac8189d2b646f5a927d77251ea91246a38ddd72c6",
            ),
        ];
        for (route, caller, reference, revision, scope_digest) in rows {
            let artifact = Digest::hash(route.pack.as_bytes());
            let catalog = Digest::hash(route.variant.as_bytes());
            let implementation = Digest::hash(route.function.as_bytes());
            let pin = json!({
                "pack_id": route.pack,
                "variant_id": route.variant,
                "artifact_digest": artifact.as_str(),
                "executable_catalog_digest": catalog.as_str(),
                "platform": "any",
                "architecture": "any",
                "runtime_abi": "python3.13",
                "backend": route.execution.backend,
                "execution_kind": route.execution.kind,
                "domain_kind": route.execution.domain
            });
            let mut binding = pin.clone();
            binding["caller_function_id"] = json!(caller);
            binding["contract_id"] = json!(route.contract);
            binding["operation_id"] = json!(route.operation);
            binding["authority_reference"] = json!(reference);
            binding["requested_scope_digest"] = json!(scope_digest);
            binding["adapter_digests"] = json!([]);
            binding["function_principal"] = json!({
                "function_id": route.function,
                "contract_revision_digest": revision,
                "function_implementation_digest": implementation.as_str(),
                "operation_id": route.operation,
                "parent_artifact_digest": artifact.as_str(),
            });
            records["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .push(binding);
            let variant_pins = records["lock"]["variant_pins"].as_array_mut().unwrap();
            if !variant_pins.contains(&pin) {
                variant_pins.push(pin);
            }
            let packs = records["profile"]["packs"].as_array_mut().unwrap();
            if !packs.iter().any(|pack| pack["pack_id"] == route.pack) {
                packs.push(json!({
                    "pack_id": route.pack,
                    "role": "provider",
                    "artifact_digest": artifact.as_str(),
                }));
            }
            records["profile"]["authority_references"]
                .as_array_mut()
                .unwrap()
                .push(json!(reference));
            records["profile"]["requested_edges"]
                .as_array_mut().unwrap().push(json!({
                    "caller_function_id": caller,
                    "target_provider_id": route.function,
                    "contract_id": route.contract,
                    "operation_id": route.operation,
                    "authority_mode": "profile_grant",
                    "authority_reference": reference,
                    "requested_scope_template": {
                        "capability": "operation.invoke",
                        "semantics_digest": revision,
                        "dimensions": {"operation": [route.operation], "contract": [route.contract]},
                        "quotas": {},
                        "opaque": false,
                        "exact_request_digest": null,
                    },
                }));
        }
        records
    }

    fn parse_records(records: &Value, budget: &Arc<dyn AllocationBudget>) -> [CoreValue; 3] {
        ["profile", "lock", "plan"].map(|field| {
            let bytes = serde_json::to_vec(&records[field]).unwrap();
            CoreValue::parse_guest(&bytes, budget).unwrap()
        })
    }

    fn select(records: &Value) -> Result<Option<(OriginalRoutePin, OriginalRoutePin)>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_route_chain(
            (&profile, &lock, &plan),
            &COORDINATOR,
            &PROVIDER_PREPARE,
            &budget,
        )
    }

    fn select_model_list(records: &Value) -> Result<Option<OriginalRoutePin>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_model_list_records((&profile, &lock, &plan), &budget)
    }

    fn select_model_save(records: &Value) -> Result<Option<(OriginalRoutePin, OriginalRoutePin)>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_route_chain(
            (&profile, &lock, &plan),
            &MODEL_MANAGE,
            &PROVIDER_READ,
            &budget,
        )
    }

    fn select_conversation_read(records: &Value) -> Result<[Option<OriginalRoutePin>; 3]> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_conversation_read_records((&profile, &lock, &plan), &budget)
    }

    fn select_model_resolve(records: &Value) -> Result<[Option<OriginalRoutePin>; 4]> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_model_resolve_records((&profile, &lock, &plan), &budget)
    }

    fn select_saved_receipt(records: &Value) -> Result<[Option<OriginalRoutePin>; 2]> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_saved_receipt_records((&profile, &lock, &plan), &budget)
    }

    fn select_confirmation(
        records: &Value,
        role: OriginalSavedReceiptRole,
    ) -> Result<Option<[OriginalRoutePin; 3]>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_conversation_confirmation_records((&profile, &lock, &plan), &budget, role)
    }

    fn select_append(records: &Value) -> Result<Option<[OriginalRoutePin; 3]>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_conversation_append_records((&profile, &lock, &plan), &budget)
    }

    fn select_saved_orchestration(records: &Value) -> Result<Option<[OriginalRoutePin; 2]>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        let Some(records) = selected_shell_records((&profile, &lock, &plan), &budget)? else {
            return Ok(None);
        };
        select_ordinary_saved_chain(&records, &SAVED_ORCHESTRATION)
    }

    const MODEL_MANAGE_ROW: usize = 3;
    const MODEL_PROVIDER_READ_ROW: usize = 4;
    const CONVERSATION_SHELL_ROW: usize = 6;
    const CONVERSATION_SAVED_TURN_ROW: usize = 7;
    const CONVERSATION_SAVED_ORCHESTRATION_ROW: usize = 8;
    const MODEL_RESOLVE_ROWS: std::ops::Range<usize> = 9..13;
    const CONVERSATION_SAVED_RECONCILE_ROW: usize = 13;
    const SAVED_TURN_SETTLEMENT_ROW: usize = 14;
    const SAVED_RECONCILE_SETTLEMENT_ROW: usize = 15;
    const SAVED_TURN_CONFIRMATION_ROW: usize = 16;
    const SAVED_RECONCILE_CONFIRMATION_ROW: usize = 17;
    const CONFIRMATION_TURN_GET_ROW: usize = 18;
    const SAVED_ORCHESTRATION_ROW: usize = 19;
    const MESSAGE_MANAGE_ROW: usize = 20;
    const TURN_LIFECYCLE_ROW: usize = 21;
    const LOCAL_CHAT_GENERATE_ROWS: [usize; 3] = [22, 23, 24];
    const LOCAL_CHAT_STREAM_ROWS: [usize; 3] = [25, 26, 27];
    const GATEWAY_QUOTE_ROW: usize = 28;
    const AI_ROUTING_ROWS: std::ops::Range<usize> = 29..33;
    const STREAM_RESULT_ROW: usize = 33;
    const USAGE_COST_GENERATE_ROW: usize = 34;
    const USAGE_COST_STREAM_ROW: usize = 35;

    fn select_saved_local_chat(
        records: &Value,
        role: OriginalSavedLocalChatRole,
    ) -> Result<Option<[OriginalRoutePin; 5]>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_saved_local_chat_records((&profile, &lock, &plan), &budget, role)
    }

    fn select_ai_routing(records: &Value) -> Result<[Option<[OriginalRoutePin; 4]>; 4]> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_ai_routing_records((&profile, &lock, &plan), &budget)
    }

    fn select_stream_result(records: &Value) -> Result<Option<[OriginalRoutePin; 4]>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_stream_result_records((&profile, &lock, &plan), &budget)
    }

    #[test]
    fn usage_cost_requires_only_its_own_exact_ordinary_gateway_chain() {
        use OriginalModelResolveRole::{Generate, QuoteGenerate, QuoteStream, Stream};

        let select = |records: &Value, role| {
            let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
            let [profile, lock, plan] = parse_records(records, &budget);
            select_usage_cost_records((&profile, &lock, &plan), &budget, role)
        };
        let fixture = records();
        assert!(select(&fixture, QuoteGenerate).unwrap().is_none());
        assert!(select(&fixture, QuoteStream).unwrap().is_none());
        for (role, other, gateway_row, child_row, cost) in [
            (
                Generate,
                Stream,
                LOCAL_CHAT_GENERATE_ROWS[0],
                USAGE_COST_GENERATE_ROW,
                &AI_USAGE_COST_GENERATE,
            ),
            (
                Stream,
                Generate,
                LOCAL_CHAT_STREAM_ROWS[0],
                USAGE_COST_STREAM_ROW,
                &AI_USAGE_COST_STREAM,
            ),
        ] {
            let [turn, orchestration, gateway, usage] = select(&fixture, role).unwrap().unwrap();
            assert_eq!(turn.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(turn.function_id(), SAVED_TURN.function);
            assert_eq!(orchestration.caller_function_id(), turn.function_id());
            assert_eq!(orchestration.function_id(), SAVED_ORCHESTRATION.function);
            assert_eq!(gateway.caller_function_id(), orchestration.function_id());
            assert_eq!(gateway.function_id(), role.gateway_route().function);
            assert_eq!(usage.caller_function_id(), gateway.function_id());
            assert_eq!(usage.function_id(), cost.function);
            assert_eq!(usage.contract_id(), cost.contract);
            assert_eq!(usage.operation_id(), cost.operation);

            for row in [
                SAVED_TURN_SETTLEMENT_ROW,
                SAVED_ORCHESTRATION_ROW,
                gateway_row,
                child_row,
            ] {
                let mut candidate = fixture.clone();
                candidate["profile"]["requested_edges"][row]["authority_mode"] =
                    json!("interactive_only");
                assert!(matches!(select(&candidate, role), Err(Inconsistent(_))));
                candidate["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
                assert!(select(&candidate, role).unwrap().is_none());
                if row == gateway_row || row == child_row {
                    assert!(select(&candidate, other).unwrap().is_some());
                }
            }

            let mut candidate = fixture.clone();
            candidate["profile"]["requested_edges"][child_row]["caller_function_id"] =
                json!(GATEWAY_QUOTE_CALLER);
            candidate["plan"]["bindings"][child_row]["caller_function_id"] =
                json!(GATEWAY_QUOTE_CALLER);
            assert!(select(&candidate, role).unwrap().is_none());
            assert!(select(&candidate, other).unwrap().is_some());
        }
        let mut unrelated = fixture.clone();
        for row in (GATEWAY_QUOTE_ROW..=STREAM_RESULT_ROW).rev() {
            unrelated["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            unrelated["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
        }
        assert!(select(&unrelated, Generate).unwrap().is_some());
        assert!(select(&unrelated, Stream).unwrap().is_some());
    }

    #[test]
    fn stream_result_uses_only_the_exact_ordinary_gateway_stream_chain() {
        let fixture = records();
        let [turn, orchestration, gateway, normalizer] =
            select_stream_result(&fixture).unwrap().unwrap();
        assert_eq!(turn.caller_function_id(), TURN_SHELL_CALLER);
        assert_eq!(turn.function_id(), SAVED_TURN.function);
        assert_eq!(orchestration.caller_function_id(), turn.function_id());
        assert_eq!(orchestration.function_id(), SAVED_ORCHESTRATION.function);
        assert_eq!(gateway.caller_function_id(), orchestration.function_id());
        assert_eq!(gateway.function_id(), GATEWAY_STREAM_CALLER);
        assert_eq!(normalizer.caller_function_id(), gateway.function_id());
        assert_eq!(normalizer.function_id(), AI_STREAM_NORMALIZE.function);
        assert_eq!(normalizer.contract_id(), AI_STREAM_NORMALIZE.contract);
        assert_eq!(normalizer.operation_id(), AI_STREAM_NORMALIZE.operation);

        for row in [
            SAVED_TURN_SETTLEMENT_ROW,
            SAVED_ORCHESTRATION_ROW,
            LOCAL_CHAT_STREAM_ROWS[0],
            STREAM_RESULT_ROW,
        ] {
            let mut candidate = fixture.clone();
            candidate["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            assert!(matches!(
                select_stream_result(&candidate),
                Err(Inconsistent(_))
            ));
            candidate["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            assert!(select_stream_result(&candidate).unwrap().is_none());
        }

        let mut candidate = fixture.clone();
        for row in AI_ROUTING_ROWS.rev() {
            candidate["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            candidate["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
        }
        assert!(select_stream_result(&candidate).unwrap().is_some());
        assert!(
            select_ai_routing(&candidate)
                .unwrap()
                .iter()
                .all(Option::is_none)
        );

        let mut candidate = fixture.clone();
        candidate["profile"]["requested_edges"][STREAM_RESULT_ROW]["caller_function_id"] =
            json!(GATEWAY_GENERATE_CALLER);
        candidate["plan"]["bindings"][STREAM_RESULT_ROW]["caller_function_id"] =
            json!(GATEWAY_GENERATE_CALLER);
        assert!(select_stream_result(&candidate).unwrap().is_none());
        assert!(
            select_ai_routing(&candidate)
                .unwrap()
                .iter()
                .all(Option::is_some)
        );
    }

    #[test]
    fn ai_routing_selects_only_the_exact_ordinary_gateway_chain_for_each_role() {
        let mut fixture = records();
        for row in [LOCAL_CHAT_STREAM_ROWS, LOCAL_CHAT_GENERATE_ROWS]
            .into_iter()
            .flat_map(|[_, adapter, registry]| [registry, adapter])
        {
            fixture["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
        }
        // Provider Adapter and Provider Registry grants are separate from Routing.
        let roles = select_ai_routing(&fixture).unwrap();
        let mut references = std::collections::BTreeSet::new();
        for (role, routing_op) in [
            (
                OriginalModelResolveRole::QuoteGenerate,
                AI_ROUTING_GENERATE.operation,
            ),
            (
                OriginalModelResolveRole::QuoteStream,
                AI_ROUTING_STREAM.operation,
            ),
            (
                OriginalModelResolveRole::Generate,
                AI_ROUTING_GENERATE.operation,
            ),
            (
                OriginalModelResolveRole::Stream,
                AI_ROUTING_STREAM.operation,
            ),
        ] {
            let [turn, orchestration, gateway, routing] = roles[role.index()].as_ref().unwrap();
            assert_eq!(turn.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(turn.function_id(), SAVED_TURN.function);
            assert_eq!(orchestration.caller_function_id(), turn.function_id());
            assert_eq!(orchestration.function_id(), SAVED_ORCHESTRATION.function);
            assert_eq!(gateway.caller_function_id(), orchestration.function_id());
            assert_eq!(gateway.operation_id(), role.gateway_route().operation);
            assert_eq!(routing.caller_function_id(), gateway.function_id());
            assert_eq!(routing.function_id(), AI_ROUTING_GENERATE.function);
            assert_eq!(routing.contract_id(), AI_ROUTING_GENERATE.contract);
            assert_eq!(routing.operation_id(), routing_op);
            assert!(references.insert(routing.authority_reference()));
        }
    }

    #[test]
    fn ai_routing_denies_interactive_grants_and_detects_inconsistent_records() {
        let fixture = records();
        for (row, denied) in [
            (SAVED_TURN_SETTLEMENT_ROW, [true, true, true, true]),
            (SAVED_ORCHESTRATION_ROW, [true, true, true, true]),
            (GATEWAY_QUOTE_ROW, [true, true, false, false]),
        ]
        .into_iter()
        .chain(
            AI_ROUTING_ROWS
                .enumerate()
                .map(|(role_index, row)| (row, std::array::from_fn(|index| index == role_index))),
        ) {
            let mut candidate = fixture.clone();
            candidate["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            assert!(matches!(
                select_ai_routing(&candidate),
                Err(Inconsistent(_))
            ));
            candidate["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            assert_eq!(
                select_ai_routing(&candidate)
                    .unwrap()
                    .map(|pins| pins.is_none()),
                denied
            );
        }
    }

    #[test]
    fn saved_local_chat_selects_exact_grants_for_each_role() {
        use OriginalSavedLocalChatRole::{Generate, Stream};

        let fixture = records();
        for (role, [gateway, adapter, registry]) in [
            (Generate, LOCAL_CHAT_GENERATE_ROWS),
            (Stream, LOCAL_CHAT_STREAM_ROWS),
        ] {
            let pins = select_saved_local_chat(&fixture, role).unwrap().unwrap();
            let rows = [
                SAVED_TURN_SETTLEMENT_ROW,
                SAVED_ORCHESTRATION_ROW,
                gateway,
                adapter,
                registry,
            ];
            let mut caller = TURN_SHELL_CALLER;
            for (pin, row) in pins.iter().zip(rows) {
                let binding = &fixture["plan"]["bindings"][row];
                assert_eq!(pin.caller_function_id(), caller);
                assert_eq!(
                    pin.function_id(),
                    binding["function_principal"]["function_id"]
                );
                assert_eq!(pin.contract_id(), binding["contract_id"]);
                assert_eq!(pin.operation_id(), binding["operation_id"]);
                assert_eq!(pin.variant_id(), binding["variant_id"]);
                assert_eq!(pin.authority_reference(), binding["authority_reference"]);
                assert_eq!(
                    pin.requested_scope_digest(),
                    binding["requested_scope_digest"]
                );
                caller = pin.function_id();
            }
        }
    }

    #[test]
    fn saved_local_chat_requires_every_ordinary_grant_without_role_substitution() {
        use OriginalSavedLocalChatRole::{Generate, Stream};

        let fixture = records();
        for row in [SAVED_TURN_SETTLEMENT_ROW, SAVED_ORCHESTRATION_ROW] {
            let mut candidate = fixture.clone();
            candidate["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            candidate["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            for role in [Generate, Stream] {
                assert!(select_saved_local_chat(&candidate, role).unwrap().is_none());
            }
        }

        for (role, other, rows) in [
            (Generate, Stream, LOCAL_CHAT_GENERATE_ROWS),
            (Stream, Generate, LOCAL_CHAT_STREAM_ROWS),
        ] {
            for row in rows {
                let mut candidate = fixture.clone();
                candidate["profile"]["requested_edges"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                assert!(matches!(
                    select_saved_local_chat(&candidate, role),
                    Err(Inconsistent(_))
                ));
                candidate["plan"]["bindings"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                assert!(select_saved_local_chat(&candidate, role).unwrap().is_none());
                assert!(
                    select_saved_local_chat(&candidate, other)
                        .unwrap()
                        .is_some()
                );
            }
        }
    }

    #[test]
    fn saved_local_chat_rejects_cross_role_scope_with_matching_plan_digest() {
        use OriginalSavedLocalChatRole::{Generate, Stream};

        let fixture = records();
        for (generate_row, stream_row) in LOCAL_CHAT_GENERATE_ROWS
            .into_iter()
            .zip(LOCAL_CHAT_STREAM_ROWS)
        {
            let mut candidate = fixture.clone();
            let stream_scope =
                fixture["profile"]["requested_edges"][stream_row]["requested_scope_template"]
                    .clone();
            let stream_digest =
                fixture["plan"]["bindings"][stream_row]["requested_scope_digest"].clone();
            candidate["profile"]["requested_edges"][generate_row]["requested_scope_template"] =
                stream_scope;
            candidate["plan"]["bindings"][generate_row]["requested_scope_digest"] = stream_digest;
            assert!(
                select_saved_local_chat(&candidate, Generate)
                    .unwrap()
                    .is_none()
            );
            assert!(
                select_saved_local_chat(&candidate, Stream)
                    .unwrap()
                    .is_some()
            );
        }
    }

    fn select_saved_turn_start(records: &Value) -> Result<Option<[OriginalRoutePin; 2]>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_saved_turn_start_records((&profile, &lock, &plan), &budget)
    }

    #[test]
    fn saved_turn_start_requires_both_ordinary_profile_grants() {
        let fixture = records();
        let [turn, lifecycle] = select_saved_turn_start(&fixture).unwrap().unwrap();
        assert_eq!(turn.caller_function_id(), TURN_SHELL_CALLER);
        assert_eq!(turn.function_id(), SAVED_TURN.function);
        assert_eq!(lifecycle.caller_function_id(), turn.function_id());
        assert_eq!(lifecycle.function_id(), TURN_LIFECYCLE.function);
        assert_eq!(lifecycle.variant_id(), TURN_LIFECYCLE.variant);
        assert_eq!(lifecycle.artifact_digest(), turn.artifact_digest());

        for row in [SAVED_TURN_SETTLEMENT_ROW, TURN_LIFECYCLE_ROW] {
            let mut candidate = fixture.clone();
            candidate["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            candidate["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            assert!(select_saved_turn_start(&candidate).unwrap().is_none());

            let mut candidate = fixture.clone();
            candidate["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            candidate["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(select_saved_turn_start(&candidate).unwrap().is_none());
        }
    }

    #[test]
    fn saved_turn_start_rejects_other_callers_scopes_and_variants() {
        let fixture = records();
        let row = TURN_LIFECYCLE_ROW;
        for caller in [
            "rumi_turn_runtime_pack.chat-saved-job-adapter",
            "tobkiri_calendar_pack.calendar-turn",
            SAVED_RECONCILE_CALLER,
            MODEL_MANAGE.function,
        ] {
            let mut candidate = fixture.clone();
            candidate["profile"]["requested_edges"][row]["caller_function_id"] = json!(caller);
            candidate["plan"]["bindings"][row]["caller_function_id"] = json!(caller);
            assert!(select_saved_turn_start(&candidate).unwrap().is_none());
        }

        let mut candidate = fixture.clone();
        let scope = &mut candidate["profile"]["requested_edges"][row]["requested_scope_template"];
        scope["quotas"] = json!({"calls": 1});
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let wire = serde_json::to_vec(scope).unwrap();
        let digest = CoreValue::parse_guest(&wire, &budget)
            .unwrap()
            .digest_guest(&budget)
            .unwrap();
        candidate["plan"]["bindings"][row]["requested_scope_digest"] = json!(digest.as_str());
        assert!(select_saved_turn_start(&candidate).unwrap().is_none());

        for (field, alternate) in [
            (
                "variant_id",
                "rumi_turn_runtime_pack.turn-runtime.lifecycle.alt",
            ),
            ("platform", "linux"),
            ("architecture", "x86_64"),
            ("runtime_abi", "python3.12"),
            ("backend", "tobkiri.python-pack-v4"),
            ("execution_kind", "pack_vm"),
            ("domain_kind", "pack_vm"),
        ] {
            let mut candidate = fixture.clone();
            let pin = candidate["lock"]["variant_pins"]
                .as_array_mut()
                .unwrap()
                .iter_mut()
                .find(|pin| pin["variant_id"] == TURN_LIFECYCLE.variant)
                .unwrap();
            pin[field] = json!(alternate);
            candidate["plan"]["bindings"][row][field] = json!(alternate);
            assert!(
                select_saved_turn_start(&candidate).unwrap().is_none(),
                "{field}"
            );
        }
    }

    fn select_saved_turn_settlement(
        records: &Value,
        role: OriginalSavedTurnSettlementRole,
    ) -> Result<Option<OriginalRoutePin>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_saved_turn_settlement_records((&profile, &lock, &plan), &budget, role)
    }

    #[test]
    fn saved_turn_settlement_selects_each_shell_grant_independently() {
        use OriginalSavedTurnSettlementRole::{SavedReconcile, SavedTurn};

        let mut fixture = records();
        for (role, row) in [
            (SavedTurn, SAVED_TURN_SETTLEMENT_ROW),
            (SavedReconcile, SAVED_RECONCILE_SETTLEMENT_ROW),
        ] {
            let binding = &fixture["plan"]["bindings"][row];
            let pin = select_saved_turn_settlement(&fixture, role)
                .unwrap()
                .unwrap();
            assert_eq!(pin.caller_function_id(), "shell.tauri.default");
            assert_eq!(pin.pack_id(), "rumi_turn_runtime_pack");
            assert_eq!(pin.contract_id(), binding["contract_id"]);
            assert_eq!(pin.operation_id(), binding["operation_id"]);
            assert_eq!(pin.authority_reference(), binding["authority_reference"]);
            assert_eq!(
                pin.requested_scope_digest(),
                binding["requested_scope_digest"]
            );
        }

        for (role, row, other) in [
            (SavedReconcile, SAVED_RECONCILE_SETTLEMENT_ROW, SavedTurn),
            (SavedTurn, SAVED_TURN_SETTLEMENT_ROW, SavedReconcile),
        ] {
            fixture["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(matches!(
                select_saved_turn_settlement(&fixture, role),
                Err(Inconsistent(_))
            ));
            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(
                select_saved_turn_settlement(&fixture, role)
                    .unwrap()
                    .is_none()
            );
            if role == SavedReconcile {
                assert!(
                    select_saved_turn_settlement(&fixture, other)
                        .unwrap()
                        .is_some()
                );
            }
            assert!(
                select_saved_receipt(&fixture)
                    .unwrap()
                    .iter()
                    .all(Option::is_some)
            );
        }
    }

    #[test]
    fn saved_turn_settlement_requires_the_original_python_variant_and_role_authority() {
        use OriginalSavedTurnSettlementRole::{SavedReconcile, SavedTurn};

        let mut fixture = records();
        let row = SAVED_RECONCILE_SETTLEMENT_ROW;
        fixture["plan"]["bindings"][row]["variant_id"] = json!("replacement");
        let pins = fixture["lock"]["variant_pins"].as_array_mut().unwrap();
        let pin = pins
            .iter_mut()
            .find(|pin| pin["variant_id"] == SAVED_RECONCILE.variant)
            .unwrap();
        pin["variant_id"] = json!("replacement");
        assert!(
            select_saved_turn_settlement(&fixture, SavedReconcile)
                .unwrap()
                .is_none()
        );
        assert!(
            select_saved_turn_settlement(&fixture, SavedTurn)
                .unwrap()
                .is_some()
        );

        let mut fixture = records();
        let reference =
            fixture["plan"]["bindings"][CONVERSATION_SAVED_RECONCILE_ROW]["authority_reference"]
                .clone();
        fixture["profile"]["requested_edges"][row]["authority_reference"] = reference.clone();
        fixture["plan"]["bindings"][row]["authority_reference"] = reference;
        assert!(matches!(
            select_saved_turn_settlement(&fixture, SavedReconcile),
            Err(Inconsistent(_))
        ));
    }

    #[test]
    fn saved_receipt_selects_only_the_two_exact_turn_runtime_grants() {
        let mut fixture = records();
        let pins = select_saved_receipt(&fixture).unwrap();
        for role in OriginalSavedReceiptRole::ALL {
            let pin = pins[role.index()].as_ref().unwrap();
            assert_eq!(pin.caller_function_id(), role.caller_function_id());
            assert_eq!(pin.operation_id(), CONVERSATION_READ.operation);
        }
        for row in [
            CONVERSATION_SAVED_RECONCILE_ROW,
            CONVERSATION_SAVED_TURN_ROW,
        ] {
            fixture["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(matches!(
                select_saved_receipt(&fixture),
                Err(Inconsistent(_))
            ));
            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(select_conversation_read(&fixture).unwrap()[0].is_some());
            assert!(select_conversation_read(&fixture).unwrap()[2].is_some());
        }
        assert!(
            select_saved_receipt(&fixture)
                .unwrap()
                .iter()
                .all(Option::is_none)
        );
    }

    #[test]
    fn saved_receipt_rejects_an_interactive_mode_or_a_borrowed_grant() {
        let mut fixture = records();
        let row = CONVERSATION_SAVED_RECONCILE_ROW;
        fixture["profile"]["requested_edges"][row]["authority_mode"] = json!("interactive_only");
        fixture["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
        let [saved_turn, reconcile] = select_saved_receipt(&fixture).unwrap();
        assert!(saved_turn.is_some());
        assert!(reconcile.is_none());

        let mut fixture = records();
        let reference =
            fixture["plan"]["bindings"][CONVERSATION_SAVED_TURN_ROW]["authority_reference"].clone();
        fixture["profile"]["requested_edges"][row]["authority_reference"] = reference.clone();
        fixture["plan"]["bindings"][row]["authority_reference"] = reference;
        assert!(matches!(
            select_saved_receipt(&fixture),
            Err(Inconsistent(_))
        ));
    }

    #[test]
    fn saved_orchestration_proof_is_independent_of_message_manage() {
        let mut fixture = records();
        let [turn, orchestration, _] = select_append(&fixture).unwrap().unwrap();
        let expect_ordinary = |fixture: &Value| {
            let pins = select_saved_orchestration(fixture).unwrap().unwrap();
            for (pin, original) in pins.into_iter().zip([&turn, &orchestration]) {
                assert_eq!(pin.caller_function_id(), original.caller_function_id());
                assert_eq!(pin.function_id(), original.function_id());
                assert_eq!(pin.authority_reference(), original.authority_reference());
                assert_eq!(pin.artifact_digest(), original.artifact_digest());
                assert_eq!(
                    pin.requested_scope_digest(),
                    original.requested_scope_digest()
                );
            }
        };
        expect_ordinary(&fixture);

        fixture["plan"]["bindings"][MESSAGE_MANAGE_ROW]["authority_reference"] = json!("invalid");
        assert!(matches!(select_append(&fixture), Err(Inconsistent(_))));
        expect_ordinary(&fixture);

        fixture["profile"]["requested_edges"]
            .as_array_mut()
            .unwrap()
            .remove(MESSAGE_MANAGE_ROW);
        fixture["plan"]["bindings"]
            .as_array_mut()
            .unwrap()
            .remove(MESSAGE_MANAGE_ROW);
        assert!(select_append(&fixture).unwrap().is_none());
        expect_ordinary(&fixture);
    }

    #[test]
    fn saved_orchestration_proof_rejects_malformed_or_missing_grants() {
        for row in [SAVED_TURN_SETTLEMENT_ROW, SAVED_ORCHESTRATION_ROW] {
            let mut fixture = records();
            fixture["plan"]["bindings"][row]["requested_scope_digest"] = json!("malformed");
            assert!(matches!(
                select_saved_orchestration(&fixture),
                Err(Inconsistent(_))
            ));

            fixture["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(select_saved_orchestration(&fixture).unwrap().is_none());
        }
    }

    #[test]
    fn saved_orchestration_proof_rejects_calendar_caller() {
        let mut fixture = records();
        let calendar = json!("rumi_turn_runtime_pack.chat-saved-job-adapter");
        fixture["profile"]["requested_edges"][SAVED_ORCHESTRATION_ROW]["caller_function_id"] =
            calendar.clone();
        fixture["plan"]["bindings"][SAVED_ORCHESTRATION_ROW]["caller_function_id"] = calendar;
        assert!(select_saved_orchestration(&fixture).unwrap().is_none());
    }

    #[test]
    fn conversation_append_requires_each_original_grant_independently() {
        let fixture = records();
        let [turn, orchestration, manage] = select_append(&fixture).unwrap().unwrap();
        assert_eq!(turn.caller_function_id(), TURN_SHELL_CALLER);
        assert_eq!(turn.function_id(), SAVED_TURN_CALLER);
        assert_eq!(orchestration.caller_function_id(), turn.function_id());
        assert_eq!(orchestration.variant_id(), SAVED_ORCHESTRATION.variant);
        assert_eq!(orchestration.execution_kind(), "pack_vm");
        assert_eq!(orchestration.domain_kind(), "pack_vm");
        assert_eq!(manage.caller_function_id(), orchestration.function_id());
        assert_eq!(manage.variant_id(), CONVERSATION_MESSAGE_MANAGE.variant);
        for row in [
            SAVED_TURN_SETTLEMENT_ROW,
            SAVED_ORCHESTRATION_ROW,
            MESSAGE_MANAGE_ROW,
        ] {
            let mut fixture = fixture.clone();
            let edges = fixture["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap();
            edges[row].as_object_mut().unwrap().remove("authority_mode");
            assert!(select_append(&fixture).unwrap().is_some());

            fixture["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            fixture["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            assert!(select_append(&fixture).unwrap().is_none());

            fixture["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(matches!(select_append(&fixture), Err(Inconsistent(_))));
            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(select_append(&fixture).unwrap().is_none());
            assert!(
                select_saved_receipt(&fixture)
                    .unwrap()
                    .iter()
                    .all(Option::is_some)
            );
            assert!(
                select_confirmation(&fixture, OriginalSavedReceiptRole::SavedReconcile)
                    .unwrap()
                    .is_some()
            );
        }
    }

    #[test]
    fn conversation_append_rejects_other_runtimes_and_callers() {
        for (field, alternate) in [
            (
                "variant_id",
                "tobkiri_conversation_orchestration_pack.saved.alternate",
            ),
            ("backend", "tobkiri.python-host-v4"),
            ("execution_kind", "host_extension"),
            ("domain_kind", "dedicated_process"),
        ] {
            let mut fixture = records();
            let pin = fixture["lock"]["variant_pins"]
                .as_array_mut()
                .unwrap()
                .iter_mut()
                .find(|pin| pin["variant_id"] == SAVED_ORCHESTRATION.variant)
                .unwrap();
            pin[field] = json!(alternate);
            fixture["plan"]["bindings"][SAVED_ORCHESTRATION_ROW][field] = json!(alternate);
            assert!(
                select_saved_orchestration(&fixture).unwrap().is_none(),
                "{field}"
            );
            assert!(select_append(&fixture).unwrap().is_none(), "{field}");
        }

        let mut fixture = records();
        fixture["profile"]["requested_edges"][MESSAGE_MANAGE_ROW]["caller_function_id"] =
            json!(SAVED_RECONCILE_CALLER);
        fixture["plan"]["bindings"][MESSAGE_MANAGE_ROW]["caller_function_id"] =
            json!(SAVED_RECONCILE_CALLER);
        assert!(select_append(&fixture).unwrap().is_none());
    }

    #[test]
    fn conversation_confirmation_requires_all_three_exact_signed_grants() {
        use OriginalSavedReceiptRole::{SavedReconcile, SavedTurn};
        for role in OriginalSavedReceiptRole::ALL {
            let [turn, completion, get] = select_confirmation(&records(), role).unwrap().unwrap();
            assert_eq!(turn.function_id(), role.caller_function_id());
            assert_eq!(turn.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(completion.caller_function_id(), turn.function_id());
            assert_eq!(completion.variant_id(), CONVERSATION_CONFIRMATION.variant);
            assert_eq!(get.caller_function_id(), completion.function_id());
            assert_eq!(get.operation_id(), TURN_GET.operation);
        }
        for (row, denied) in [
            (SAVED_TURN_SETTLEMENT_ROW, [true, false]),
            (SAVED_RECONCILE_SETTLEMENT_ROW, [false, true]),
            (SAVED_TURN_CONFIRMATION_ROW, [true, false]),
            (SAVED_RECONCILE_CONFIRMATION_ROW, [false, true]),
            (CONFIRMATION_TURN_GET_ROW, [true, true]),
        ] {
            let mut fixture = records();
            fixture["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(matches!(
                select_confirmation(&fixture, if denied[0] { SavedTurn } else { SavedReconcile }),
                Err(Inconsistent(_))
            ));
            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            for (role, expected) in OriginalSavedReceiptRole::ALL.into_iter().zip(denied) {
                assert_eq!(
                    select_confirmation(&fixture, role).unwrap().is_none(),
                    expected
                );
            }
            assert!(
                select_saved_receipt(&fixture)
                    .unwrap()
                    .iter()
                    .all(Option::is_some)
            );
        }
    }

    #[test]
    fn conversation_confirmation_rejects_borrowed_interactive_or_other_variant_grants() {
        use OriginalSavedReceiptRole::{SavedReconcile, SavedTurn};
        let mut fixture = records();
        fixture["plan"]["bindings"][CONFIRMATION_TURN_GET_ROW]["authority_mode"] =
            json!("interactive_only");
        fixture["profile"]["requested_edges"][CONFIRMATION_TURN_GET_ROW]["authority_mode"] =
            json!("interactive_only");
        for role in OriginalSavedReceiptRole::ALL {
            assert!(select_confirmation(&fixture, role).unwrap().is_none());
        }

        let mut fixture = records();
        let reference =
            fixture["plan"]["bindings"][SAVED_TURN_CONFIRMATION_ROW]["authority_reference"].clone();
        fixture["plan"]["bindings"][SAVED_RECONCILE_CONFIRMATION_ROW]["authority_reference"] =
            reference.clone();
        fixture["profile"]["requested_edges"][SAVED_RECONCILE_CONFIRMATION_ROW]["authority_reference"] =
            reference;
        assert!(matches!(
            select_confirmation(&fixture, SavedReconcile),
            Err(Inconsistent(_))
        ));
        assert!(select_confirmation(&fixture, SavedTurn).unwrap().is_some());

        let mut fixture = records();
        let alternate = "rumi_conversation_store_pack.conversation-store.completion.alternate";
        fixture["plan"]["bindings"][SAVED_TURN_CONFIRMATION_ROW]["variant_id"] = json!(alternate);
        fixture["plan"]["bindings"][SAVED_RECONCILE_CONFIRMATION_ROW]["variant_id"] =
            json!(alternate);
        let lock = fixture["lock"]["variant_pins"].as_array_mut().unwrap();
        let variant = lock
            .iter_mut()
            .find(|pin| pin["variant_id"] == CONVERSATION_CONFIRMATION.variant)
            .unwrap();
        variant["variant_id"] = json!(alternate);
        for role in OriginalSavedReceiptRole::ALL {
            assert!(select_confirmation(&fixture, role).unwrap().is_none());
        }
    }

    #[test]
    fn model_resolve_requires_each_exact_caller_and_child_operation() {
        for (role_index, row) in MODEL_RESOLVE_ROWS.enumerate() {
            let mut fixture = records();
            let plan_binding = fixture["plan"]["bindings"][row].clone();
            let pins = select_model_resolve(&fixture).unwrap();
            let pin = pins[role_index].as_ref().unwrap();
            assert_eq!(pin.caller_function_id(), plan_binding["caller_function_id"]);
            assert_eq!(pin.operation_id(), plan_binding["operation_id"]);
            assert_eq!(
                pin.authority_reference(),
                plan_binding["authority_reference"]
            );
            assert_eq!(
                pin.requested_scope_digest(),
                plan_binding["requested_scope_digest"]
            );

            fixture["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(matches!(
                select_model_resolve(&fixture),
                Err(Inconsistent(_))
            ));

            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            for (other_index, pin) in select_model_resolve(&fixture)
                .unwrap()
                .into_iter()
                .enumerate()
            {
                assert_eq!(pin.is_some(), other_index != role_index);
            }
        }

        let mut fixture = records();
        fixture["profile"]["requested_edges"]
            .as_array_mut()
            .unwrap()
            .drain(MODEL_RESOLVE_ROWS);
        fixture["plan"]["bindings"]
            .as_array_mut()
            .unwrap()
            .drain(MODEL_RESOLVE_ROWS);
        assert!(
            select_model_resolve(&fixture)
                .unwrap()
                .iter()
                .all(Option::is_none)
        );
        assert!(select_model_list(&fixture).unwrap().is_some());
        assert!(select_model_save(&fixture).unwrap().is_some());
    }

    #[test]
    fn model_resolve_checks_signed_scope_and_authority_for_both_child_operations() {
        for (role_index, row) in MODEL_RESOLVE_ROWS.enumerate() {
            let mut fixture = records();
            // Two callers share each operation scope but have separate authority references.
            let other_caller_row = if role_index < 2 { row + 2 } else { row - 2 };
            let wrong_reference =
                fixture["plan"]["bindings"][other_caller_row]["authority_reference"].clone();
            fixture["plan"]["bindings"][row]["authority_reference"] = wrong_reference.clone();
            fixture["profile"]["requested_edges"][row]["authority_reference"] = wrong_reference;
            assert!(matches!(
                select_model_resolve(&fixture),
                Err(Inconsistent(_))
            ));

            let mut fixture = records();
            let scope = &mut fixture["profile"]["requested_edges"][row]["requested_scope_template"];
            scope["dimensions"]["operation"] = json!([
                MODEL_RESOLVE_GENERATE.operation,
                MODEL_RESOLVE_STREAM.operation
            ]);
            let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
            let scope_digest = CoreValue::parse_guest(&serde_json::to_vec(scope).unwrap(), &budget)
                .unwrap()
                .digest_guest(&budget)
                .unwrap();
            assert!(matches!(
                select_model_resolve(&fixture),
                Err(Inconsistent(_))
            ));
            fixture["plan"]["bindings"][row]["requested_scope_digest"] =
                json!(scope_digest.as_str());
            for (other_index, pin) in select_model_resolve(&fixture)
                .unwrap()
                .into_iter()
                .enumerate()
            {
                assert_eq!(pin.is_some(), other_index != role_index);
            }
        }
    }

    #[test]
    fn conversation_read_selects_each_exact_caller_and_accepts_the_legacy_default_mode() {
        let mut fixture = records();
        let rows = [
            (CONVERSATION_SHELL_ROW, "shell.tauri.default"),
            (CONVERSATION_SAVED_TURN_ROW, SAVED_TURN_CALLER),
            (
                CONVERSATION_SAVED_ORCHESTRATION_ROW,
                SAVED_ORCHESTRATION_CALLER,
            ),
        ];
        for (row, _) in rows {
            fixture["profile"]["requested_edges"][row]
                .as_object_mut()
                .unwrap()
                .remove("authority_mode");
        }
        for (pin, (row, caller)) in select_conversation_read(&fixture)
            .unwrap()
            .into_iter()
            .zip(rows)
        {
            let pin = pin.unwrap();
            assert_eq!(pin.caller_function_id(), caller);
            assert_eq!(pin.function_id(), CONVERSATION_READ.function);
            assert_eq!(pin.contract_id(), CONVERSATION_READ.contract);
            assert_eq!(pin.operation_id(), CONVERSATION_READ.operation);
            assert_eq!(pin.variant_id(), CONVERSATION_READ.variant);
            assert_eq!(
                pin.authority_reference(),
                fixture["profile"]["requested_edges"][row]["authority_reference"]
            );
        }
    }

    #[test]
    fn conversation_read_needs_each_role_to_have_its_own_profile_grant() {
        let mut fixture = records();
        let row = CONVERSATION_SAVED_TURN_ROW;
        fixture["profile"]["requested_edges"][row]["authority_mode"] = json!("interactive_only");
        fixture["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
        let row = CONVERSATION_SAVED_ORCHESTRATION_ROW;
        fixture["profile"]["requested_edges"][row]["caller_function_id"] =
            json!("unrelated.reader");
        fixture["plan"]["bindings"][row]["caller_function_id"] = json!("unrelated.reader");
        let [shell, saved_turn, saved_orchestration] = select_conversation_read(&fixture).unwrap();
        assert!(shell.is_some());
        assert!(saved_turn.is_none());
        assert!(saved_orchestration.is_none());

        let mut fixture = records();
        fixture["plan"]["bindings"][CONVERSATION_SAVED_TURN_ROW]["authority_mode"] =
            json!("interactive_only");
        assert!(matches!(
            select_conversation_read(&fixture),
            Err(Inconsistent(
                "the Profile edge and Plan Authority modes differ"
            ))
        ));
    }

    #[test]
    fn selects_the_exact_model_save_grants_with_both_scopes_and_variants() {
        let fixture = records();
        let (manage, read) = select_model_save(&fixture).unwrap().unwrap();
        assert_eq!(manage.caller_function_id(), "shell.tauri.default");
        assert_eq!(
            manage.function_id(),
            "rumi_model_registry_pack.model-registry.manage"
        );
        assert_eq!(read.caller_function_id(), manage.function_id());
        assert_eq!(
            read.function_id(),
            "rumi_provider_registry_pack.provider-registry.resource"
        );
        for (row, pin) in [(MODEL_MANAGE_ROW, manage), (MODEL_PROVIDER_READ_ROW, read)] {
            let binding = &fixture["plan"]["bindings"][row];
            assert_eq!(pin.contract_id(), binding["contract_id"].as_str().unwrap());
            assert_eq!(
                pin.operation_id(),
                binding["operation_id"].as_str().unwrap()
            );
            assert_eq!(pin.variant_id(), binding["variant_id"].as_str().unwrap());
            assert_eq!(
                pin.requested_scope_digest(),
                binding["requested_scope_digest"].as_str().unwrap()
            );
            assert_eq!(
                pin.authority_reference(),
                binding["authority_reference"].as_str().unwrap()
            );
        }
    }

    #[test]
    fn model_save_requires_each_grant_even_when_other_settings_grants_are_present() {
        for row in [MODEL_MANAGE_ROW, MODEL_PROVIDER_READ_ROW] {
            let mut fixture = records();
            fixture["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(
                matches!(select_model_save(&fixture), Err(Inconsistent(_))),
                "row {row}"
            );

            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(select_model_save(&fixture).unwrap().is_none(), "row {row}");
            assert!(select_model_list(&fixture).unwrap().is_some());

            let mut fixture = records();
            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(
                matches!(select_model_save(&fixture), Err(Inconsistent(_))),
                "row {row}"
            );
        }
    }

    #[test]
    fn model_save_rejects_drift_in_each_grant_scope_authority_and_pin() {
        for row in [MODEL_MANAGE_ROW, MODEL_PROVIDER_READ_ROW] {
            for field in [
                "requested_scope_digest",
                "authority_reference",
                "variant_id",
            ] {
                let mut fixture = records();
                fixture["plan"]["bindings"][row][field] = json!("changed");
                assert!(
                    matches!(select_model_save(&fixture), Err(Inconsistent(_))),
                    "row {row} {field}"
                );
            }

            let mut fixture = records();
            fixture["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            assert!(
                matches!(select_model_save(&fixture), Err(Inconsistent(_))),
                "row {row}"
            );
            fixture["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            assert!(select_model_save(&fixture).unwrap().is_none(), "row {row}");
        }
    }

    #[test]
    fn selects_only_the_shell_model_list_grant_with_exact_scope_and_variant() {
        let fixture = records();
        let model_list = select_model_list(&fixture).unwrap().unwrap();
        assert_eq!(model_list.caller_function_id(), "shell.tauri.default");
        assert_eq!(
            model_list.requested_scope_digest(),
            fixture["plan"]["bindings"][2]["requested_scope_digest"]
                .as_str()
                .unwrap()
        );

        let mut other_caller = fixture.clone();
        other_caller["profile"]["requested_edges"][2]["caller_function_id"] =
            json!(COORDINATOR.function);
        other_caller["plan"]["bindings"][2]["caller_function_id"] = json!(COORDINATOR.function);
        assert!(select_model_list(&other_caller).unwrap().is_none());

        let mut interactive = fixture.clone();
        interactive["profile"]["requested_edges"][2]["authority_mode"] = json!("interactive_only");
        assert!(matches!(
            select_model_list(&interactive),
            Err(Inconsistent(_))
        ));
        interactive["plan"]["bindings"][2]["authority_mode"] = json!("interactive_only");
        assert!(select_model_list(&interactive).unwrap().is_none());

        let mut other_variant = fixture.clone();
        other_variant["plan"]["bindings"][2]["variant_id"] = json!("other.model.profile");
        assert!(matches!(
            select_model_list(&other_variant),
            Err(Inconsistent(_))
        ));
        other_variant["lock"]["variant_pins"][2]["variant_id"] = json!("other.model.profile");
        assert!(select_model_list(&other_variant).unwrap().is_none());

        let mut other_scope = fixture;
        let scope = &mut other_scope["profile"]["requested_edges"][2]["requested_scope_template"];
        scope["dimensions"]["operation"] =
            json!(["rumi_model_registry_pack.model-profile-resource.generate"]);
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let digest = CoreValue::parse_guest(&serde_json::to_vec(scope).unwrap(), &budget)
            .unwrap()
            .digest_guest(&budget)
            .unwrap();
        assert!(matches!(
            select_model_list(&other_scope),
            Err(Inconsistent(_))
        ));
        other_scope["plan"]["bindings"][2]["requested_scope_digest"] = json!(digest.as_str());
        assert!(select_model_list(&other_scope).unwrap().is_none());
    }

    #[test]
    fn selects_the_old_keyless_prepare_from_the_two_current_grants() {
        let (coordinator, provider) = select(&records()).unwrap().unwrap();
        assert_eq!(coordinator.caller_function_id(), "shell.tauri.default");
        assert_eq!(provider.caller_function_id(), coordinator.function_id());
        assert_eq!(
            provider.operation_id(),
            "rumi_provider_registry_pack.provider-configure-prepare"
        );
        assert_eq!(provider.variant_id(), PROVIDER_PREPARE.variant);
        assert_eq!(
            provider.artifact_digest(),
            Digest::hash(PROVIDER_PREPARE.pack.as_bytes()).as_str()
        );
        assert_eq!(
            provider.executable_catalog_digest(),
            Digest::hash(PROVIDER_PREPARE.variant.as_bytes()).as_str()
        );
    }

    #[test]
    fn leaves_absent_or_unsupported_routes_to_the_original_owner() {
        const EXECUTE: &str = "rumi_provider_registry_pack.provider-configure";
        const EXECUTE_FUNCTION: &str = "rumi_provider_registry_pack.provider-configure.execute";
        let mut execute = records();
        let edge = &mut execute["profile"]["requested_edges"][1];
        edge["target_provider_id"] = json!(EXECUTE_FUNCTION);
        edge["operation_id"] = json!(EXECUTE);
        edge["authority_mode"] = json!("interactive_only");
        let binding = &mut execute["plan"]["bindings"][1];
        binding["operation_id"] = json!(EXECUTE);
        binding["function_principal"]["function_id"] = json!(EXECUTE_FUNCTION);
        binding["authority_mode"] = json!("interactive_only");
        assert!(select(&execute).unwrap().is_none());

        let mut alternative = records();
        alternative["plan"]["bindings"][1]["variant_id"] = json!("other.provider.prepare");
        alternative["lock"]["variant_pins"][1]["variant_id"] = json!("other.provider.prepare");
        assert!(select(&alternative).unwrap().is_none());
    }

    #[test]
    fn rejects_ambiguous_or_inconsistent_selected_records() {
        for (record, field) in [
            ("profile", "requested_edges"),
            ("profile", "authority_references"),
            ("lock", "variant_pins"),
            ("plan", "bindings"),
        ] {
            let mut fixture = records();
            let rows = fixture[record][field].as_array_mut().unwrap();
            rows.push(rows[1].clone());
            assert!(matches!(select(&fixture), Err(Inconsistent(_))), "{field}");
        }

        for field in PIN_FIELDS {
            let mut fixture = records();
            fixture["lock"]["variant_pins"][1][field] = json!("changed");
            assert!(matches!(select(&fixture), Err(Inconsistent(_))), "{field}");
        }

        let mut fixture = records();
        fixture["plan"]["bindings"]
            .as_array_mut()
            .unwrap()
            .remove(1);
        assert!(matches!(select(&fixture), Err(Inconsistent(_))));

        let mut fixture = records();
        fixture["plan"]["bindings"][1]["authority_mode"] = json!("interactive_only");
        assert!(matches!(select(&fixture), Err(Inconsistent(_))));

        let mut fixture = records();
        fixture["plan"]["bindings"][1]["function_principal"]["parent_artifact_digest"] =
            json!(Digest::hash(b"other artifact").as_str());
        assert!(matches!(select(&fixture), Err(Inconsistent(_))));
    }

    #[test]
    fn rejects_inconsistent_authority_and_skips_an_unsupported_legacy_route() {
        let mut rebound = records();
        let reference =
            json!("authority-ref:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
        rebound["profile"]["authority_references"][1] = reference.clone();
        rebound["profile"]["requested_edges"][1]["authority_reference"] = reference.clone();
        rebound["plan"]["bindings"][1]["authority_reference"] = reference;
        assert!(matches!(select(&rebound), Err(Inconsistent(_))));

        let mut adapted = records();
        adapted["plan"]["bindings"][1]["adapter_digests"] =
            json!([Digest::hash(b"adapter").as_str()]);
        assert!(select(&adapted).unwrap().is_none());

        let mut platform = records();
        platform["plan"]["bindings"][1]["platform"] = json!("macos");
        platform["lock"]["variant_pins"][1]["platform"] = json!("macos");
        assert!(select(&platform).unwrap().is_none());

        let mut widened = records();
        widened["profile"]["requested_edges"][1]["requested_scope_template"]["dimensions"]["workspace"] =
            json!(["other.workspace"]);
        assert!(matches!(select(&widened), Err(Inconsistent(_))));
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let authored_scope = &widened["profile"]["requested_edges"][1]["requested_scope_template"];
        let encoded = serde_json::to_vec(authored_scope).unwrap();
        let digest = CoreValue::parse_guest(&encoded, &budget)
            .unwrap()
            .digest_guest(&budget)
            .unwrap();
        widened["plan"]["bindings"][1]["requested_scope_digest"] = json!(digest.as_str());
        assert!(select(&widened).unwrap().is_none());

        let mut inconsistent = records();
        inconsistent["plan"]["bindings"][1]["requested_scope_digest"] =
            json!(Digest::hash(b"other scope").as_str());
        assert!(matches!(select(&inconsistent), Err(Inconsistent(_))));
    }
}
