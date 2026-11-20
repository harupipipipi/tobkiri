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

const REVIEWER_POLICY_CALLER: &str = "rumi_tool_local_executor_pack.tool-executor.local";
static REVIEWER_POLICY_ANCHOR: Route = Route {
    function: "rumi_host_authority_bridge_pack.host-authority.approval-policy",
    contract: "tobkiri.action.host.approval-policy.v1",
    operation: "host.action_approval_policy.select",
    variant: "rumi_host_authority_bridge_pack.host-authority.approval-policy.python",
    ..COORDINATOR
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

static APPLICATION_PRESENTATION: Route = Route {
    pack: "defaultspack",
    function: "defaultspack.application-presentation",
    contract: "tobkiri.resource.application.presentation.v1",
    operation: "defaultspack.presentation.read",
    variant: "defaultspack.application-presentation.python",
    execution: PYTHON_PACK_VM,
};

static CONVERSATION_COMPLETE: Route = Route {
    pack: "defaultspack",
    function: "defaultspack.conversation",
    contract: "conversation.turn.v1",
    operation: "complete",
    variant: "defaultspack.conversation.python",
    execution: PYTHON_PACK_VM,
};

const CONVERSATION_COMPLETE_REVISION: &str =
    "sha256:4ab185713b507f4cece1a3fadadcd9b8ed0918f66570d9d6d7b3b22f0ebca59a";
const CONVERSATION_GENERATE_REVISION: &str =
    "sha256:39eddd13b6cfae773c5f2e31705f8f073b42e54d0160f49918f87b8eeb63d768";

/// Application runtime Pack selected by the ordinary desktop Shell.
pub const CONVERSATION_COMPLETE_APPLICATION_ID: &str = "runtime.tauri.application.default";

static SETTINGS_READ: Route = Route {
    pack: "tobkiri_ui_settings_pack",
    function: "tobkiri.ui.settings.read",
    contract: "tobkiri.resource.ui.settings.v1",
    operation: "tobkiri_ui_settings_pack.settings-read",
    variant: "tobkiri.ui.settings.read.python",
    execution: PYTHON_HOST,
};

static CATALOG_READ: Route = Route {
    function: "tobkiri.ui.catalog.read",
    operation: "tobkiri_ui_settings_pack.catalog-read",
    variant: "tobkiri.ui.catalog.read.python",
    ..SETTINGS_READ
};

static COMMAND_CATALOG_READ: Route = Route {
    pack: "rumi_command_protocol_pack",
    function: "rumi_command_protocol_pack.catalog.read",
    contract: "tobkiri.resource.command.catalog.v1",
    operation: "command.catalog.read",
    variant: "rumi_command_protocol_pack.catalog.read.python",
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

static MODEL_SEARCH: Route = Route {
    pack: "tobkiri_ui_settings_pack",
    function: "tobkiri.ui.model-search.read",
    contract: "tobkiri.resource.ui.model-search.v1",
    operation: "tobkiri_ui_settings_pack.model-search",
    variant: "tobkiri.ui.model-search.read.python",
    execution: PYTHON_HOST,
};

static COMMAND_DATASOURCE: Route = Route {
    pack: "rumi_command_protocol_pack",
    function: "rumi_command_protocol_pack.command.datasource",
    contract: "tobkiri.resource.command.datasource.v1",
    operation: "command.datasource.query",
    variant: "rumi_command_protocol_pack.command.datasource.python",
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

static PROVIDER_HEALTH_GENERATE: Route = Route {
    function: "rumi_provider_registry_pack.provider-registry.health",
    contract: "tobkiri.resource.ai.provider.health.v1",
    operation: "rumi_provider_registry_pack.provider-registry-health.generate",
    variant: "rumi_provider_registry_pack.provider-registry.health.python",
    ..PROVIDER_READ
};

static PROVIDER_HEALTH_STREAM: Route = Route {
    operation: "rumi_provider_registry_pack.provider-registry-health.stream",
    ..PROVIDER_HEALTH_GENERATE
};

static MODEL_ACCESS_READ: Route = Route {
    function: "rumi_provider_registry_pack.model-access.read",
    operation: "rumi_provider_registry_pack.model-access-read",
    variant: "rumi_provider_registry_pack.model-access.read.python",
    ..PROVIDER_READ
};

static MODEL_ACCESS_CATALOG: Route = Route {
    function: "rumi_provider_registry_pack.model-access.catalog",
    operation: "rumi_provider_registry_pack.model-access-catalog",
    variant: "rumi_provider_registry_pack.model-access.catalog.python",
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

static SIDE_CHAT_TURN: Route = Route {
    pack: "tobkiri_side_chat_pack",
    function: "tobkiri_side_chat_pack.turn",
    contract: "tobkiri.service.side-chat.turn.v1",
    operation: "tobkiri_side_chat_pack.side-chat-turn",
    variant: "tobkiri_side_chat_pack.turn.python",
    execution: PYTHON_HOST,
};

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

const SCHEDULER_CLOCK_CALLER: &str = "rumi_scheduler_runtime_pack.scheduler.clock";
static SCHEDULER_CONTROL: Route = Route {
    pack: "rumi_scheduler_runtime_pack",
    function: "rumi_scheduler_runtime_pack.scheduler.control",
    contract: "tobkiri.action.scheduler.v1",
    operation: "rumi_scheduler_runtime_pack.scheduler-control",
    variant: "rumi_scheduler_runtime_pack.scheduler.control.python",
    execution: PYTHON_HOST,
};
static JOB_ACTION_BROKER: Route = Route {
    pack: "rumi_job_action_broker_pack",
    function: "rumi_job_action_broker_pack.job-action.broker",
    contract: "tobkiri.action.job.v1",
    operation: "rumi_job_action_broker_pack.job-action-broker",
    variant: "rumi_job_action_broker_pack.job-action.broker.python",
    execution: PYTHON_HOST,
};
static CHAT_SAVED_JOB_ADAPTER: Route = Route {
    pack: TURN_RUNTIME_PACK,
    function: "rumi_turn_runtime_pack.chat-saved-job-adapter",
    contract: "tobkiri.action.job.adapter.v2",
    operation: "rumi_turn_runtime_pack.chat-saved-job-adapter",
    variant: "rumi_turn_runtime_pack.chat-saved-job-adapter.python",
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

static WORKFLOW_ADVANCE: Route = Route {
    pack: "tobkiri_workflow_pack",
    function: "tobkiri.workflow.provider",
    contract: "tobkiri.workflow.v4",
    operation: "run.advance",
    variant: "tobkiri.workflow.provider.python",
    execution: PYTHON_HOST,
};

static WORKFLOW_CANCEL: Route = Route {
    operation: "run.cancel",
    ..WORKFLOW_ADVANCE
};

static WORKFLOW_SELECTED: Route = Route {
    operation: "run.selected",
    ..WORKFLOW_ADVANCE
};

static WORKFLOW_STEP_EXECUTE: Route = Route {
    operation: "run.step.execute",
    ..WORKFLOW_ADVANCE
};

static WORKFLOW_STEP_RESUME: Route = Route {
    operation: "run.step.resume",
    ..WORKFLOW_ADVANCE
};

static WORKFLOW_STEP_RETRY: Route = Route {
    operation: "run.step.retry",
    ..WORKFLOW_ADVANCE
};

static GATEWAY_TEXT: Route = Route {
    pack: "rumi_ai_gateway_pack",
    function: "rumi_ai_gateway_pack.ai-gateway.text-generate",
    contract: "tobkiri.service.ai.text.generate.v1",
    operation: "rumi_ai_gateway_pack.ai-gateway.text-generate",
    variant: "rumi_ai_gateway_pack.ai-gateway.text-generate.python",
    execution: PYTHON_HOST,
};

static GATEWAY_MESSAGES: Route = Route {
    pack: "rumi_ai_gateway_pack",
    function: "rumi_ai_gateway_pack.ai-gateway.messages-generate",
    contract: "tobkiri.service.ai.messages.generate.v1",
    operation: "rumi_ai_gateway_pack.ai-gateway.messages-generate",
    variant: "rumi_ai_gateway_pack.ai-gateway.messages-generate.python",
    execution: PYTHON_HOST,
};

static MODALITY_TRANSCRIBE: Route = Route {
    pack: "rumi_ai_modality_pack",
    function: "rumi_ai_modality_pack.ai-modality.transcribe",
    contract: "tobkiri.service.ai.audio.transcribe.v1",
    operation: "rumi_ai_modality_pack.ai-transcribe",
    variant: "rumi_ai_modality_pack.ai-modality.transcribe.python",
    execution: PYTHON_HOST,
};

static MODALITY_SPEECH: Route = Route {
    pack: "rumi_ai_modality_pack",
    function: "rumi_ai_modality_pack.ai-modality.speech",
    contract: "tobkiri.service.ai.audio.speech.v1",
    operation: "rumi_ai_modality_pack.ai-speech",
    variant: "rumi_ai_modality_pack.ai-modality.speech.python",
    execution: PYTHON_HOST,
};

static ADAPTER_TRANSCRIBE: Route = Route {
    pack: "rumi_provider_adapters_pack",
    function: "rumi_provider_adapters_pack.provider.compatibility.audio_transcribe",
    contract: "tobkiri.service.ai.provider.audio.transcribe.v1",
    operation: "rumi_provider_adapters_pack.provider-audio-transcribe",
    variant: "rumi_provider_adapters_pack.provider.compatibility.audio_transcribe.python",
    execution: PYTHON_HOST,
};

static ADAPTER_SPEECH: Route = Route {
    pack: "rumi_provider_adapters_pack",
    function: "rumi_provider_adapters_pack.provider.compatibility.audio_speech",
    contract: "tobkiri.service.ai.provider.audio.speech.v1",
    operation: "rumi_provider_adapters_pack.provider-audio-speech",
    variant: "rumi_provider_adapters_pack.provider.compatibility.audio_speech.python",
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

static AI_TOOL_INTENTS_GENERATE: Route = Route {
    pack: "rumi_ai_tool_bridge_pack",
    function: "rumi_ai_tool_bridge_pack.ai-tool-bridge.normalize",
    contract: "tobkiri.service.ai.tool_intent.normalize.v1",
    operation: "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.generate",
    variant: "rumi_ai_tool_bridge_pack.ai-tool-bridge.normalize.python",
    execution: PYTHON_HOST,
};

static AI_TOOL_INTENTS_STREAM: Route = Route {
    operation: "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.stream",
    ..AI_TOOL_INTENTS_GENERATE
};

static AI_REQUEST_PREPARE_GENERATE: Route = Route {
    pack: "rumi_ai_pipeline_pack",
    function: "rumi_ai_pipeline_pack.ai-pipeline.prepare",
    contract: "tobkiri.service.ai.request.prepare.v1",
    operation: "rumi_ai_pipeline_pack.ai-request-prepare.generate",
    variant: "rumi_ai_pipeline_pack.ai-pipeline.prepare.python",
    execution: PYTHON_HOST,
};

static AI_REQUEST_PREPARE_STREAM: Route = Route {
    operation: "rumi_ai_pipeline_pack.ai-request-prepare.stream",
    ..AI_REQUEST_PREPARE_GENERATE
};

static AI_FAILOVER_GENERATE: Route = Route {
    pack: "rumi_ai_pipeline_pack",
    function: "rumi_ai_pipeline_pack.ai-pipeline.failover",
    contract: "tobkiri.service.ai.failover.decide.v1",
    operation: "rumi_ai_pipeline_pack.ai-failover-decide.generate",
    variant: "rumi_ai_pipeline_pack.ai-pipeline.failover.python",
    execution: PYTHON_HOST,
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

/// Original operation whose signed nested grant can read application presentation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ApplicationPresentationRole {
    /// UI settings read, including its complete settings view.
    Settings,
    /// UI catalog read.
    Catalog,
    /// Command catalog read.
    Commands,
}

impl ApplicationPresentationRole {
    /// Independent ordinary application presentation callers.
    pub const ALL: [Self; 3] = [Self::Settings, Self::Catalog, Self::Commands];

    fn caller(self) -> &'static Route {
        match self {
            Self::Settings => &SETTINGS_READ,
            Self::Catalog => &CATALOG_READ,
            Self::Commands => &COMMAND_CATALOG_READ,
        }
    }
}

/// Exact Shell-to-owner and owner-to-presentation grants in the active Profile.
pub struct OriginalApplicationPresentationChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 2],
}

impl<'a> OriginalApplicationPresentationChoice<'a> {
    /// Require both grants from the ordinary desktop Shell for this role.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: ApplicationPresentationRole,
    ) -> Result<Option<Self>> {
        Ok(
            select_application_presentation_records(active.records(), budget, role)?.map(|pins| {
                Self {
                    _active: active,
                    pins,
                }
            }),
        )
    }

    /// The caller and its nested presentation route, in chain order.
    pub fn pins(&self) -> &[OriginalRoutePin; 2] {
        &self.pins
    }
}

struct ConversationCompleteRoutes {
    pins: [OriginalRoutePin; 2],
    shell_artifacts: [CoreValue; 2],
    application_artifacts: [CoreValue; 2],
}

/// Exact ordinary desktop Shell-to-Conversation-to-Gateway Generate grants.
pub struct OriginalConversationCompleteChoice<'a> {
    _active: &'a OriginalActiveProfile,
    routes: ConversationCompleteRoutes,
}

impl<'a> OriginalConversationCompleteChoice<'a> {
    /// Select both grants and the Shell and Application artifact identities.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Ok(
            select_conversation_complete_records(active.records(), budget)?.map(|routes| Self {
                _active: active,
                routes,
            }),
        )
    }

    /// Selected Conversation and Gateway pins in call order.
    pub fn pins(&self) -> &[OriginalRoutePin; 2] {
        &self.routes.pins
    }

    /// Aggregate and executable digests for the selected Shell Pack.
    pub fn shell_artifacts(&self) -> [&str; 2] {
        self.routes.shell_artifacts.each_ref().map(verified_text)
    }

    /// Aggregate and executable digests for the selected Application Pack.
    pub fn application_artifacts(&self) -> [&str; 2] {
        self.routes
            .application_artifacts
            .each_ref()
            .map(verified_text)
    }
}

/// The signed Conversation Complete chain and its Gateway's Provider Health grant.
pub struct OriginalConversationCompleteHealthChoice<'a> {
    complete: OriginalConversationCompleteChoice<'a>,
    health: OriginalRoutePin,
}

impl<'a> OriginalConversationCompleteHealthChoice<'a> {
    /// Select the exact desktop Shell, Application, Conversation, Gateway, and Health grants.
    /// Verify all returned pins against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Ok(
            select_conversation_complete_health_records(active.records(), budget)?.map(
                |(routes, health)| Self {
                    complete: OriginalConversationCompleteChoice {
                        _active: active,
                        routes,
                    },
                    health,
                },
            ),
        )
    }

    /// The selected Complete grants and the ordinary Shell and Application identities.
    pub fn complete(&self) -> &OriginalConversationCompleteChoice<'a> {
        &self.complete
    }

    /// The Gateway Generate grant for the dedicated Provider Health operation.
    pub fn health(&self) -> &OriginalRoutePin {
        &self.health
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

/// Immutable metadata for the desktop Shell's direct Provider Registry read.
pub struct OriginalShellProviderSnapshotSelection<'a> {
    _active: &'a OriginalActiveProfile,
    provider_read: OriginalRoutePin,
}

impl<'a> OriginalShellProviderSnapshotSelection<'a> {
    /// Select the exact desktop Shell and its signed Registry operation.
    /// Verify the returned pin against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Ok(
            select_shell_provider_snapshot_records(active.records(), budget)?.map(
                |provider_read| Self {
                    _active: active,
                    provider_read,
                },
            ),
        )
    }

    /// The selected Shell-to-Provider Registry read grant target.
    pub fn provider_read(&self) -> &OriginalRoutePin {
        &self.provider_read
    }
}

/// Immutable metadata for each desktop Shell Model Access operation's own Profile grant.
pub struct OriginalModelAccessProviderSnapshotSelection<'a> {
    _active: &'a OriginalActiveProfile,
    read: Option<OriginalRoutePin>,
    catalog: Option<OriginalRoutePin>,
}

impl<'a> OriginalModelAccessProviderSnapshotSelection<'a> {
    /// Select the exact Model Access read and catalog grants independently.
    ///
    /// Verify each returned pin against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let [read, catalog] =
            select_model_access_provider_snapshot_records(active.records(), budget)?;
        if read.is_none() && catalog.is_none() {
            return Ok(None);
        }
        Ok(Some(Self {
            _active: active,
            read,
            catalog,
        }))
    }

    /// The Shell's grant for the Registry Model Access read operation.
    pub fn read(&self) -> Option<&OriginalRoutePin> {
        self.read.as_ref()
    }

    /// The Shell's grant for the Registry Model Access catalog operation.
    pub fn catalog(&self) -> Option<&OriginalRoutePin> {
        self.catalog.as_ref()
    }
}

/// Immutable metadata for the desktop Shell's query owners and their Registry grants.
pub struct OriginalQueryProviderSnapshotSelection<'a> {
    _active: &'a OriginalActiveProfile,
    model_search: Option<[OriginalRoutePin; 2]>,
    command_datasource: Option<[OriginalRoutePin; 2]>,
}

impl<'a> OriginalQueryProviderSnapshotSelection<'a> {
    /// Select each exact desktop Shell query and its owner's separate Registry grant.
    ///
    /// Verify both pins of each selected query against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let [model_search, command_datasource] =
            select_query_provider_snapshot_records(active.records(), budget)?;
        if model_search.is_none() && command_datasource.is_none() {
            return Ok(None);
        }
        Ok(Some(Self {
            _active: active,
            model_search,
            command_datasource,
        }))
    }

    /// The selected Model Search and its own Provider Registry read, in call order.
    pub fn model_search(&self) -> Option<&[OriginalRoutePin; 2]> {
        self.model_search.as_ref()
    }

    /// The selected Command Datasource and its own Provider Registry read, in call order.
    pub fn command_datasource(&self) -> Option<&[OriginalRoutePin; 2]> {
        self.command_datasource.as_ref()
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
    /// Saved Reconcile invoked directly by the selected Shell.
    SavedReconcile,
    /// Saved Turn invoked through the selected Shell's Side Chat service.
    SideChatTurn,
    /// Saved Reconcile invoked through the selected Shell's Side Chat service.
    SideChatReconcile,
}

impl OriginalSavedReceiptRole {
    /// Roles invoked directly by the selected Shell.
    pub const ALL: [Self; 2] = [Self::SavedTurn, Self::SavedReconcile];

    /// Roles invoked through the selected Shell's Side Chat service.
    pub const SIDE_CHAT_CALLERS: [Self; 2] = [Self::SideChatTurn, Self::SideChatReconcile];

    /// All roles supported by the private saved Conversation and Turn operations.
    pub const RECEIPT_CALLERS: [Self; 4] = [
        Self::SavedTurn,
        Self::SavedReconcile,
        Self::SideChatTurn,
        Self::SideChatReconcile,
    ];

    /// The function identity required on the original Profile grant.
    pub const fn caller_function_id(self) -> &'static str {
        match self {
            Self::SavedTurn | Self::SideChatTurn => SAVED_TURN_CALLER,
            Self::SavedReconcile | Self::SideChatReconcile => SAVED_RECONCILE_CALLER,
        }
    }

    const fn index(self) -> usize {
        match self {
            Self::SavedTurn => 0,
            Self::SavedReconcile => 1,
            Self::SideChatTurn => 2,
            Self::SideChatReconcile => 3,
        }
    }
}

/// The ordinary signed Conversation owner edges for the private saved receipt read.
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

    /// The grant from this exact ordinary Turn Runtime caller to the Conversation owner.
    pub fn pin(&self, role: OriginalSavedReceiptRole) -> Option<&OriginalRoutePin> {
        self.pins.get(role.index())?.as_ref()
    }
}

/// The signed Side Chat caller chain for the private saved receipt read.
pub struct OriginalSideChatSavedReceiptChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 3],
}

impl<'a> OriginalSideChatSavedReceiptChoice<'a> {
    /// Select the exact three grants for this Side Chat caller.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalSavedReceiptRole,
    ) -> Result<Option<Self>> {
        Ok(
            select_side_chat_saved_receipt_records(active.records(), budget, role)?.map(|pins| {
                Self {
                    _active: active,
                    pins,
                }
            }),
        )
    }

    /// The Shell-to-Side Chat, Side Chat-to-Turn, and Turn-to-Conversation grants.
    pub fn pins(&self) -> &[OriginalRoutePin; 3] {
        &self.pins
    }
}

/// The signed Shell, Turn Runtime, and Conversation owner chain for confirmation.
pub struct OriginalConversationConfirmationChoice<'a> {
    _active: &'a OriginalActiveProfile,
    side_chat: Option<OriginalRoutePin>,
    pins: [OriginalRoutePin; 3],
}

impl<'a> OriginalConversationConfirmationChoice<'a> {
    /// Select the exact Shell entry, completion grant, and nested Turn resource grant.
    /// Verify the selected pins against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalSavedReceiptRole,
    ) -> Result<Option<Self>> {
        Ok(
            select_conversation_confirmation_records(active.records(), budget, role)?.map(
                |(side_chat, pins)| Self {
                    _active: active,
                    side_chat,
                    pins,
                },
            ),
        )
    }

    /// The Shell-to-Side Chat grant for a Side Chat role.
    pub fn side_chat(&self) -> Option<&OriginalRoutePin> {
        self.side_chat.as_ref()
    }

    /// The original grant from the Shell or Side Chat to the selected Turn function.
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

/// The entry point behind a signed saved Turn start.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SavedTurnStartCaller {
    /// The selected Shell invokes Saved Turn directly.
    SavedTurn,
    /// The selected Shell invokes Saved Turn through the Side Chat service.
    SideChat,
}

/// The signed entry point behind a Saved Input request.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SavedInputCaller {
    /// The selected Shell invokes ordinary Saved Turn.
    SavedTurn,
    /// The Scheduler Clock wakes an automatic Calendar dispatch.
    Calendar,
}

/// The signed chain from the Scheduler Clock to Turn Lifecycle for automatic Calendar dispatch.
pub struct OriginalCalendarSavedInputChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 4],
}

impl<'a> OriginalCalendarSavedInputChoice<'a> {
    /// Select the four exact grants. Verify every pin against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Ok(
            select_calendar_saved_input_records(active.records(), budget)?.map(|pins| Self {
                _active: active,
                pins,
            }),
        )
    }

    /// The ordered Scheduler, Job Action Broker, saved job adapter, and Turn Lifecycle grants.
    pub fn pins(&self) -> &[OriginalRoutePin; 4] {
        &self.pins
    }
}

/// The signed chain from the selected Shell through Saved Turn to Turn Lifecycle.
pub struct OriginalSavedTurnStartChoice<'a> {
    _active: &'a OriginalActiveProfile,
    side_chat: Option<OriginalRoutePin>,
    pins: [OriginalRoutePin; 2],
}

impl<'a> OriginalSavedTurnStartChoice<'a> {
    /// Select the two exact ordinary saved grants. Verify both pins against the signed Bundle.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Self::select_for(active, budget, SavedTurnStartCaller::SavedTurn)
    }

    /// Select this caller's exact start grants. Verify every pin against the signed Bundle.
    pub fn select_for(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        caller: SavedTurnStartCaller,
    ) -> Result<Option<Self>> {
        let choice = select_saved_turn_start_records(active.records(), budget, caller)?;
        Ok(choice.map(|(side_chat, pins)| Self {
            _active: active,
            side_chat,
            pins,
        }))
    }

    /// The Shell-to-Side Chat grant for the Side Chat entry point.
    pub fn side_chat(&self) -> Option<&OriginalRoutePin> {
        self.side_chat.as_ref()
    }

    /// The grant into Saved Turn followed by the Saved Turn-to-Lifecycle grant.
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
    /// The saved generate chain entered through the Side Chat service.
    SideChatGenerate,
    /// The saved stream chain entered through the Side Chat service.
    SideChatStream,
}

impl OriginalSavedLocalChatRole {
    /// Whether the selected Gateway and Provider operations stream their results.
    pub fn is_stream(self) -> bool {
        matches!(self, Self::Stream | Self::SideChatStream)
    }

    /// Whether the selected Shell enters Saved Turn through Side Chat.
    pub fn is_side_chat(self) -> bool {
        matches!(self, Self::SideChatGenerate | Self::SideChatStream)
    }

    fn routes(self) -> [&'static Route; 3] {
        if self.is_stream() {
            [&GATEWAY_STREAM, &PROVIDER_STREAM, &PROVIDER_READ_STREAM]
        } else {
            [
                &GATEWAY_GENERATE,
                &PROVIDER_GENERATE,
                &PROVIDER_READ_GENERATE,
            ]
        }
    }
}

/// The signed grant chain for one saved local chat role.
pub struct OriginalSavedLocalChatChoice<'a> {
    _active: &'a OriginalActiveProfile,
    role: OriginalSavedLocalChatRole,
    side_chat: Option<OriginalRoutePin>,
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
            select_saved_local_chat_records(active.records(), budget, role)?.map(
                |(side_chat, pins)| Self {
                    _active: active,
                    role,
                    side_chat,
                    pins,
                },
            ),
        )
    }

    /// The saved local chat role attested by the selected grants.
    pub fn role(&self) -> OriginalSavedLocalChatRole {
        self.role
    }

    /// The Shell-to-Side Chat grant for a Side Chat role.
    pub fn side_chat(&self) -> Option<&OriginalRoutePin> {
        self.side_chat.as_ref()
    }

    /// The chain from the selected entry point through Saved Turn, Saved Orchestration, Gateway,
    /// and Provider Adapter to the original Provider Registry.
    pub fn pins(&self) -> &[OriginalRoutePin; 5] {
        &self.pins
    }
}

/// The entry point behind a signed saved message append.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConversationAppendCaller {
    /// The selected Shell invokes Saved Turn directly.
    SavedTurn,
    /// The selected Shell invokes Saved Turn through the Side Chat service.
    SideChat,
}

/// The signed chain from the selected Shell through Saved Orchestration to Message Manage.
pub struct OriginalConversationAppendChoice<'a> {
    _active: &'a OriginalActiveProfile,
    side_chat: Option<OriginalRoutePin>,
    pins: [OriginalRoutePin; 3],
}

impl<'a> OriginalConversationAppendChoice<'a> {
    /// Select the exact grants for an ordinary saved turn's message append.
    /// Verify every returned pin against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Self::select_for(active, budget, ConversationAppendCaller::SavedTurn)
    }

    /// Select the exact append grants for the given signed entry point.
    pub fn select_for(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        caller: ConversationAppendCaller,
    ) -> Result<Option<Self>> {
        let choice = select_conversation_append_records(active.records(), budget, caller)?;
        Ok(choice.map(|(side_chat, pins)| Self {
            _active: active,
            side_chat,
            pins,
        }))
    }

    /// The Shell-to-Side Chat grant for the Side Chat entry point.
    pub fn side_chat(&self) -> Option<&OriginalRoutePin> {
        self.side_chat.as_ref()
    }

    /// The grant into Saved Turn, followed by the Orchestration and Message Manage grants.
    pub fn pins(&self) -> &[OriginalRoutePin; 3] {
        &self.pins
    }
}

/// The original caller chain under which a Turn owner may settle a saved receipt.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalSavedTurnSettlementRole {
    /// The saved execution path after validating the owner result.
    SavedTurn,
    /// The explicit reconciliation path after validating the saved receipt.
    SavedReconcile,
    /// The saved execution path invoked through Side Chat.
    SideChatTurn,
    /// The explicit reconciliation path invoked through Side Chat.
    SideChatReconcile,
}

impl OriginalSavedTurnSettlementRole {
    /// The distinct original Turn Runtime grants used for settlement.
    pub const ALL: [Self; 4] = [
        Self::SavedTurn,
        Self::SavedReconcile,
        Self::SideChatTurn,
        Self::SideChatReconcile,
    ];

    fn route(self) -> &'static Route {
        match self {
            Self::SavedTurn | Self::SideChatTurn => &SAVED_TURN,
            Self::SavedReconcile | Self::SideChatReconcile => &SAVED_RECONCILE,
        }
    }

    fn via_side_chat(self) -> bool {
        matches!(self, Self::SideChatTurn | Self::SideChatReconcile)
    }
}

/// The exact Profile grants from the selected default Shell to the Turn owner.
pub struct OriginalSavedTurnSettlementChoice<'a> {
    _active: &'a OriginalActiveProfile,
    role: OriginalSavedTurnSettlementRole,
    side_chat: Option<OriginalRoutePin>,
    pin: OriginalRoutePin,
}

impl OriginalSavedTurnSettlementChoice<'_> {
    /// The settlement role selected by the original caller chain.
    pub fn role(&self) -> OriginalSavedTurnSettlementRole {
        self.role
    }

    /// The Shell-to-Side Chat grant for a Side Chat settlement role.
    pub fn side_chat(&self) -> Option<&OriginalRoutePin> {
        self.side_chat.as_ref()
    }

    /// The exact grant and Python execution pins for the Turn owner.
    pub fn pin(&self) -> &OriginalRoutePin {
        &self.pin
    }
}

/// Select one original settlement chain from the active Profile, Lock, and Plan.
///
/// An absent or unsupported grant yields `None`. The packaged catalog must
/// independently attest each returned pin and its privileged effect class.
pub fn select_original_saved_turn_settlement<'a>(
    active: &'a OriginalActiveProfile,
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalSavedTurnSettlementRole,
) -> Result<Option<OriginalSavedTurnSettlementChoice<'a>>> {
    Ok(
        select_saved_turn_settlement_records(active.records(), budget, role)?.map(
            |(side_chat, pin)| OriginalSavedTurnSettlementChoice {
                _active: active,
                role,
                side_chat,
                pin,
            },
        ),
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

    /// Position in `ALL` and in the verification results for each saved entry.
    pub const fn index(self) -> usize {
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

/// Gateway operations used by the independent approval reviewer.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalReviewerHealthRole {
    /// Route Quote for buffered generation.
    Quote,
    /// Generate without tools or failover.
    Generate,
}

impl OriginalReviewerHealthRole {
    /// Both independently authorized reviewer operations.
    pub const ALL: [Self; 2] = [Self::Quote, Self::Generate];

    /// Position in `ALL` and in the corresponding verification results.
    pub const fn index(self) -> usize {
        self as usize
    }

    const fn gateway_route(self) -> &'static Route {
        match self {
            Self::Quote => &GATEWAY_QUOTE,
            Self::Generate => &GATEWAY_GENERATE,
        }
    }
}

/// Exact raw Registry callers in the Workflow attempt dispatch chain.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalWorkflowSnapshotCaller {
    /// Text Generate Gateway.
    Text,
    /// Messages Generate Gateway.
    Messages,
    /// Transcription provider adapter.
    AudioTranscribe,
    /// Speech provider adapter.
    AudioSpeech,
    /// Generic Generate provider adapter reached through the Text Gateway.
    TextAdapter,
    /// Generic Generate provider adapter reached through the Messages Gateway.
    MessagesAdapter,
}

impl OriginalWorkflowSnapshotCaller {
    /// All fixed callers.
    pub const ALL: [Self; 6] = [
        Self::Text,
        Self::Messages,
        Self::AudioTranscribe,
        Self::AudioSpeech,
        Self::TextAdapter,
        Self::MessagesAdapter,
    ];

    /// Position in `ALL` and in the corresponding verification results.
    pub const fn index(self) -> usize {
        self as usize
    }

    const fn routes(self) -> (Option<&'static Route>, &'static Route) {
        match self {
            Self::Text => (None, &GATEWAY_TEXT),
            Self::Messages => (None, &GATEWAY_MESSAGES),
            Self::AudioTranscribe => (Some(&MODALITY_TRANSCRIBE), &ADAPTER_TRANSCRIBE),
            Self::AudioSpeech => (Some(&MODALITY_SPEECH), &ADAPTER_SPEECH),
            Self::TextAdapter => (Some(&GATEWAY_TEXT), &PROVIDER_GENERATE),
            Self::MessagesAdapter => (Some(&GATEWAY_MESSAGES), &PROVIDER_GENERATE),
        }
    }
}

/// The two Workflow Gateways that have individual Provider Health grants.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalWorkflowHealthCaller {
    /// Text Generate Gateway.
    Text,
    /// Messages Generate Gateway.
    Messages,
}

impl OriginalWorkflowHealthCaller {
    /// All fixed Workflow Health callers.
    pub const ALL: [Self; 2] = [Self::Text, Self::Messages];

    /// Identify the shared Workflow-to-Gateway route in the snapshot selection.
    pub const fn snapshot_caller(self) -> OriginalWorkflowSnapshotCaller {
        match self {
            Self::Text => OriginalWorkflowSnapshotCaller::Text,
            Self::Messages => OriginalWorkflowSnapshotCaller::Messages,
        }
    }
}

/// The fixed Workflow attempt operations that can enter the dispatch chain.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalWorkflowSnapshotEntry {
    /// `run.advance`.
    Advance,
    /// `run.cancel`.
    Cancel,
    /// `run.selected`.
    Selected,
    /// `run.step.execute`.
    StepExecute,
    /// `run.step.resume`.
    StepResume,
    /// `run.step.retry`.
    StepRetry,
}

impl OriginalWorkflowSnapshotEntry {
    /// All operations in WorkflowAttemptDeclarationV4.
    pub const ALL: [Self; 6] = [
        Self::Advance,
        Self::Cancel,
        Self::Selected,
        Self::StepExecute,
        Self::StepResume,
        Self::StepRetry,
    ];

    /// Position in `ALL` and in the corresponding verification results.
    pub const fn index(self) -> usize {
        self as usize
    }

    const fn route(self) -> &'static Route {
        match self {
            Self::Advance => &WORKFLOW_ADVANCE,
            Self::Cancel => &WORKFLOW_CANCEL,
            Self::Selected => &WORKFLOW_SELECTED,
            Self::StepExecute => &WORKFLOW_STEP_EXECUTE,
            Self::StepResume => &WORKFLOW_STEP_RESUME,
            Self::StepRetry => &WORKFLOW_STEP_RETRY,
        }
    }
}

/// The exact original caller of a read-only Provider Registry operation.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalProviderSnapshotRole {
    /// The Provider Adapter owns this child grant on the selected saved chain.
    Adapter(OriginalSavedLocalChatRole),
    /// AI Gateway owns this child grant on the selected saved chain.
    Gateway {
        /// The Gateway function and the Registry operation it selects.
        role: OriginalModelResolveRole,
        /// Whether the selected Shell enters Saved Turn through Side Chat.
        side_chat: bool,
    },
    /// The saved Gateway's own Provider Registry health grant for this entry.
    GatewayHealth {
        /// The Gateway function and the health operation it selects.
        role: OriginalModelResolveRole,
        /// Whether the selected Shell enters Saved Turn through Side Chat.
        side_chat: bool,
    },
    /// The Workflow attempt entry and its exact raw Registry caller.
    Workflow {
        /// The Gateway or provider adapter with its own Registry grant.
        caller: OriginalWorkflowSnapshotCaller,
        /// The operation selected by the desktop Shell.
        entry: OriginalWorkflowSnapshotEntry,
    },
    /// The Workflow attempt entry and its Gateway's own Provider Health grant.
    WorkflowHealth {
        /// The Gateway with its own Provider Health grant.
        caller: OriginalWorkflowHealthCaller,
        /// The operation selected by the desktop Shell.
        entry: OriginalWorkflowSnapshotEntry,
    },
    /// The ordinary Conversation Complete chain's Gateway Generate Health grant.
    ConversationCompleteHealth,
    /// The independent reviewer's Gateway Health grant for the selected operation.
    ReviewerHealth(OriginalReviewerHealthRole),
    /// The desktop Shell owns this direct settings read grant.
    ShellSettings,
    /// Model Manage owns a nested read grant on the selected Model Save chain.
    ModelSave,
    /// Model Search owns a nested read grant on the selected desktop Shell chain.
    ModelSearch,
    /// Command Datasource owns a nested read grant on the selected desktop Shell chain.
    CommandDatasource,
    /// The direct Registry Model Access read operation selected by the desktop Shell.
    ModelAccessRead,
    /// The direct Registry Model Access catalog operation selected by the desktop Shell.
    ModelAccessCatalog,
}

impl OriginalProviderSnapshotRole {
    /// Whether the role selects the dedicated Provider Health port.
    pub const fn is_health(self) -> bool {
        matches!(
            self,
            Self::GatewayHealth { .. }
                | Self::WorkflowHealth { .. }
                | Self::ConversationCompleteHealth
                | Self::ReviewerHealth(_)
        )
    }
}

impl From<OriginalSavedLocalChatRole> for OriginalProviderSnapshotRole {
    fn from(role: OriginalSavedLocalChatRole) -> Self {
        Self::Adapter(role)
    }
}

/// The signed Workflow entries and their outgoing Provider Registry chains.
pub struct OriginalWorkflowProviderSnapshotSelection<'a> {
    _active: &'a OriginalActiveProfile,
    entries: [Option<OriginalRoutePin>; OriginalWorkflowSnapshotEntry::ALL.len()],
    callers: [Option<WorkflowProviderSnapshotPins>; OriginalWorkflowSnapshotCaller::ALL.len()],
}

struct WorkflowProviderSnapshotPins {
    prefix: Option<OriginalRoutePin>,
    caller: OriginalRoutePin,
    registry: OriginalRoutePin,
}

impl<'a> OriginalWorkflowProviderSnapshotSelection<'a> {
    /// Select exact Workflow entry and nested caller grants from the active Profile.
    /// Verify all returned pins against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Self::select_registry(
            active,
            budget,
            OriginalWorkflowSnapshotCaller::ALL,
            &PROVIDER_READ_GENERATE,
        )
    }

    /// Select exact Workflow entries and the Text and Messages Gateway Health grants.
    /// Verify all returned pins against the signed Bundle before use.
    pub fn select_health(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Self::select_registry(
            active,
            budget,
            OriginalWorkflowHealthCaller::ALL.map(OriginalWorkflowHealthCaller::snapshot_caller),
            &PROVIDER_HEALTH_GENERATE,
        )
    }

    fn select_registry(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        allowed_callers: impl IntoIterator<Item = OriginalWorkflowSnapshotCaller>,
        registry: &'static Route,
    ) -> Result<Option<Self>> {
        Ok(select_workflow_provider_snapshot_records(
            active.records(),
            budget,
            allowed_callers,
            registry,
        )?
        .map(|(entries, callers)| Self {
            _active: active,
            entries,
            callers,
        }))
    }

    /// The desktop Shell grant for this operation.
    pub fn entry(&self, entry: OriginalWorkflowSnapshotEntry) -> Option<&OriginalRoutePin> {
        self.entries[entry.index()].as_ref()
    }

    /// The optional parent Gateway or audio modality and ordered caller and Registry grants.
    pub fn caller(
        &self,
        caller: OriginalWorkflowSnapshotCaller,
    ) -> Option<(Option<&OriginalRoutePin>, [&OriginalRoutePin; 2])> {
        let pins = self.callers[caller.index()].as_ref()?;
        Some((pins.prefix.as_ref(), [&pins.caller, &pins.registry]))
    }
}

/// The signed saved chain and the Gateway's separate Provider Registry grant.
pub struct OriginalGatewayProviderSnapshotChoice<'a> {
    _active: &'a OriginalActiveProfile,
    side_chat: Option<OriginalRoutePin>,
    pins: [OriginalRoutePin; 4],
}

impl<'a> OriginalGatewayProviderSnapshotChoice<'a> {
    /// Select the exact saved entry, Gateway caller, and raw Registry operation.
    /// Verify all returned pins against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalModelResolveRole,
        via_side_chat: bool,
    ) -> Result<Option<Self>> {
        Ok(
            select_gateway_provider_snapshot_records(
                active.records(),
                budget,
                role,
                via_side_chat,
            )?
            .map(|(side_chat, pins)| Self {
                _active: active,
                side_chat,
                pins,
            }),
        )
    }

    /// The original Shell-to-Side Chat grant when entering via Side Chat.
    pub fn side_chat(&self) -> Option<&OriginalRoutePin> {
        self.side_chat.as_ref()
    }

    /// Ordered grants to Turn, Saved Orchestration, Gateway, and Provider Registry.
    pub fn pins(&self) -> &[OriginalRoutePin; 4] {
        &self.pins
    }
}

/// The signed saved entry and the Gateway's separate health grant.
pub struct OriginalGatewayProviderHealthChoice<'a> {
    _active: &'a OriginalActiveProfile,
    via_side_chat: bool,
    side_chat: Option<OriginalRoutePin>,
    pins: [OriginalRoutePin; 4],
}

impl<'a> OriginalGatewayProviderHealthChoice<'a> {
    /// Select the saved Gateway chain and its exact health operation.
    /// Verify all returned pins against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalModelResolveRole,
        via_side_chat: bool,
    ) -> Result<Option<Self>> {
        Ok(
            select_gateway_provider_health_records(active.records(), budget, role, via_side_chat)?
                .map(|(side_chat, pins)| Self {
                    _active: active,
                    via_side_chat,
                    side_chat,
                    pins,
                }),
        )
    }

    /// Whether the authenticated choice enters the saved chain via Side Chat.
    pub fn is_side_chat(&self) -> bool {
        self.via_side_chat
    }

    /// The original Shell-to-Side Chat grant when entering via Side Chat.
    pub fn side_chat(&self) -> Option<&OriginalRoutePin> {
        self.side_chat.as_ref()
    }

    /// Ordered grants to Turn, Saved Orchestration, Gateway, and Registry health.
    pub fn pins(&self) -> &[OriginalRoutePin; 4] {
        &self.pins
    }
}

/// The approval reviewer's signed policy identity and independent Health grants.
pub struct OriginalReviewerHealthChoice<'a> {
    _active: &'a OriginalActiveProfile,
    policy_anchor: OriginalRoutePin,
    pins: [OriginalRoutePin; 2],
}

impl<'a> OriginalReviewerHealthChoice<'a> {
    /// Select the policy identity and the reviewer Gateway and Health grants.
    /// Verify the returned pins against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalReviewerHealthRole,
    ) -> Result<Option<Self>> {
        Ok(
            select_reviewer_health_records(active.records(), budget, role)?.map(
                |(policy_anchor, pins)| Self {
                    _active: active,
                    policy_anchor,
                    pins,
                },
            ),
        )
    }

    /// Evidence of the signed policy principal's identity for Bundle verification only.
    pub fn policy_anchor(&self) -> &OriginalRoutePin {
        &self.policy_anchor
    }

    /// The exact reviewer Gateway and Provider Health grants in call order.
    pub fn pins(&self) -> &[OriginalRoutePin; 2] {
        &self.pins
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

/// The signed ordinary saved chain ending at the original Gateway itself.
pub struct OriginalSavedGatewayChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 3],
}

impl<'a> OriginalSavedGatewayChoice<'a> {
    /// Select the original Gateway role and both of its signed parents.
    /// Verify all three pins against the packaged catalog before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalModelResolveRole,
    ) -> Result<Option<Self>> {
        Ok(
            select_saved_gateway_records(active.records(), budget, role)?.map(|pins| Self {
                _active: active,
                pins,
            }),
        )
    }

    /// Ordered grants from Shell to the selected original Gateway operation.
    pub fn pins(&self) -> &[OriginalRoutePin; 3] {
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

/// The signed ordinary saved grants to one original Gateway tool-intent normalization operation.
pub struct OriginalToolIntentsChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 4],
}

impl<'a> OriginalToolIntentsChoice<'a> {
    /// Select the complete Generate or Stream chain from the authenticated records.
    /// Verify every returned pin against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalModelResolveRole,
    ) -> Result<Option<Self>> {
        Ok(select_saved_role_records(
            active.records(),
            budget,
            role,
            [&AI_TOOL_INTENTS_GENERATE, &AI_TOOL_INTENTS_STREAM],
        )?
        .map(|pins| Self {
            _active: active,
            pins,
        }))
    }

    /// Ordered grants from Shell to the selected original tool-intent normalizer operation.
    pub fn pins(&self) -> &[OriginalRoutePin; 4] {
        &self.pins
    }
}

/// The signed ordinary saved grants to an original Gateway request-preparation operation.
pub struct OriginalRequestPrepareChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 4],
}

impl<'a> OriginalRequestPrepareChoice<'a> {
    /// Select the complete chain for the exact Gateway caller and operation.
    /// Verify every returned pin against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalModelResolveRole,
    ) -> Result<Option<Self>> {
        Ok(
            select_request_prepare_records(active.records(), budget, role)?.map(|pins| Self {
                _active: active,
                pins,
            }),
        )
    }

    /// Ordered grants from Shell to the original request-preparation operation.
    pub fn pins(&self) -> &[OriginalRoutePin; 4] {
        &self.pins
    }
}

/// The signed ordinary Generate chain to the original failover-decision operation.
pub struct OriginalFailoverChoice<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [OriginalRoutePin; 4],
}

impl<'a> OriginalFailoverChoice<'a> {
    /// Select the original Gateway Generate grant used for failover decisions.
    /// Verify every returned pin against the signed Bundle before use.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Ok(
            select_failover_records(active.records(), budget)?.map(|pins| Self {
                _active: active,
                pins,
            }),
        )
    }

    /// Ordered grants from Shell to the original Generate failover-decision operation.
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

#[derive(Clone, Copy)]
enum RouteEvidence {
    ProfileGrant,
    ReviewerPolicyIdentity,
    ReviewerGateway(OriginalReviewerHealthRole),
}

impl RouteEvidence {
    const fn extra_dimensions(self) -> &'static [(&'static str, &'static str)] {
        match self {
            Self::ProfileGrant | Self::ReviewerPolicyIdentity => &[],
            Self::ReviewerGateway(OriginalReviewerHealthRole::Quote) => &[
                ("request_surface", "approval-review"),
                ("tool_calling", "false"),
            ],
            Self::ReviewerGateway(OriginalReviewerHealthRole::Generate) => &[
                ("request_surface", "approval-review"),
                ("tool_calling", "false"),
                ("allow_failover", "false"),
            ],
        }
    }
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
    records.select_shell_chain(shell_target, nested_target)
}

fn select_application_presentation_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: ApplicationPresentationRole,
) -> Result<Option<[OriginalRoutePin; 2]>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    if verified_text(&records.shell_caller) != TURN_SHELL_CALLER {
        return Ok(None);
    }
    let Some(caller) = records.select_shell_route(role.caller())? else {
        return Ok(None);
    };
    Ok(records
        .select_route(caller.function_id(), &APPLICATION_PRESENTATION)?
        .map(|presentation| [caller, presentation]))
}

fn select_conversation_complete_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<ConversationCompleteRoutes>> {
    let Some(records) = selected_desktop_shell_records(active, budget)? else {
        return Ok(None);
    };
    select_conversation_complete_routes(active, &records)
}

fn select_conversation_complete_health_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<(ConversationCompleteRoutes, OriginalRoutePin)>> {
    let Some(records) = selected_desktop_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some(complete) = select_conversation_complete_routes(active, &records)? else {
        return Ok(None);
    };
    let [_, gateway] = &complete.pins;
    Ok(records
        .select_route(gateway.function_id(), &PROVIDER_HEALTH_GENERATE)?
        .map(|health| (complete, health)))
}

fn select_conversation_complete_routes(
    active: (&CoreValue, &CoreValue, &CoreValue),
    records: &Records<'_>,
) -> Result<Option<ConversationCompleteRoutes>> {
    let (profile, lock, plan) = active;
    let Some(conversation) = records.select_shell_route(&CONVERSATION_COMPLETE)? else {
        return Ok(None);
    };
    let Some(gateway) = records.select_route(conversation.function_id(), &GATEWAY_GENERATE)? else {
        return Ok(None);
    };
    if conversation.contract_revision_digest() != CONVERSATION_COMPLETE_REVISION
        || gateway.contract_revision_digest() != CONVERSATION_GENERATE_REVISION
    {
        return Ok(None);
    }
    let application = required(plan, "application")?;
    if !is_text(
        &application,
        "pack_id",
        CONVERSATION_COMPLETE_APPLICATION_ID,
    ) {
        return Ok(None);
    }
    let application_pin = records
        .unique(&records.pack_pins, |pack| {
            is_text(pack, "role", "application")
        })?
        .ok_or(Inconsistent(
            "the selected Application is missing from the Profile",
        ))?;
    let application_digest = digest_field(&application, "artifact_digest")?;
    if !is_text(
        &application_pin,
        "pack_id",
        CONVERSATION_COMPLETE_APPLICATION_ID,
    ) || !is_text(
        &application_pin,
        "artifact_digest",
        verified_text(&application_digest),
    ) || !application
        .eq_guest(&required(lock, "application")?, records.budget)
        .map_err(|_| Capacity)?
    {
        return Err(Inconsistent(
            "the selected Application differs across records",
        ));
    }
    let shell = required(profile, "shell")?;
    Ok(Some(ConversationCompleteRoutes {
        pins: [conversation, gateway],
        shell_artifacts: [
            digest_field(&shell, "artifact_digest")?,
            digest_field(&shell, "executable_artifact_digest")?,
        ],
        application_artifacts: [
            application_digest,
            digest_field(&application, "executable_artifact_digest")?,
        ],
    }))
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

fn select_shell_provider_snapshot_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<OriginalRoutePin>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    if verified_text(&records.shell_caller) != TURN_SHELL_CALLER {
        return Ok(None);
    }
    records.select_shell_route(&PROVIDER_READ)
}

fn select_model_access_provider_snapshot_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<[Option<OriginalRoutePin>; 2]> {
    let Some(records) = selected_desktop_shell_records(active, budget)? else {
        return Ok([None, None]);
    };
    Ok([
        records.select_shell_route(&MODEL_ACCESS_READ)?,
        records.select_shell_route(&MODEL_ACCESS_CATALOG)?,
    ])
}

fn select_query_provider_snapshot_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<[Option<[OriginalRoutePin; 2]>; 2]> {
    let Some(records) = selected_desktop_shell_records(active, budget)? else {
        return Ok([None, None]);
    };

    let select = |owner| -> Result<Option<[OriginalRoutePin; 2]>> {
        Ok(records
            .select_shell_chain(owner, &PROVIDER_READ)?
            .map(|(query, read)| [query, read]))
    };
    Ok([select(&MODEL_SEARCH)?, select(&COMMAND_DATASOURCE)?])
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

fn select_side_chat_saved_receipt_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalSavedReceiptRole,
) -> Result<Option<[OriginalRoutePin; 3]>> {
    let turn = match role {
        OriginalSavedReceiptRole::SideChatTurn => &SAVED_TURN,
        OriginalSavedReceiptRole::SideChatReconcile => &SAVED_RECONCILE,
        OriginalSavedReceiptRole::SavedTurn | OriginalSavedReceiptRole::SavedReconcile => {
            return Ok(None);
        }
    };
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some((Some(side_chat), turn)) = select_turn_entry(&records, true, turn)? else {
        return Ok(None);
    };
    Ok(records
        .select_route(turn.function_id(), &CONVERSATION_READ)?
        .map(|receipt| [side_chat, turn, receipt]))
}

type ConversationConfirmationPins = (Option<OriginalRoutePin>, [OriginalRoutePin; 3]);

fn select_conversation_confirmation_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalSavedReceiptRole,
) -> Result<Option<ConversationConfirmationPins>> {
    let (via_side_chat, turn) = match role {
        OriginalSavedReceiptRole::SavedTurn => (false, &SAVED_TURN),
        OriginalSavedReceiptRole::SavedReconcile => (false, &SAVED_RECONCILE),
        OriginalSavedReceiptRole::SideChatTurn => (true, &SAVED_TURN),
        OriginalSavedReceiptRole::SideChatReconcile => (true, &SAVED_RECONCILE),
    };
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some((side_chat, turn_caller)) = select_turn_entry(&records, via_side_chat, turn)? else {
        return Ok(None);
    };
    let Some(completion) =
        records.select_route(turn_caller.function_id(), &CONVERSATION_CONFIRMATION)?
    else {
        return Ok(None);
    };
    Ok(records
        .select_route(completion.function_id(), &TURN_GET)?
        .map(|turn_get| (side_chat, [turn_caller, completion, turn_get])))
}

type ConversationAppendPins = (Option<OriginalRoutePin>, [OriginalRoutePin; 3]);

fn select_conversation_append_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    caller: ConversationAppendCaller,
) -> Result<Option<ConversationAppendPins>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some((side_chat, turn)) = select_turn_entry(
        &records,
        matches!(caller, ConversationAppendCaller::SideChat),
        &SAVED_TURN,
    )?
    else {
        return Ok(None);
    };
    let Some(orchestration) = records.select_route(turn.function_id(), &SAVED_ORCHESTRATION)?
    else {
        return Ok(None);
    };
    Ok(records
        .select_route(orchestration.function_id(), &CONVERSATION_MESSAGE_MANAGE)?
        .map(|manage| (side_chat, [turn, orchestration, manage])))
}

type TurnEntryPins = (Option<OriginalRoutePin>, OriginalRoutePin);

fn select_turn_entry(
    records: &Records<'_>,
    via_side_chat: bool,
    turn: &'static Route,
) -> Result<Option<TurnEntryPins>> {
    if verified_text(&records.shell_caller) != TURN_SHELL_CALLER {
        return Ok(None);
    }
    let side_chat = if via_side_chat {
        let Some(side_chat) = records.select_shell_route(&SIDE_CHAT_TURN)? else {
            return Ok(None);
        };
        Some(side_chat)
    } else {
        None
    };
    let turn_caller = side_chat
        .as_ref()
        .map_or(TURN_SHELL_CALLER, OriginalRoutePin::function_id);
    Ok(records
        .select_route(turn_caller, turn)?
        .map(|turn| (side_chat, turn)))
}

type SavedLocalChatPins = (Option<OriginalRoutePin>, [OriginalRoutePin; 5]);

fn select_saved_local_chat_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalSavedLocalChatRole,
) -> Result<Option<SavedLocalChatPins>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some((side_chat, turn)) = select_turn_entry(&records, role.is_side_chat(), &SAVED_TURN)?
    else {
        return Ok(None);
    };
    let Some(orchestration) = records.select_route(turn.function_id(), &SAVED_ORCHESTRATION)?
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
        .map(|registry| (side_chat, [turn, orchestration, gateway, adapter, registry])))
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

type SavedTurnStartPins = (Option<OriginalRoutePin>, [OriginalRoutePin; 2]);

fn select_saved_turn_start_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    caller: SavedTurnStartCaller,
) -> Result<Option<SavedTurnStartPins>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some((side_chat, turn)) = select_turn_entry(
        &records,
        matches!(caller, SavedTurnStartCaller::SideChat),
        &SAVED_TURN,
    )?
    else {
        return Ok(None);
    };
    Ok(records
        .select_route(turn.function_id(), &TURN_LIFECYCLE)?
        .map(|lifecycle| (side_chat, [turn, lifecycle])))
}

fn select_calendar_saved_input_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<[OriginalRoutePin; 4]>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some(scheduler) = records.select_route(SCHEDULER_CLOCK_CALLER, &SCHEDULER_CONTROL)? else {
        return Ok(None);
    };
    let Some(broker) = records.select_route(scheduler.function_id(), &JOB_ACTION_BROKER)? else {
        return Ok(None);
    };
    let Some(adapter) = records.select_route(broker.function_id(), &CHAT_SAVED_JOB_ADAPTER)? else {
        return Ok(None);
    };
    Ok(records
        .select_route(adapter.function_id(), &TURN_LIFECYCLE)?
        .map(|lifecycle| [scheduler, broker, adapter, lifecycle]))
}

fn select_saved_turn_settlement_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalSavedTurnSettlementRole,
) -> Result<Option<TurnEntryPins>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    select_turn_entry(&records, role.via_side_chat(), role.route())
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

type SavedGatewayChildPins = (Option<OriginalRoutePin>, [OriginalRoutePin; 4]);

type WorkflowProviderSnapshotSelection = (
    [Option<OriginalRoutePin>; OriginalWorkflowSnapshotEntry::ALL.len()],
    [Option<WorkflowProviderSnapshotPins>; OriginalWorkflowSnapshotCaller::ALL.len()],
);

fn select_workflow_provider_snapshot_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    allowed_callers: impl IntoIterator<Item = OriginalWorkflowSnapshotCaller>,
    registry_route: &'static Route,
) -> Result<Option<WorkflowProviderSnapshotSelection>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    if records.shell_caller.as_str() != Some("shell.tauri.default") {
        return Ok(None);
    }
    let mut entries = [const { None }; OriginalWorkflowSnapshotEntry::ALL.len()];
    for entry in OriginalWorkflowSnapshotEntry::ALL {
        entries[entry.index()] = records.select_shell_route(entry.route())?;
    }
    if entries.iter().all(Option::is_none) {
        return Ok(None);
    }
    // Outgoing edges are keyed by the Workflow function, shared by its allowed entries.
    let mut callers = [const { None }; OriginalWorkflowSnapshotCaller::ALL.len()];
    for caller in allowed_callers {
        let (prefix_route, caller_route) = caller.routes();
        let (prefix, caller_function) = if let Some(route) = prefix_route {
            let Some(pin) = records.select_route(WORKFLOW_ADVANCE.function, route)? else {
                continue;
            };
            (Some(pin), route.function)
        } else {
            (None, WORKFLOW_ADVANCE.function)
        };
        let Some(caller_pin) = records.select_route(caller_function, caller_route)? else {
            continue;
        };
        let Some(registry) = records.select_route(caller_pin.function_id(), registry_route)? else {
            continue;
        };
        callers[caller.index()] = Some(WorkflowProviderSnapshotPins {
            prefix,
            caller: caller_pin,
            registry,
        });
    }
    Ok(callers
        .iter()
        .any(Option::is_some)
        .then_some((entries, callers)))
}

fn select_gateway_provider_snapshot_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalModelResolveRole,
    via_side_chat: bool,
) -> Result<Option<SavedGatewayChildPins>> {
    let registry = match role {
        OriginalModelResolveRole::QuoteGenerate | OriginalModelResolveRole::Generate => {
            &PROVIDER_READ_GENERATE
        }
        OriginalModelResolveRole::QuoteStream | OriginalModelResolveRole::Stream => {
            &PROVIDER_READ_STREAM
        }
    };
    select_saved_gateway_registry_records(
        active,
        budget,
        via_side_chat,
        role.gateway_route(),
        registry,
    )
}

fn select_gateway_provider_health_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalModelResolveRole,
    via_side_chat: bool,
) -> Result<Option<SavedGatewayChildPins>> {
    let health = match role {
        OriginalModelResolveRole::QuoteGenerate | OriginalModelResolveRole::Generate => {
            &PROVIDER_HEALTH_GENERATE
        }
        OriginalModelResolveRole::QuoteStream | OriginalModelResolveRole::Stream => {
            &PROVIDER_HEALTH_STREAM
        }
    };
    select_saved_gateway_registry_records(
        active,
        budget,
        via_side_chat,
        role.gateway_route(),
        health,
    )
}

fn select_reviewer_health_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalReviewerHealthRole,
) -> Result<Option<(OriginalRoutePin, [OriginalRoutePin; 2])>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some(policy_anchor) = records.select_reviewer_policy_anchor()? else {
        return Ok(None);
    };
    let Some(gateway) = records.select_reviewer_gateway(&policy_anchor, role)? else {
        return Ok(None);
    };
    Ok(records
        .select_route(gateway.function_id(), &PROVIDER_HEALTH_GENERATE)?
        .map(|health| (policy_anchor, [gateway, health])))
}

fn select_saved_gateway_registry_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    via_side_chat: bool,
    gateway: &'static Route,
    registry: &'static Route,
) -> Result<Option<SavedGatewayChildPins>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some((side_chat, turn)) = select_turn_entry(&records, via_side_chat, &SAVED_TURN)? else {
        return Ok(None);
    };
    let Some(orchestration) = records.select_route(turn.function_id(), &SAVED_ORCHESTRATION)?
    else {
        return Ok(None);
    };
    Ok(
        select_saved_gateway_child(&records, [turn, orchestration], gateway, registry)?
            .map(|pins| (side_chat, pins)),
    )
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

fn select_saved_gateway_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalModelResolveRole,
) -> Result<Option<[OriginalRoutePin; 3]>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some(saved) = select_ordinary_saved_chain(&records, &SAVED_ORCHESTRATION)? else {
        return Ok(None);
    };
    select_saved_gateway(&records, saved, role.gateway_route())
}

fn select_usage_cost_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalModelResolveRole,
) -> Result<Option<[OriginalRoutePin; 4]>> {
    select_saved_role_records(
        active,
        budget,
        role,
        [&AI_USAGE_COST_GENERATE, &AI_USAGE_COST_STREAM],
    )
}

fn select_saved_role_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalModelResolveRole,
    [generate, stream]: [&'static Route; 2],
) -> Result<Option<[OriginalRoutePin; 4]>> {
    let child = match role {
        OriginalModelResolveRole::Generate => generate,
        OriginalModelResolveRole::Stream => stream,
        OriginalModelResolveRole::QuoteGenerate | OriginalModelResolveRole::QuoteStream => {
            return Ok(None);
        }
    };
    select_ordinary_gateway_child(active, budget, role, child)
}

fn select_request_prepare_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalModelResolveRole,
) -> Result<Option<[OriginalRoutePin; 4]>> {
    let child = match role {
        OriginalModelResolveRole::QuoteGenerate | OriginalModelResolveRole::Generate => {
            &AI_REQUEST_PREPARE_GENERATE
        }
        OriginalModelResolveRole::QuoteStream | OriginalModelResolveRole::Stream => {
            &AI_REQUEST_PREPARE_STREAM
        }
    };
    select_ordinary_gateway_child(active, budget, role, child)
}

fn select_failover_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<[OriginalRoutePin; 4]>> {
    // The original Gateway returns its streaming failure before consulting failover.
    select_ordinary_gateway_child(
        active,
        budget,
        OriginalModelResolveRole::Generate,
        &AI_FAILOVER_GENERATE,
    )
}

fn select_ordinary_gateway_child(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
    role: OriginalModelResolveRole,
    child: &'static Route,
) -> Result<Option<[OriginalRoutePin; 4]>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some(saved) = select_ordinary_saved_chain(&records, &SAVED_ORCHESTRATION)? else {
        return Ok(None);
    };
    select_saved_gateway_child(&records, saved, role.gateway_route(), child)
}

fn select_saved_gateway_child(
    records: &Records<'_>,
    saved: [OriginalRoutePin; 2],
    gateway: &'static Route,
    child: &'static Route,
) -> Result<Option<[OriginalRoutePin; 4]>> {
    let Some([turn, orchestration, gateway]) = select_saved_gateway(records, saved, gateway)?
    else {
        return Ok(None);
    };
    Ok(records
        .select_route(gateway.function_id(), child)?
        .map(|child| [turn, orchestration, gateway, child]))
}

fn select_saved_gateway(
    records: &Records<'_>,
    [turn, orchestration]: [OriginalRoutePin; 2],
    gateway: &'static Route,
) -> Result<Option<[OriginalRoutePin; 3]>> {
    Ok(records
        .select_route(orchestration.function_id(), gateway)?
        .map(|gateway| [turn, orchestration, gateway]))
}

fn selected_desktop_shell_records<'a>(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &'a Arc<dyn AllocationBudget>,
) -> Result<Option<Records<'a>>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let shell = required(active.0, "shell")?;
    if verified_text(&records.shell_caller) != TURN_SHELL_CALLER
        || !is_text(&shell, "pack_id", TURN_SHELL_CALLER)
    {
        return Ok(None);
    }
    Ok(Some(records))
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
    fn select_shell_chain(
        &self,
        shell_target: &'static Route,
        nested_target: &'static Route,
    ) -> Result<Option<(OriginalRoutePin, OriginalRoutePin)>> {
        let Some(first) = self.select_shell_route(shell_target)? else {
            return Ok(None);
        };
        Ok(self
            .select_route(first.function_id(), nested_target)?
            .map(|second| (first, second)))
    }

    fn select_shell_route(&self, route: &'static Route) -> Result<Option<OriginalRoutePin>> {
        self.select_route(verified_text(&self.shell_caller), route)
    }

    fn select_route(
        &self,
        caller: &str,
        route: &'static Route,
    ) -> Result<Option<OriginalRoutePin>> {
        self.select_route_evidence(caller, route, RouteEvidence::ProfileGrant)
    }

    fn select_reviewer_policy_anchor(&self) -> Result<Option<OriginalRoutePin>> {
        self.select_route_evidence(
            REVIEWER_POLICY_CALLER,
            &REVIEWER_POLICY_ANCHOR,
            RouteEvidence::ReviewerPolicyIdentity,
        )
    }

    fn select_reviewer_gateway(
        &self,
        policy_anchor: &OriginalRoutePin,
        role: OriginalReviewerHealthRole,
    ) -> Result<Option<OriginalRoutePin>> {
        self.select_route_evidence(
            policy_anchor.function_id(),
            role.gateway_route(),
            RouteEvidence::ReviewerGateway(role),
        )
    }

    fn select_route_evidence(
        &self,
        caller: &str,
        route: &'static Route,
        evidence: RouteEvidence,
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
        let expects_profile_grant = !matches!(evidence, RouteEvidence::ReviewerPolicyIdentity);
        if grant != expects_profile_grant
            || verified_text(&variant) != route.variant
            || route
                .execution_fields()
                .into_iter()
                .any(|(field, expected)| !is_text(&binding, field, expected))
            || !adapters.is_empty()
            || !exact_scope(&scope, route, verified_text(&revision), evidence)?
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

fn exact_scope(
    scope: &CoreValue,
    route: &Route,
    revision: &str,
    evidence: RouteEvidence,
) -> Result<bool> {
    let dimensions = required(scope, "dimensions")?;
    let extra_dimensions = evidence.extra_dimensions();
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
        && dimensions
            .object()
            .is_some_and(|object| object.len() == 2 + extra_dimensions.len())
        && matches_dimension("contract", route.contract)
        && matches_dimension("operation", route.operation)
        && extra_dimensions
            .iter()
            .all(|&(field, expected)| matches_dimension(field, expected))
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

    static AI_FAILOVER_STREAM: Route = Route {
        operation: "rumi_ai_pipeline_pack.ai-failover-decide.stream",
        ..AI_FAILOVER_GENERATE
    };

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
        let application_artifact_digest = Digest::hash(b"application");
        let application = json!({
            "pack_id": CONVERSATION_COMPLETE_APPLICATION_ID,
            "artifact_digest": application_artifact_digest.as_str(),
            "executable_artifact_digest": Digest::hash(b"executable").as_str(),
            "definition_digest": Digest::hash(b"application-definition").as_str(),
        });
        let mut records = json!({
            "profile": {
                "shell": shell,
                "packs": [{
                    "pack_id": CONVERSATION_COMPLETE_APPLICATION_ID,
                    "artifact_digest": application_artifact_digest.as_str(),
                    "role": "application"
                }],
                "requested_edges": [],
                "authority_references": [],
                "profile_authority_snapshot_digest":
                    "sha256:0df912a62eed85abe0cadd3f18e5e419d2b011f7be059759fe71537879bdb276"
            },
            "lock": {"shell": shell, "application": application, "variant_pins": []},
            "plan": {"shell": plan_shell, "application": application, "bindings": []}
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
            (
                &AI_TOOL_INTENTS_GENERATE,
                GATEWAY_GENERATE_CALLER,
                "authority-ref:2f4b6034050be06a46540b5106faf247f6cb1343bbe9d73043147e0ba3c02cdd",
                "sha256:d61573162f6e5bca7acadfb039dc367001fc1422ad1fc27a184586c77b0b6c75",
                "sha256:fa057f5213e0169559a113db4049be08e89e72009532d2b5d331f2fadb86675e",
            ),
            (
                &AI_TOOL_INTENTS_STREAM,
                GATEWAY_STREAM_CALLER,
                "authority-ref:7bdfdb9836c8f97d367562cd407397c29839167b2ed948115dd3ed0bf263d238",
                "sha256:d61573162f6e5bca7acadfb039dc367001fc1422ad1fc27a184586c77b0b6c75",
                "sha256:65a9e1a0fafefe735fdc1494ed454f695a32e60d8c269bcb9ecf4597e64b62be",
            ),
            (
                &AI_REQUEST_PREPARE_GENERATE,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:dd380f85c4f21c8404cce2edfce59b384c968c43a92663642f6c3e59bae17c81",
                "sha256:96fc8b32cf8543c448b07b66c3b47e03e3db113de522debd0f619d0f247b78bc",
                "sha256:1fe94d8fb4d36baaed8cb007ebbd9806ea91c116f94b385a4118196637a3e76e",
            ),
            (
                &AI_REQUEST_PREPARE_STREAM,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:3aab30da8bb7e59a5c890bf0b703c6074738f63c36aaae99162d60750a8d6094",
                "sha256:96fc8b32cf8543c448b07b66c3b47e03e3db113de522debd0f619d0f247b78bc",
                "sha256:d2eabb89cc3ee99bc66d29dbf314e2f320e28021122d575c2da83e8d0ceeeeaf",
            ),
            (
                &AI_REQUEST_PREPARE_GENERATE,
                GATEWAY_GENERATE_CALLER,
                "authority-ref:d38626b7966e42b8a9f2c834d66dfe69111ab62d6b9dc4fcb8dcdda5e12585a6",
                "sha256:96fc8b32cf8543c448b07b66c3b47e03e3db113de522debd0f619d0f247b78bc",
                "sha256:1fe94d8fb4d36baaed8cb007ebbd9806ea91c116f94b385a4118196637a3e76e",
            ),
            (
                &AI_REQUEST_PREPARE_STREAM,
                GATEWAY_STREAM_CALLER,
                "authority-ref:92f1d626fd8130b7e6850e8a84b71af3cd6351e2e782f05b4d44344cb23f62aa",
                "sha256:96fc8b32cf8543c448b07b66c3b47e03e3db113de522debd0f619d0f247b78bc",
                "sha256:d2eabb89cc3ee99bc66d29dbf314e2f320e28021122d575c2da83e8d0ceeeeaf",
            ),
            (
                &AI_FAILOVER_GENERATE,
                GATEWAY_GENERATE_CALLER,
                "authority-ref:0fe90adffe58d0ba387c5e3c589f7da715bb21731da482fc3ef7b633c7813b99",
                "sha256:39508895d8bb9105029c246dcefaf063aba8c62f6e498f626d55f03b1d1d063d",
                "sha256:4c6e753476bb584f4e32cdbf05e55ff7babd3e87632fe20035718a5978f6812e",
            ),
            (
                &AI_FAILOVER_STREAM,
                GATEWAY_STREAM_CALLER,
                "authority-ref:64afc8239d6e5b7d7d8d071692566782de976ec9a7313473abe1be628362d748",
                "sha256:39508895d8bb9105029c246dcefaf063aba8c62f6e498f626d55f03b1d1d063d",
                "sha256:49b503091bb37f7f3a8a0ec8e3a2242df94d09f2d494edf2a88f7b8720241d8b",
            ),
            (
                &SETTINGS_READ,
                SHELL,
                "authority-ref:e3dc6bccce89cfce7dbad6ef94e9dcaf3b3eaf2438e5973218af170a89921159",
                "sha256:bf2a86f5787194c8301e93586e03495ed4b5d2c6537c9227870943754a5f416c",
                "sha256:a0d56d52768d389143361876ddc49913b35528cbd6f4ea83ab28d6ac043ea3d5",
            ),
            (
                &APPLICATION_PRESENTATION,
                SETTINGS_READ.function,
                "authority-ref:0a4549ef06aac7f9458a86284db7cfca7489111f56713239fbb6278fc5349063",
                "sha256:d6e1d7af7ba7009863707c15c253dfe43728453b901c6db7c1d33e0861357967",
                "sha256:278f3e40167b87ecb30c79605645e506d39952386dff5dfbb27f591e5467cadd",
            ),
            (
                &CATALOG_READ,
                SHELL,
                "authority-ref:e7310d968403282e5eb7354962bc80e2cd740ea504ab36971fe33c3226b57210",
                "sha256:bf2a86f5787194c8301e93586e03495ed4b5d2c6537c9227870943754a5f416c",
                "sha256:db48a9b7c1991f86a26b31b4dc388c1e10d5e4be786a9165fc1f6c27666d58df",
            ),
            (
                &APPLICATION_PRESENTATION,
                CATALOG_READ.function,
                "authority-ref:3ed084f1ee19dc303e13c7e592efc25dbdece96714c7b25ce6009f64d446dd5d",
                "sha256:d6e1d7af7ba7009863707c15c253dfe43728453b901c6db7c1d33e0861357967",
                "sha256:278f3e40167b87ecb30c79605645e506d39952386dff5dfbb27f591e5467cadd",
            ),
            (
                &COMMAND_CATALOG_READ,
                SHELL,
                "authority-ref:782a93445998e0e790119294021f927425a6423fb2dbc82a5166604219e4c908",
                "sha256:5c4331eb18c513f72b57ae7310400710fed18cecf2d99f9b068bb04ba6f5c391",
                "sha256:47defa37cae17ebab9e36fad4c123b72e496b6bde4d704ea69252a005074cb13",
            ),
            (
                &APPLICATION_PRESENTATION,
                COMMAND_CATALOG_READ.function,
                "authority-ref:29e02b0b35458b7763a20fdf4c158eed9786632b07368052da8a4f5ce0928598",
                "sha256:d6e1d7af7ba7009863707c15c253dfe43728453b901c6db7c1d33e0861357967",
                "sha256:278f3e40167b87ecb30c79605645e506d39952386dff5dfbb27f591e5467cadd",
            ),
            (
                &CONVERSATION_COMPLETE,
                SHELL,
                "authority-ref:e24a9be3479f642b40d6a7b9d8cef669553dad8086881373ac90ff8ed6fccd90",
                CONVERSATION_COMPLETE_REVISION,
                "sha256:0143a990ff774359ea090bfed3c5247f37c0e48d4f813abe09d781f7bddef639",
            ),
            (
                &GATEWAY_GENERATE,
                CONVERSATION_COMPLETE.function,
                "authority-ref:70aaac3025b9a3cbb0faf7ecaa897e581a305242f8d53c4eda1388d2733d24c7",
                CONVERSATION_GENERATE_REVISION,
                "sha256:aed4252c92a298de9bed574c81b5e421fa7f592541ce146b0e77b0c085f42370",
            ),
            (
                &SIDE_CHAT_TURN,
                SHELL,
                "authority-ref:aa8a670e8e7a339da934002d1423e5a39ddb7a0de6e7c0d2a0152848026ce070",
                "sha256:1b82e55995eb061819839ac0f42a89f1ebcc2c4ca9c8bc3206253767b9d9e452",
                "sha256:67f7c67c2400747fc1629a4ae5dd630c8a93d90eb7197387223a82d17f1a7b12",
            ),
            (
                &SAVED_TURN,
                SIDE_CHAT_TURN.function,
                "authority-ref:fe0224ee12c57aee177055c1d1accca8444022c7efa9aa78948669ad5d621e3e",
                "sha256:b7dda70b162f84a9af42022f3025059c074d6a66dc5b7b2b7ef8b153d48fc8c9",
                "sha256:def47bb0d27a6a624fd7ac61b770fa148a287173f52e79416a0476927a17b930",
            ),
            (
                &SAVED_RECONCILE,
                SIDE_CHAT_TURN.function,
                "authority-ref:a88cffd09547e22e7ab688a8a6a1172943a2aed40a17abae535c3e8a4c56b3bc",
                "sha256:4206c74719c2a3e9a033544695736ef6273747c2c9a186ec7330a57fca283070",
                "sha256:ec01327293949904e663230af519789526a68e3acafcef188a7c01a7d70bfc2c",
            ),
            (
                &PROVIDER_READ_GENERATE,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:9cc5cd380a7e7f7f051d2ae9e1c9fbe1a217d717936cc8de0d2c2d9f15bab44c",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:01dea66907528b42fe211fda67bf9c09aeeecf6bf092de43f15317a0f37c6184",
            ),
            (
                &PROVIDER_READ_STREAM,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:f6200e8ce9d2f4dd8fb4690d366be879dcddc812e97781d6c00d5bc0bee20ce6",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:ba31ddc2fe6a194376c0130d1cce5b2bd6af8c36c5c7353584443247786bf883",
            ),
            (
                &PROVIDER_READ_GENERATE,
                GATEWAY_GENERATE_CALLER,
                "authority-ref:cd2620de5dee7efe90a04b878da3aa32fde40b9dab313a662bd539f988b0b521",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:01dea66907528b42fe211fda67bf9c09aeeecf6bf092de43f15317a0f37c6184",
            ),
            (
                &PROVIDER_READ_STREAM,
                GATEWAY_STREAM_CALLER,
                "authority-ref:3de4d2eec347e6f6d995739d9187f0898fc2e8a6978a360674e52a9fd7116c18",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:ba31ddc2fe6a194376c0130d1cce5b2bd6af8c36c5c7353584443247786bf883",
            ),
            (
                &MODEL_SEARCH,
                SHELL,
                "authority-ref:0a9ae4ecb5ba14e4781dd6558d076a2f0acaccec4f082f6fda07cf11ff645cbc",
                "sha256:ffff3d094eb89e7ce42a67a600c717036d20f2b2bb3fabeab19a79574a16369a",
                "sha256:46b9fcfddedb6966576d9d8d94b96bdcc3fe6204e2e3c26e00eb821e0416034c",
            ),
            (
                &PROVIDER_READ,
                MODEL_SEARCH.function,
                "authority-ref:a1029edaefa8d9500b023e60d6ac2bd56a7fd51e107674b77a8813aed8487e30",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:1b58a95d7c67e6f789b5e17a10a61c360c706443adbbe3026502e7e29395bd6f",
            ),
            (
                &COMMAND_DATASOURCE,
                SHELL,
                "authority-ref:15324217b3da35c98a17a55304dec45cb039323d9459243d9e9a6dab06a26213",
                "sha256:0e660d5b3ebc6ebcdff186a95ab831dd056a354ce6278a5661d771701a0a4647",
                "sha256:9870e0eed292e2c21eae4ecf0954e5ed3b277ccf080f32c3e1167138b70e3ae0",
            ),
            (
                &PROVIDER_READ,
                COMMAND_DATASOURCE.function,
                "authority-ref:9dc62018a5fd2b7acce6094a373f01dd133619eb4b4b8f5de762091e793ba537",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:1b58a95d7c67e6f789b5e17a10a61c360c706443adbbe3026502e7e29395bd6f",
            ),
            (
                &PROVIDER_HEALTH_GENERATE,
                GATEWAY_GENERATE_CALLER,
                "authority-ref:51bde75f30a996b2f0be7732403363148c56fe3a170c9e9df20acd6722d6c5eb",
                "sha256:5636a93e43b5226774e2add17ae792f15ec4ff53fd6202d5027dfc3a1ea840a4",
                "sha256:5c6f9da7c4f8f248a5223cf8ab714b18615b83fff1fe967d07072fbdb49b3718",
            ),
            (
                &PROVIDER_HEALTH_STREAM,
                GATEWAY_STREAM_CALLER,
                "authority-ref:a6111c21e6d567a83a31c1b5da7efdf9609de66717d436d1066d67874ca17a17",
                "sha256:5636a93e43b5226774e2add17ae792f15ec4ff53fd6202d5027dfc3a1ea840a4",
                "sha256:ab3b9f63a045331009f2ef9c397a8b365e7b61007111d2401bf9659ac3397d7a",
            ),
            (
                &PROVIDER_HEALTH_GENERATE,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:efd1c3283d7868bb3f4b94d2c8115248ff0e123451c6c11b040ff8e87dd8d4c1",
                "sha256:5636a93e43b5226774e2add17ae792f15ec4ff53fd6202d5027dfc3a1ea840a4",
                "sha256:5c6f9da7c4f8f248a5223cf8ab714b18615b83fff1fe967d07072fbdb49b3718",
            ),
            (
                &PROVIDER_HEALTH_STREAM,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:c08a9d7e2639a0b17ff724f986e2da492318e5f295d73d685b09919853e19f9c",
                "sha256:5636a93e43b5226774e2add17ae792f15ec4ff53fd6202d5027dfc3a1ea840a4",
                "sha256:ab3b9f63a045331009f2ef9c397a8b365e7b61007111d2401bf9659ac3397d7a",
            ),
        ];
        for (route, caller, reference, revision, scope_digest) in rows {
            add_record(
                &mut records,
                route,
                caller,
                reference,
                revision,
                scope_digest,
            );
        }
        records
    }

    fn add_record(
        records: &mut Value,
        route: &Route,
        caller: &str,
        reference: &str,
        revision: &str,
        scope_digest: &str,
    ) {
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
            .as_array_mut()
            .unwrap()
            .push(json!({
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

    fn parse_records(records: &Value, budget: &Arc<dyn AllocationBudget>) -> [CoreValue; 3] {
        ["profile", "lock", "plan"].map(|field| {
            let bytes = serde_json::to_vec(&records[field]).unwrap();
            CoreValue::parse_guest(&bytes, budget).unwrap()
        })
    }

    fn add_fixture_grant(
        fixture: &mut Value,
        caller: &str,
        route: &Route,
        budget: &Arc<dyn AllocationBudget>,
    ) {
        let snapshot = fixture["profile"]["profile_authority_snapshot_digest"]
            .as_str()
            .unwrap();
        let reference = authority_reference(caller, route, snapshot, budget).unwrap();
        let revision = Digest::hash(route.contract.as_bytes());
        let scope = json!({
            "capability": "operation.invoke",
            "semantics_digest": revision.as_str(),
            "dimensions": {"operation": [route.operation], "contract": [route.contract]},
            "quotas": {},
            "opaque": false,
            "exact_request_digest": null,
        });
        let scope_digest = CoreValue::parse_guest(&serde_json::to_vec(&scope).unwrap(), budget)
            .unwrap()
            .digest_guest(budget)
            .unwrap();
        add_record(
            fixture,
            route,
            caller,
            &format!("authority-ref:{}", reference.hex()),
            revision.as_str(),
            scope_digest.as_str(),
        );
    }

    fn workflow_snapshot_records(health_callers: &[OriginalWorkflowSnapshotCaller]) -> Value {
        let mut fixture = records();
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let mut add = |caller, route: &Route| {
            if fixture["profile"]["requested_edges"]
                .as_array()
                .unwrap()
                .iter()
                .any(|edge| {
                    edge["caller_function_id"] == caller
                        && edge["operation_id"] == route.operation
                        && edge["contract_id"] == route.contract
                        && edge["target_provider_id"] == route.function
                })
            {
                return;
            }
            add_fixture_grant(&mut fixture, caller, route, &budget);
        };
        for entry in OriginalWorkflowSnapshotEntry::ALL {
            add("shell.tauri.default", entry.route());
        }
        for caller in OriginalWorkflowSnapshotCaller::ALL {
            let (prefix, route) = caller.routes();
            if let Some(prefix) = prefix {
                add(WORKFLOW_ADVANCE.function, prefix);
            }
            add(prefix.unwrap_or(&WORKFLOW_ADVANCE).function, route);
            add(route.function, &PROVIDER_READ_GENERATE);
        }
        for caller in health_callers {
            add(caller.routes().1.function, &PROVIDER_HEALTH_GENERATE);
        }
        fixture
    }

    fn select_workflow_snapshot(
        fixture: &Value,
    ) -> Result<Option<WorkflowProviderSnapshotSelection>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(fixture, &budget);
        select_workflow_provider_snapshot_records(
            (&profile, &lock, &plan),
            &budget,
            OriginalWorkflowSnapshotCaller::ALL,
            &PROVIDER_READ_GENERATE,
        )
    }

    fn select_workflow_health(
        fixture: &Value,
    ) -> Result<Option<WorkflowProviderSnapshotSelection>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(fixture, &budget);
        select_workflow_provider_snapshot_records(
            (&profile, &lock, &plan),
            &budget,
            OriginalWorkflowHealthCaller::ALL.map(OriginalWorkflowHealthCaller::snapshot_caller),
            &PROVIDER_HEALTH_GENERATE,
        )
    }

    fn set_grant_interactive_only(fixture: &mut Value, row: usize) {
        fixture["profile"]["requested_edges"][row]["authority_mode"] = json!("interactive_only");
        fixture["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
    }

    fn sync_fixture_scope_digest(
        fixture: &mut Value,
        row: usize,
        budget: &Arc<dyn AllocationBudget>,
    ) {
        let scope = &fixture["profile"]["requested_edges"][row]["requested_scope_template"];
        let bytes = serde_json::to_vec(scope).unwrap();
        let scope = CoreValue::parse_guest(&bytes, budget).unwrap();
        fixture["plan"]["bindings"][row]["requested_scope_digest"] =
            json!(scope.digest_guest(budget).unwrap().as_str());
    }

    fn reviewer_health_fixture(budget: &Arc<dyn AllocationBudget>) -> (Value, usize, [usize; 2]) {
        let mut fixture = records();
        let anchor = fixture["profile"]["requested_edges"]
            .as_array()
            .unwrap()
            .len();
        add_fixture_grant(
            &mut fixture,
            REVIEWER_POLICY_CALLER,
            &REVIEWER_POLICY_ANCHOR,
            budget,
        );
        set_grant_interactive_only(&mut fixture, anchor);
        let gateways = OriginalReviewerHealthRole::ALL.map(|role| {
            let row = fixture["profile"]["requested_edges"].as_array().unwrap().len();
            add_fixture_grant(&mut fixture, REVIEWER_POLICY_ANCHOR.function, role.gateway_route(), budget);
            let dimensions = &mut fixture["profile"]["requested_edges"][row]["requested_scope_template"]["dimensions"];
            dimensions["request_surface"] = json!(["approval-review"]);
            dimensions["tool_calling"] = json!(["false"]);
            if role == OriginalReviewerHealthRole::Generate {
                dimensions["allow_failover"] = json!(["false"]);
            }
            sync_fixture_scope_digest(&mut fixture, row, budget);
            row
        });
        (fixture, anchor, gateways)
    }

    fn select_reviewer_health(
        fixture: &Value,
        budget: &Arc<dyn AllocationBudget>,
        role: OriginalReviewerHealthRole,
    ) -> Result<Option<(OriginalRoutePin, [OriginalRoutePin; 2])>> {
        let [profile, lock, plan] = parse_records(fixture, budget);
        select_reviewer_health_records((&profile, &lock, &plan), budget, role)
    }

    #[test]
    fn reviewer_health_requires_the_interactive_identity_and_separate_gateway_grants() {
        use OriginalReviewerHealthRole::{Generate, Quote};

        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let (fixture, anchor_row, gateway_rows) = reviewer_health_fixture(&budget);
        let select = |fixture: &Value, role| select_reviewer_health(fixture, &budget, role);
        for (role, other, ordinary_role) in [
            (Quote, Generate, OriginalModelResolveRole::QuoteGenerate),
            (Generate, Quote, OriginalModelResolveRole::Generate),
        ] {
            let (anchor, [gateway, health]) = select(&fixture, role).unwrap().unwrap();
            assert_eq!(anchor.caller_function_id(), REVIEWER_POLICY_CALLER);
            assert_eq!(anchor.operation_id(), REVIEWER_POLICY_ANCHOR.operation);
            assert_eq!(gateway.caller_function_id(), anchor.function_id());
            assert_eq!(gateway.operation_id(), role.gateway_route().operation);
            assert_eq!(health.caller_function_id(), gateway.function_id());
            assert_eq!(health.operation_id(), PROVIDER_HEALTH_GENERATE.operation);
            assert!(select(&records(), role).unwrap().is_none());

            let mut wrong_mode = fixture.clone();
            wrong_mode["profile"]["requested_edges"][anchor_row]["authority_mode"] =
                json!("profile_grant");
            assert!(matches!(select(&wrong_mode, role), Err(Inconsistent(_))));
            wrong_mode["plan"]["bindings"][anchor_row]["authority_mode"] = json!("profile_grant");
            assert!(select(&wrong_mode, role).unwrap().is_none());

            let mut wrong_anchor = fixture.clone();
            wrong_anchor["profile"]["requested_edges"][anchor_row]["caller_function_id"] =
                json!(TURN_SHELL_CALLER);
            wrong_anchor["plan"]["bindings"][anchor_row]["caller_function_id"] =
                json!(TURN_SHELL_CALLER);
            assert!(select(&wrong_anchor, role).unwrap().is_none());

            let mut only_other = fixture.clone();
            set_grant_interactive_only(&mut only_other, gateway_rows[role.index()]);
            assert!(select(&only_other, role).unwrap().is_none());
            assert!(select(&only_other, other).unwrap().is_some());
            let [profile, lock, plan] = parse_records(&only_other, &budget);
            assert!(
                select_gateway_provider_health_records(
                    (&profile, &lock, &plan),
                    &budget,
                    ordinary_role,
                    false
                )
                .unwrap()
                .is_some()
            );

            let health_row = GATEWAY_PROVIDER_HEALTH_ROWS[ordinary_role.index()];
            let mut wrong_leaf = fixture.clone();
            set_grant_interactive_only(&mut wrong_leaf, health_row);
            assert!(select(&wrong_leaf, role).unwrap().is_none());
            assert!(select(&wrong_leaf, other).unwrap().is_some());
        }
    }

    #[test]
    fn reviewer_health_requires_exact_scope_dimension_keys_and_singleton_strings() {
        use OriginalReviewerHealthRole::{Generate, Quote};

        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let (fixture, _, gateway_rows) = reviewer_health_fixture(&budget);
        for (role, key, value) in [
            (Quote, "request_surface", json!(["ordinary-chat"])),
            (Quote, "tool_calling", json!([false])),
            (Quote, "allow_failover", json!(["false"])),
            (
                Generate,
                "request_surface",
                json!(["approval-review", "ordinary-chat"]),
            ),
            (Generate, "tool_calling", json!(["true"])),
            (Generate, "allow_failover", json!(["true"])),
        ] {
            let mut invalid = fixture.clone();
            let row = gateway_rows[role.index()];
            invalid["profile"]["requested_edges"][row]["requested_scope_template"]["dimensions"]
                [key] = value;
            assert!(matches!(
                select_reviewer_health(&invalid, &budget, role),
                Err(Inconsistent(_))
            ));
            sync_fixture_scope_digest(&mut invalid, row, &budget);
            assert!(
                select_reviewer_health(&invalid, &budget, role)
                    .unwrap()
                    .is_none()
            );
        }
        let mut missing = fixture;
        let row = gateway_rows[Generate.index()];
        missing["profile"]["requested_edges"][row]["requested_scope_template"]["dimensions"]
            .as_object_mut()
            .unwrap()
            .remove("allow_failover");
        sync_fixture_scope_digest(&mut missing, row, &budget);
        assert!(
            select_reviewer_health(&missing, &budget, Generate)
                .unwrap()
                .is_none()
        );
        assert!(
            select_reviewer_health(&missing, &budget, Quote)
                .unwrap()
                .is_some()
        );
    }

    #[test]
    fn workflow_provider_health_requires_each_gateways_own_generate_grant() {
        use OriginalWorkflowSnapshotCaller::{
            AudioSpeech, AudioTranscribe, Messages, MessagesAdapter, Text, TextAdapter,
        };

        let unsupported = [AudioSpeech, AudioTranscribe, MessagesAdapter, TextAdapter];
        assert!(
            select_workflow_health(&workflow_snapshot_records(&unsupported))
                .unwrap()
                .is_none()
        );

        let fixture = workflow_snapshot_records(&OriginalWorkflowSnapshotCaller::ALL);
        for (caller, gateway) in [(Text, &GATEWAY_TEXT), (Messages, &GATEWAY_MESSAGES)] {
            let mut candidate = fixture.clone();
            let edges = fixture["profile"]["requested_edges"].as_array().unwrap();
            for (row, edge) in edges.iter().enumerate() {
                if edge["operation_id"] == PROVIDER_READ_GENERATE.operation
                    || (edge["operation_id"] == PROVIDER_HEALTH_GENERATE.operation
                        && edge["caller_function_id"] != gateway.function)
                {
                    set_grant_interactive_only(&mut candidate, row);
                }
            }
            assert!(select_workflow_snapshot(&candidate).unwrap().is_none());
            let (_, callers) = select_workflow_health(&candidate).unwrap().unwrap();
            for row in OriginalWorkflowSnapshotCaller::ALL {
                assert_eq!(callers[row.index()].is_some(), row == caller);
            }
            let pins = callers[caller.index()].as_ref().unwrap();
            assert!(pins.prefix.is_none());
            assert_eq!(pins.caller.caller_function_id(), WORKFLOW_ADVANCE.function);
            assert_eq!(pins.caller.operation_id(), gateway.operation);
            assert_eq!(pins.registry.caller_function_id(), gateway.function);
            assert_eq!(
                pins.registry.operation_id(),
                PROVIDER_HEALTH_GENERATE.operation
            );
        }
    }

    #[test]
    fn workflow_provider_health_requires_the_exact_shell_entry_and_gateway_grants() {
        use OriginalWorkflowSnapshotCaller::Text;
        use OriginalWorkflowSnapshotEntry::{Advance, StepResume};

        let fixture = workflow_snapshot_records(&[Text]);
        let edges = fixture["profile"]["requested_edges"].as_array().unwrap();
        let gateway_row = edges
            .iter()
            .position(|edge| {
                edge["caller_function_id"] == WORKFLOW_ADVANCE.function
                    && edge["operation_id"] == GATEWAY_TEXT.operation
            })
            .unwrap();
        for root in [Advance, StepResume] {
            let mut candidate = fixture.clone();
            for (row, edge) in edges.iter().enumerate() {
                if edge["caller_function_id"] == TURN_SHELL_CALLER
                    && edge["target_provider_id"] == WORKFLOW_ADVANCE.function
                    && edge["operation_id"] != root.route().operation
                {
                    set_grant_interactive_only(&mut candidate, row);
                }
            }
            let (entries, _) = select_workflow_health(&candidate).unwrap().unwrap();
            for entry in OriginalWorkflowSnapshotEntry::ALL {
                assert_eq!(entries[entry.index()].is_some(), entry == root);
            }
            let pin = entries[root.index()].as_ref().unwrap();
            assert_eq!(pin.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(pin.function_id(), WORKFLOW_ADVANCE.function);
            assert_eq!(pin.operation_id(), root.route().operation);

            let root_row = edges
                .iter()
                .position(|edge| {
                    edge["caller_function_id"] == TURN_SHELL_CALLER
                        && edge["operation_id"] == root.route().operation
                })
                .unwrap();
            for revoked_row in [root_row, gateway_row] {
                let mut revoked = candidate.clone();
                set_grant_interactive_only(&mut revoked, revoked_row);
                assert!(select_workflow_health(&revoked).unwrap().is_none());
            }
        }
    }

    #[test]
    fn workflow_provider_snapshot_selects_each_entry_and_the_callers_own_nested_grants() {
        assert!(select_workflow_snapshot(&records()).unwrap().is_none());
        let (entries, callers) = select_workflow_snapshot(&workflow_snapshot_records(&[]))
            .unwrap()
            .unwrap();
        for entry in OriginalWorkflowSnapshotEntry::ALL {
            let pin = entries[entry.index()].as_ref().unwrap();
            assert_eq!(pin.caller_function_id(), "shell.tauri.default");
            assert_eq!(pin.function_id(), "tobkiri.workflow.provider");
            assert_eq!(pin.operation_id(), entry.route().operation);
        }
        for caller in OriginalWorkflowSnapshotCaller::ALL {
            let (prefix, route) = caller.routes();
            let pins = callers[caller.index()].as_ref().unwrap();
            assert_eq!(
                pins.prefix.as_ref().map(OriginalRoutePin::operation_id),
                prefix.map(|route| route.operation)
            );
            assert_eq!(
                pins.caller.caller_function_id(),
                prefix.unwrap_or(&WORKFLOW_ADVANCE).function
            );
            assert_eq!(pins.caller.operation_id(), route.operation);
            assert_eq!(pins.registry.caller_function_id(), route.function);
            assert_eq!(
                pins.registry.operation_id(),
                PROVIDER_READ_GENERATE.operation
            );
        }
    }

    #[test]
    fn workflow_provider_snapshot_requires_each_exact_profile_grant_and_scope() {
        let fixture = workflow_snapshot_records(&[]);
        let edges = fixture["profile"]["requested_edges"].as_array().unwrap();
        let first_workflow_edge = edges
            .iter()
            .position(|edge| edge["operation_id"] == WORKFLOW_ADVANCE.operation)
            .unwrap();
        for (row, edge) in edges.iter().enumerate().filter(|(row, edge)| {
            *row >= first_workflow_edge
                || (edge["operation_id"] == PROVIDER_READ_GENERATE.operation
                    && edge["caller_function_id"] == PROVIDER_GENERATE.function)
        }) {
            let mut candidate = fixture.clone();
            candidate["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            assert!(matches!(
                select_workflow_snapshot(&candidate),
                Err(Inconsistent(_))
            ));
            candidate["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            let (entries, callers) = select_workflow_snapshot(&candidate).unwrap().unwrap();
            for entry in OriginalWorkflowSnapshotEntry::ALL {
                assert_eq!(
                    entries[entry.index()].is_none(),
                    edge["operation_id"] == entry.route().operation,
                );
            }
            for caller in OriginalWorkflowSnapshotCaller::ALL {
                let (prefix, route) = caller.routes();
                assert_eq!(
                    callers[caller.index()].is_none(),
                    (edge["operation_id"] == route.operation
                        && edge["caller_function_id"]
                            == prefix.unwrap_or(&WORKFLOW_ADVANCE).function)
                        || prefix.is_some_and(|route| edge["operation_id"] == route.operation
                            && edge["caller_function_id"] == WORKFLOW_ADVANCE.function)
                        || (edge["operation_id"] == PROVIDER_READ_GENERATE.operation
                            && edge["caller_function_id"] == route.function),
                );
            }
        }

        let row = edges
            .iter()
            .position(|edge| {
                edge["operation_id"] == PROVIDER_READ_GENERATE.operation
                    && edge["caller_function_id"] == ADAPTER_SPEECH.function
            })
            .unwrap();
        let mut candidate = fixture;
        let scope = &mut candidate["profile"]["requested_edges"][row]["requested_scope_template"];
        scope["dimensions"]["operation"] = json!([PROVIDER_READ_GENERATE.operation, "extra"]);
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let digest = CoreValue::parse_guest(&serde_json::to_vec(&scope).unwrap(), &budget)
            .unwrap()
            .digest_guest(&budget)
            .unwrap();
        candidate["plan"]["bindings"][row]["requested_scope_digest"] = json!(digest.as_str());
        let (_, callers) = select_workflow_snapshot(&candidate).unwrap().unwrap();
        assert_eq!(
            callers.map(|role| role.is_some()),
            [true, true, true, false, true, true]
        );
    }

    #[test]
    fn workflow_provider_snapshot_adapter_requires_the_same_gateway_on_both_edges() {
        use OriginalWorkflowSnapshotCaller::{MessagesAdapter, TextAdapter};
        use OriginalWorkflowSnapshotEntry::StepResume;

        let fixture = workflow_snapshot_records(&[]);
        let edges = fixture["profile"]["requested_edges"].as_array().unwrap();
        let first_workflow_edge = edges
            .iter()
            .position(|edge| edge["operation_id"] == WORKFLOW_ADVANCE.operation)
            .unwrap();
        for (workflow_gateway, adapter_gateway, expected) in [
            (&GATEWAY_TEXT, &GATEWAY_TEXT, Some(TextAdapter)),
            (&GATEWAY_MESSAGES, &GATEWAY_MESSAGES, Some(MessagesAdapter)),
            (&GATEWAY_TEXT, &GATEWAY_MESSAGES, None),
            (&GATEWAY_MESSAGES, &GATEWAY_TEXT, None),
        ] {
            let mut candidate = fixture.clone();
            for (row, edge) in edges.iter().enumerate().skip(first_workflow_edge) {
                let keep = edge["operation_id"] == StepResume.route().operation
                    || (edge["caller_function_id"] == WORKFLOW_ADVANCE.function
                        && edge["operation_id"] == workflow_gateway.operation)
                    || (edge["caller_function_id"] == adapter_gateway.function
                        && edge["operation_id"] == PROVIDER_GENERATE.operation);
                if !keep {
                    candidate["profile"]["requested_edges"][row]["authority_mode"] =
                        json!("interactive_only");
                    candidate["plan"]["bindings"][row]["authority_mode"] =
                        json!("interactive_only");
                }
            }
            let selection = select_workflow_snapshot(&candidate).unwrap();
            if let Some(expected) = expected {
                let (entries, callers) = selection.unwrap();
                for entry in OriginalWorkflowSnapshotEntry::ALL {
                    assert_eq!(entries[entry.index()].is_some(), entry == StepResume);
                }
                for caller in OriginalWorkflowSnapshotCaller::ALL {
                    assert_eq!(callers[caller.index()].is_some(), caller == expected);
                }
            } else {
                assert!(selection.is_none());
            }
        }
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

    #[test]
    fn presentation_requires_each_roles_exact_ordinary_parent_and_nested_read() {
        let fixture = records();
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let select = |fixture: &Value, role| {
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_application_presentation_records((&profile, &lock, &plan), &budget, role)
        };
        for role in ApplicationPresentationRole::ALL {
            let [caller, presentation] = select(&fixture, role).unwrap().unwrap();
            assert_eq!(caller.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(caller.operation_id(), role.caller().operation);
            assert_eq!(presentation.caller_function_id(), caller.function_id());
            assert_eq!(
                presentation.operation_id(),
                "defaultspack.presentation.read"
            );
            assert_eq!(presentation.execution_kind(), "pack_vm");
            for pin in [&caller, &presentation] {
                let row = fixture["profile"]["requested_edges"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .position(|edge| edge["authority_reference"] == pin.authority_reference())
                    .unwrap();
                let mut wrong_caller = fixture.clone();
                wrong_caller["profile"]["requested_edges"][row]["caller_function_id"] =
                    json!("unrelated.call");
                wrong_caller["plan"]["bindings"][row]["caller_function_id"] =
                    json!("unrelated.call");
                assert!(select(&wrong_caller, role).unwrap().is_none());
                for unrelated in ApplicationPresentationRole::ALL {
                    if unrelated != role {
                        assert!(select(&wrong_caller, unrelated).unwrap().is_some());
                    }
                }

                let mut interactive = fixture.clone();
                interactive["profile"]["requested_edges"][row]["authority_mode"] =
                    json!("interactive_only");
                interactive["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
                assert!(select(&interactive, role).unwrap().is_none());
            }
        }
    }

    #[test]
    fn conversation_complete_requires_exact_shell_application_and_generate_grants() {
        let fixture = records();
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let select = |fixture: &Value| {
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_conversation_complete_records((&profile, &lock, &plan), &budget)
        };
        let proof = select(&fixture).unwrap().unwrap();
        let [conversation, gateway] = &proof.pins;
        assert_eq!(conversation.caller_function_id(), "shell.tauri.default");
        assert_eq!(conversation.pack_id(), "defaultspack");
        assert_eq!(conversation.operation_id(), "complete");
        assert_eq!(conversation.execution_kind(), "pack_vm");
        assert_eq!(gateway.caller_function_id(), conversation.function_id());
        assert_eq!(gateway.operation_id(), GATEWAY_GENERATE.operation);
        assert_eq!(gateway.execution_kind(), "host_extension");

        for pin in &proof.pins {
            let row = fixture["profile"]["requested_edges"]
                .as_array()
                .unwrap()
                .iter()
                .position(|edge| edge["authority_reference"] == pin.authority_reference())
                .unwrap();
            let mut absent = fixture.clone();
            absent["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            absent["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(select(&absent).unwrap().is_none());

            let mut interactive = fixture.clone();
            interactive["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            interactive["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            assert!(select(&interactive).unwrap().is_none());

            let mut revised = fixture.clone();
            let scope = &mut revised["profile"]["requested_edges"][row]["requested_scope_template"];
            let digest = Digest::hash(b"different contract revision");
            scope["semantics_digest"] = json!(digest.as_str());
            let bytes = serde_json::to_vec(scope).unwrap();
            let scope = CoreValue::parse_guest(&bytes, &budget).unwrap();
            let binding = &mut revised["plan"]["bindings"][row];
            binding["function_principal"]["contract_revision_digest"] = json!(digest.as_str());
            binding["requested_scope_digest"] =
                json!(scope.digest_guest(&budget).unwrap().as_str());
            assert!(select(&revised).unwrap().is_none());
        }

        let mut wrong_app_pin = fixture.clone();
        wrong_app_pin["lock"]["application"]["artifact_digest"] =
            json!(Digest::hash(b"other application").as_str());
        assert!(select(&wrong_app_pin).is_err());
        let mut wrong_shell_pack = fixture.clone();
        for record in ["profile", "lock", "plan"] {
            wrong_shell_pack[record]["shell"]["pack_id"] = json!("unrelated.shell-pack");
        }
        assert!(select(&wrong_shell_pack).unwrap().is_none());
    }

    #[test]
    fn conversation_complete_health_requires_its_exact_generate_grant_and_complete_root() {
        let fixture = records();
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let select = |fixture: &Value| {
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_conversation_complete_health_records((&profile, &lock, &plan), &budget)
        };
        let select_complete = |fixture: &Value| {
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_conversation_complete_records((&profile, &lock, &plan), &budget)
        };
        let select_saved_health = |fixture: &Value| {
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_gateway_provider_health_records(
                (&profile, &lock, &plan),
                &budget,
                OriginalModelResolveRole::Generate,
                false,
            )
        };
        let (complete, health) = select(&fixture).unwrap().unwrap();
        let [conversation, gateway] = &complete.pins;
        assert_eq!(conversation.caller_function_id(), TURN_SHELL_CALLER);
        assert_eq!(conversation.operation_id(), CONVERSATION_COMPLETE.operation);
        assert_eq!(gateway.caller_function_id(), conversation.function_id());
        assert_eq!(gateway.function_id(), GATEWAY_GENERATE.function);
        assert_eq!(health.caller_function_id(), gateway.function_id());
        assert_eq!(health.operation_id(), PROVIDER_HEALTH_GENERATE.operation);

        let edges = fixture["profile"]["requested_edges"].as_array().unwrap();
        let row = |caller: &str, route: &Route| {
            edges
                .iter()
                .position(|edge| {
                    edge["caller_function_id"] == caller && edge["operation_id"] == route.operation
                })
                .unwrap()
        };
        let health_row = row(gateway.function_id(), &PROVIDER_HEALTH_GENERATE);
        let mut missing = fixture.clone();
        missing["profile"]["requested_edges"]
            .as_array_mut()
            .unwrap()
            .remove(health_row);
        missing["plan"]["bindings"]
            .as_array_mut()
            .unwrap()
            .remove(health_row);
        assert!(select(&missing).unwrap().is_none());
        assert!(select_complete(&missing).unwrap().is_some());
        add_fixture_grant(
            &mut missing,
            gateway.function_id(),
            &PROVIDER_HEALTH_STREAM,
            &budget,
        );
        let [profile, lock, plan] = parse_records(&missing, &budget);
        let stream_records = selected_shell_records((&profile, &lock, &plan), &budget)
            .unwrap()
            .unwrap();
        assert!(
            stream_records
                .select_route(gateway.function_id(), &PROVIDER_HEALTH_STREAM)
                .unwrap()
                .is_some()
        );
        assert!(select(&missing).unwrap().is_none());
        assert!(select_complete(&missing).unwrap().is_some());

        let mut mismatched = fixture.clone();
        mismatched["plan"]["bindings"][health_row]["authority_mode"] = json!("interactive_only");
        assert!(matches!(select(&mismatched), Err(Inconsistent(_))));
        assert!(select_complete(&mismatched).unwrap().is_some());

        for (caller, route) in [
            (TURN_SHELL_CALLER, &CONVERSATION_COMPLETE),
            (CONVERSATION_COMPLETE.function, &GATEWAY_GENERATE),
        ] {
            let mut saved_only = fixture.clone();
            set_grant_interactive_only(&mut saved_only, row(caller, route));
            assert!(select(&saved_only).unwrap().is_none());
            assert!(select_saved_health(&saved_only).unwrap().is_some());
        }
        let saved_turn_row = row(TURN_SHELL_CALLER, &SAVED_TURN);
        let mut complete_only = fixture;
        set_grant_interactive_only(&mut complete_only, saved_turn_row);
        assert!(select_saved_health(&complete_only).unwrap().is_none());
        assert!(select(&complete_only).unwrap().is_some());
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
    ) -> Result<Option<ConversationConfirmationPins>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_conversation_confirmation_records((&profile, &lock, &plan), &budget, role)
    }

    fn select_append(records: &Value) -> Result<Option<[OriginalRoutePin; 3]>> {
        Ok(select_append_for(records, ConversationAppendCaller::SavedTurn)?.map(|(_, pins)| pins))
    }

    fn select_append_for(
        records: &Value,
        caller: ConversationAppendCaller,
    ) -> Result<Option<ConversationAppendPins>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_conversation_append_records((&profile, &lock, &plan), &budget, caller)
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
    const SHELL_PROVIDER_READ_ROW: usize = 5;
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
    const TOOL_INTENTS_GENERATE_ROW: usize = 36;
    const TOOL_INTENTS_STREAM_ROW: usize = 37;
    const REQUEST_PREPARE_ROWS: std::ops::Range<usize> = 38..42;
    const FAILOVER_GENERATE_ROW: usize = 42;
    const FAILOVER_STREAM_ROW: usize = 43;
    const SIDE_CHAT_TURN_ROW: usize = 52;
    const SIDE_CHAT_SAVED_TURN_ROW: usize = 53;
    const GATEWAY_PROVIDER_SNAPSHOT_ROWS: std::ops::Range<usize> = 55..59;
    const GATEWAY_PROVIDER_HEALTH_ROWS: [usize; 4] = [65, 66, 63, 64];

    fn select_saved_local_chat(
        records: &Value,
        role: OriginalSavedLocalChatRole,
    ) -> Result<Option<SavedLocalChatPins>> {
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
    fn saved_gateway_selects_only_the_three_signed_edges_for_its_role() {
        use OriginalModelResolveRole::{Generate, QuoteGenerate, QuoteStream, Stream};

        let select = |fixture: &Value, role| {
            let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_saved_gateway_records((&profile, &lock, &plan), &budget, role)
        };
        let fixture = records();
        for role in OriginalModelResolveRole::ALL {
            let [turn, orchestration, gateway] = select(&fixture, role).unwrap().unwrap();
            assert_eq!(turn.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(turn.function_id(), SAVED_TURN.function);
            assert_eq!(orchestration.caller_function_id(), turn.function_id());
            assert_eq!(orchestration.function_id(), SAVED_ORCHESTRATION.function);
            assert_eq!(gateway.caller_function_id(), orchestration.function_id());
            assert_eq!(gateway.function_id(), role.gateway_route().function);
            assert_eq!(gateway.operation_id(), role.gateway_route().operation);
        }

        for (row, unavailable) in [
            (LOCAL_CHAT_GENERATE_ROWS[0], &[Generate][..]),
            (LOCAL_CHAT_STREAM_ROWS[0], &[Stream][..]),
            (GATEWAY_QUOTE_ROW, &[QuoteGenerate, QuoteStream][..]),
            (
                SAVED_TURN_SETTLEMENT_ROW,
                &OriginalModelResolveRole::ALL[..],
            ),
            (SAVED_ORCHESTRATION_ROW, &OriginalModelResolveRole::ALL[..]),
        ] {
            let mut candidate = fixture.clone();
            candidate["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            for role in unavailable {
                assert!(matches!(select(&candidate, *role), Err(Inconsistent(_))));
            }
            candidate["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            for role in OriginalModelResolveRole::ALL {
                assert_eq!(
                    select(&candidate, role).unwrap().is_none(),
                    unavailable.contains(&role)
                );
            }
        }

        let mut only_gateway_grants = fixture;
        let needed = [
            SAVED_TURN_SETTLEMENT_ROW,
            SAVED_ORCHESTRATION_ROW,
            LOCAL_CHAT_GENERATE_ROWS[0],
            LOCAL_CHAT_STREAM_ROWS[0],
            GATEWAY_QUOTE_ROW,
        ];
        for (field, edges) in [("profile", "requested_edges"), ("plan", "bindings")] {
            let edges = only_gateway_grants[field][edges].as_array_mut().unwrap();
            for row in (0..edges.len()).rev().filter(|row| !needed.contains(row)) {
                edges.remove(row);
            }
        }
        for role in OriginalModelResolveRole::ALL {
            assert!(select(&only_gateway_grants, role).unwrap().is_some());
        }
    }

    #[test]
    fn usage_cost_requires_only_its_own_exact_ordinary_gateway_chain() {
        assert_exact_ordinary_gateway_chains(
            [&AI_USAGE_COST_GENERATE, &AI_USAGE_COST_STREAM],
            [USAGE_COST_GENERATE_ROW, USAGE_COST_STREAM_ROW],
            GATEWAY_QUOTE_ROW..=STREAM_RESULT_ROW,
        );
    }

    #[test]
    fn tool_intents_require_only_their_own_exact_ordinary_gateway_chain() {
        assert_exact_ordinary_gateway_chains(
            [&AI_TOOL_INTENTS_GENERATE, &AI_TOOL_INTENTS_STREAM],
            [TOOL_INTENTS_GENERATE_ROW, TOOL_INTENTS_STREAM_ROW],
            GATEWAY_QUOTE_ROW..=USAGE_COST_STREAM_ROW,
        );
    }

    #[test]
    fn request_prepare_grants_require_each_exact_gateway_role_and_operation() {
        let select = |fixture: &Value, role| {
            let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_request_prepare_records((&profile, &lock, &plan), &budget, role)
        };
        let fixture = records();
        for ((role, child_row), (gateway_row, child_route)) in OriginalModelResolveRole::ALL
            .into_iter()
            .zip(REQUEST_PREPARE_ROWS)
            .zip([
                (GATEWAY_QUOTE_ROW, &AI_REQUEST_PREPARE_GENERATE),
                (GATEWAY_QUOTE_ROW, &AI_REQUEST_PREPARE_STREAM),
                (LOCAL_CHAT_GENERATE_ROWS[0], &AI_REQUEST_PREPARE_GENERATE),
                (LOCAL_CHAT_STREAM_ROWS[0], &AI_REQUEST_PREPARE_STREAM),
            ])
        {
            let [turn, orchestration, gateway, child] = select(&fixture, role).unwrap().unwrap();
            assert_eq!(turn.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(turn.function_id(), SAVED_TURN.function);
            assert_eq!(orchestration.caller_function_id(), turn.function_id());
            assert_eq!(orchestration.function_id(), SAVED_ORCHESTRATION.function);
            assert_eq!(gateway.caller_function_id(), orchestration.function_id());
            assert_eq!(gateway.function_id(), role.gateway_route().function);
            assert_eq!(child.caller_function_id(), gateway.function_id());
            assert_eq!(child.function_id(), child_route.function);
            assert_eq!(child.contract_id(), child_route.contract);
            assert_eq!(child.operation_id(), child_route.operation);

            for row in [
                SAVED_TURN_SETTLEMENT_ROW,
                SAVED_ORCHESTRATION_ROW,
                gateway_row,
                child_row,
            ] {
                let mut changed = fixture.clone();
                changed["profile"]["requested_edges"][row]["authority_mode"] =
                    json!("interactive_only");
                assert!(matches!(select(&changed, role), Err(Inconsistent(_))));
                changed["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
                assert!(select(&changed, role).unwrap().is_none());
                if row == child_row {
                    for sibling in OriginalModelResolveRole::ALL {
                        assert_eq!(
                            select(&changed, sibling).unwrap().is_some(),
                            sibling != role
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn failover_grant_requires_the_generate_leaf_even_with_a_signed_stream_leaf() {
        let select = |fixture: &Value| {
            let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_failover_records((&profile, &lock, &plan), &budget)
        };
        let mut fixture = records();
        let [_, _, gateway, failover] = select(&fixture).unwrap().unwrap();
        assert_eq!(gateway.function_id(), GATEWAY_GENERATE_CALLER);
        assert_eq!(failover.caller_function_id(), gateway.function_id());
        assert_eq!(failover.function_id(), AI_FAILOVER_GENERATE.function);
        assert_eq!(failover.contract_id(), AI_FAILOVER_GENERATE.contract);
        assert_eq!(failover.operation_id(), AI_FAILOVER_GENERATE.operation);

        assert_eq!(
            fixture["plan"]["bindings"][FAILOVER_STREAM_ROW]["operation_id"],
            AI_FAILOVER_STREAM.operation
        );
        fixture["profile"]["requested_edges"][FAILOVER_GENERATE_ROW]["authority_mode"] =
            json!("interactive_only");
        assert!(matches!(select(&fixture), Err(Inconsistent(_))));
        fixture["plan"]["bindings"][FAILOVER_GENERATE_ROW]["authority_mode"] =
            json!("interactive_only");
        assert!(select(&fixture).unwrap().is_none());
    }

    fn assert_exact_ordinary_gateway_chains(
        [generate, stream]: [&'static Route; 2],
        [generate_row, stream_row]: [usize; 2],
        unrelated_rows: std::ops::RangeInclusive<usize>,
    ) {
        use OriginalModelResolveRole::{Generate, QuoteGenerate, QuoteStream, Stream};

        let select = |records: &Value, role| {
            let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
            let [profile, lock, plan] = parse_records(records, &budget);
            select_saved_role_records((&profile, &lock, &plan), &budget, role, [generate, stream])
        };
        let fixture = records();
        assert!(select(&fixture, QuoteGenerate).unwrap().is_none());
        assert!(select(&fixture, QuoteStream).unwrap().is_none());
        for (role, other, gateway_row, child_row, child_route) in [
            (
                Generate,
                Stream,
                LOCAL_CHAT_GENERATE_ROWS[0],
                generate_row,
                generate,
            ),
            (
                Stream,
                Generate,
                LOCAL_CHAT_STREAM_ROWS[0],
                stream_row,
                stream,
            ),
        ] {
            let [turn, orchestration, gateway, child] = select(&fixture, role).unwrap().unwrap();
            assert_eq!(turn.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(turn.function_id(), SAVED_TURN.function);
            assert_eq!(orchestration.caller_function_id(), turn.function_id());
            assert_eq!(orchestration.function_id(), SAVED_ORCHESTRATION.function);
            assert_eq!(gateway.caller_function_id(), orchestration.function_id());
            assert_eq!(gateway.function_id(), role.gateway_route().function);
            assert_eq!(child.caller_function_id(), gateway.function_id());
            assert_eq!(child.function_id(), child_route.function);
            assert_eq!(child.contract_id(), child_route.contract);
            assert_eq!(child.operation_id(), child_route.operation);

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

            let mut candidate = fixture.clone();
            candidate["plan"]["bindings"][child_row]["requested_scope_digest"] =
                json!(Digest::hash(b"other scope").as_str());
            assert!(matches!(select(&candidate, role), Err(Inconsistent(_))));
            assert!(select(&candidate, other).unwrap().is_some());
        }
        let mut unrelated = fixture.clone();
        for row in unrelated_rows.rev() {
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
    fn gateway_health_requires_each_saved_entry_and_its_exact_grant() {
        use OriginalModelResolveRole::{Generate, QuoteGenerate, QuoteStream, Stream};

        let select = |fixture: &Value, role, via_side_chat| {
            let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_gateway_provider_health_records(
                (&profile, &lock, &plan),
                &budget,
                role,
                via_side_chat,
            )
        };
        let gateway_row = |role| match role {
            QuoteGenerate | QuoteStream => GATEWAY_QUOTE_ROW,
            Generate => LOCAL_CHAT_GENERATE_ROWS[0],
            Stream => LOCAL_CHAT_STREAM_ROWS[0],
        };
        let fixture = records();
        let roles = || {
            [false, true].into_iter().flat_map(|via_side_chat| {
                OriginalModelResolveRole::ALL.map(|role| (role, via_side_chat))
            })
        };
        for (role, via_side_chat) in roles() {
            let health = match role {
                QuoteGenerate | Generate => &PROVIDER_HEALTH_GENERATE,
                QuoteStream | Stream => &PROVIDER_HEALTH_STREAM,
            };
            let health_row = GATEWAY_PROVIDER_HEALTH_ROWS[role.index()];
            let (side_chat, [turn, orchestration, gateway_pin, child]) =
                select(&fixture, role, via_side_chat).unwrap().unwrap();
            assert_eq!(side_chat.is_some(), via_side_chat);
            let turn_caller = side_chat.as_ref().map_or(TURN_SHELL_CALLER, |pin| {
                assert_eq!(pin.caller_function_id(), TURN_SHELL_CALLER);
                assert_eq!(pin.function_id(), SIDE_CHAT_TURN.function);
                assert_eq!(pin.operation_id(), SIDE_CHAT_TURN.operation);
                pin.function_id()
            });
            assert_eq!(turn.caller_function_id(), turn_caller);
            assert_eq!(turn.function_id(), SAVED_TURN.function);
            assert_eq!(orchestration.caller_function_id(), turn.function_id());
            assert_eq!(orchestration.function_id(), SAVED_ORCHESTRATION.function);
            assert_eq!(
                gateway_pin.caller_function_id(),
                orchestration.function_id()
            );
            assert_eq!(gateway_pin.function_id(), role.gateway_route().function);
            assert_eq!(child.caller_function_id(), gateway_pin.function_id());
            assert_eq!(child.function_id(), health.function);
            assert_eq!(child.contract_id(), health.contract);
            assert_eq!(child.operation_id(), health.operation);

            let entry_rows = if via_side_chat {
                &[SIDE_CHAT_TURN_ROW, SIDE_CHAT_SAVED_TURN_ROW][..]
            } else {
                &[SAVED_TURN_SETTLEMENT_ROW][..]
            };
            for row in entry_rows.iter().copied().chain([
                SAVED_ORCHESTRATION_ROW,
                gateway_row(role),
                health_row,
            ]) {
                let mut denied = fixture.clone();
                denied["profile"]["requested_edges"][row]["authority_mode"] =
                    json!("interactive_only");
                assert!(matches!(
                    select(&denied, role, via_side_chat),
                    Err(Inconsistent(_))
                ));
                denied["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
                for (candidate, candidate_side_chat) in roles() {
                    let denied_by_row = if entry_rows.contains(&row) {
                        candidate_side_chat == via_side_chat
                    } else {
                        row == SAVED_ORCHESTRATION_ROW
                            || row == gateway_row(candidate)
                            || row == GATEWAY_PROVIDER_HEALTH_ROWS[candidate.index()]
                    };
                    assert_eq!(
                        select(&denied, candidate, candidate_side_chat)
                            .unwrap()
                            .is_none(),
                        denied_by_row,
                        "{candidate:?}, side chat {candidate_side_chat}, disabled row {row}"
                    );
                }
            }
        }

        let mut no_raw_snapshot = fixture;
        for row in GATEWAY_PROVIDER_SNAPSHOT_ROWS {
            no_raw_snapshot["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            no_raw_snapshot["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
        }
        for (role, via_side_chat) in roles() {
            assert!(
                select(&no_raw_snapshot, role, via_side_chat)
                    .unwrap()
                    .is_some()
            );
        }
    }

    #[test]
    fn gateway_provider_snapshot_requires_its_own_registry_grant_and_saved_entry() {
        let select = |fixture: &Value, role, via_side_chat| {
            let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_gateway_provider_snapshot_records(
                (&profile, &lock, &plan),
                &budget,
                role,
                via_side_chat,
            )
        };
        let fixture = records();
        for (role, row) in OriginalModelResolveRole::ALL
            .into_iter()
            .zip(GATEWAY_PROVIDER_SNAPSHOT_ROWS)
        {
            for via_side_chat in [false, true] {
                let (side_chat, [turn, orchestration, gateway, registry]) =
                    select(&fixture, role, via_side_chat).unwrap().unwrap();
                assert_eq!(side_chat.is_some(), via_side_chat);
                let caller = side_chat.as_ref().map_or(TURN_SHELL_CALLER, |pin| {
                    assert_eq!(pin.caller_function_id(), TURN_SHELL_CALLER);
                    assert_eq!(pin.function_id(), SIDE_CHAT_TURN.function);
                    pin.function_id()
                });
                assert_eq!(turn.caller_function_id(), caller);
                assert_eq!(turn.function_id(), SAVED_TURN.function);
                assert_eq!(orchestration.caller_function_id(), turn.function_id());
                assert_eq!(orchestration.function_id(), SAVED_ORCHESTRATION.function);
                assert_eq!(gateway.caller_function_id(), orchestration.function_id());
                assert_eq!(gateway.function_id(), role.gateway_route().function);
                assert_eq!(registry.caller_function_id(), gateway.function_id());
                let binding = &fixture["plan"]["bindings"][row];
                assert_eq!(registry.operation_id(), binding["operation_id"]);
                assert_eq!(
                    registry.authority_reference(),
                    binding["authority_reference"]
                );
            }
            let mut no_child = fixture.clone();
            no_child["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            assert!(matches!(
                select(&no_child, role, false),
                Err(Inconsistent(_))
            ));
            no_child["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            for sibling in OriginalModelResolveRole::ALL {
                for via_side_chat in [false, true] {
                    assert_eq!(
                        select(&no_child, sibling, via_side_chat).unwrap().is_none(),
                        sibling == role
                    );
                }
            }
        }

        let mut no_side_chat = fixture.clone();
        no_side_chat["profile"]["requested_edges"][SIDE_CHAT_TURN_ROW]["authority_mode"] =
            json!("interactive_only");
        no_side_chat["plan"]["bindings"][SIDE_CHAT_TURN_ROW]["authority_mode"] =
            json!("interactive_only");
        for role in OriginalModelResolveRole::ALL {
            assert!(select(&no_side_chat, role, false).unwrap().is_some());
            assert!(select(&no_side_chat, role, true).unwrap().is_none());
        }

        let mut no_adapter = fixture;
        for [_, _, row] in [LOCAL_CHAT_GENERATE_ROWS, LOCAL_CHAT_STREAM_ROWS] {
            no_adapter["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            no_adapter["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
        }
        for role in OriginalModelResolveRole::ALL {
            assert!(select(&no_adapter, role, false).unwrap().is_some());
        }
        assert!(
            select_saved_local_chat(&no_adapter, OriginalSavedLocalChatRole::Generate)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn saved_local_chat_selects_exact_grants_for_each_role() {
        use OriginalSavedLocalChatRole::{Generate, SideChatGenerate, SideChatStream, Stream};

        let fixture = records();
        for (role, [gateway, adapter, registry]) in [
            (Generate, LOCAL_CHAT_GENERATE_ROWS),
            (Stream, LOCAL_CHAT_STREAM_ROWS),
            (SideChatGenerate, LOCAL_CHAT_GENERATE_ROWS),
            (SideChatStream, LOCAL_CHAT_STREAM_ROWS),
        ] {
            let (side_chat, pins) = select_saved_local_chat(&fixture, role).unwrap().unwrap();
            let rows = [
                if role.is_side_chat() {
                    SIDE_CHAT_SAVED_TURN_ROW
                } else {
                    SAVED_TURN_SETTLEMENT_ROW
                },
                SAVED_ORCHESTRATION_ROW,
                gateway,
                adapter,
                registry,
            ];
            let mut caller = TURN_SHELL_CALLER;
            assert_eq!(side_chat.is_some(), role.is_side_chat());
            for (pin, row) in side_chat
                .iter()
                .map(|pin| (pin, SIDE_CHAT_TURN_ROW))
                .chain(pins.iter().zip(rows))
            {
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
    fn saved_local_chat_requires_its_own_entry_and_shared_orchestration() {
        use OriginalSavedLocalChatRole::{Generate, SideChatGenerate, SideChatStream, Stream};

        let fixture = records();
        for (row, denied) in [
            (SAVED_TURN_SETTLEMENT_ROW, [true, true, false, false]),
            (SAVED_ORCHESTRATION_ROW, [true, true, true, true]),
            (SIDE_CHAT_TURN_ROW, [false, false, true, true]),
            (SIDE_CHAT_SAVED_TURN_ROW, [false, false, true, true]),
        ] {
            let mut candidate = fixture.clone();
            candidate["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            candidate["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            for (role, denied) in [Generate, Stream, SideChatGenerate, SideChatStream]
                .into_iter()
                .zip(denied)
            {
                assert_eq!(
                    select_saved_local_chat(&candidate, role).unwrap().is_none(),
                    denied,
                    "binding {row}, role {role:?}"
                );
            }
        }
    }

    #[test]
    fn saved_local_chat_requires_each_operation_grant_without_role_substitution() {
        use OriginalSavedLocalChatRole::{Generate, SideChatGenerate, SideChatStream, Stream};

        let fixture = records();
        for (denied, allowed, rows) in [
            (
                [Generate, SideChatGenerate],
                [Stream, SideChatStream],
                LOCAL_CHAT_GENERATE_ROWS,
            ),
            (
                [Stream, SideChatStream],
                [Generate, SideChatGenerate],
                LOCAL_CHAT_STREAM_ROWS,
            ),
        ] {
            for row in rows {
                let mut candidate = fixture.clone();
                candidate["profile"]["requested_edges"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                for role in denied {
                    assert!(matches!(
                        select_saved_local_chat(&candidate, role),
                        Err(Inconsistent(_))
                    ));
                }
                candidate["plan"]["bindings"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                for role in denied {
                    assert!(select_saved_local_chat(&candidate, role).unwrap().is_none());
                }
                for role in allowed {
                    assert!(select_saved_local_chat(&candidate, role).unwrap().is_some());
                }
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
        Ok(
            select_saved_turn_start_for(records, SavedTurnStartCaller::SavedTurn)?
                .map(|(_, pins)| pins),
        )
    }

    fn select_saved_turn_start_for(
        records: &Value,
        caller: SavedTurnStartCaller,
    ) -> Result<Option<SavedTurnStartPins>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_saved_turn_start_records((&profile, &lock, &plan), &budget, caller)
    }

    #[test]
    fn calendar_saved_input_requires_a_complete_scheduler_clock_chain() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let select = |fixture: &Value| {
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_calendar_saved_input_records((&profile, &lock, &plan), &budget)
        };
        let mut fixture = records();
        assert!(select(&fixture).unwrap().is_none());
        let first_row = fixture["plan"]["bindings"].as_array().unwrap().len();
        let chain = [
            (SCHEDULER_CLOCK_CALLER, &SCHEDULER_CONTROL),
            (SCHEDULER_CONTROL.function, &JOB_ACTION_BROKER),
            (JOB_ACTION_BROKER.function, &CHAT_SAVED_JOB_ADAPTER),
            (CHAT_SAVED_JOB_ADAPTER.function, &TURN_LIFECYCLE),
        ];
        for (caller, route) in chain {
            add_fixture_grant(&mut fixture, caller, route, &budget);
        }
        for (pin, (caller, route)) in select(&fixture).unwrap().unwrap().iter().zip(chain) {
            assert_eq!(pin.caller_function_id(), caller);
            assert_eq!(pin.function_id(), route.function);
            assert_eq!(pin.operation_id(), route.operation);
        }
        for row in first_row..(first_row + chain.len()) {
            let mut interactive = fixture.clone();
            set_grant_interactive_only(&mut interactive, row);
            assert!(select(&interactive).unwrap().is_none(), "grant {row}");

            let mut missing = fixture.clone();
            missing["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(matches!(select(&missing), Err(Inconsistent(_))));
            missing["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(select(&missing).unwrap().is_none(), "grant {row}");
        }

        let mut manual = fixture;
        manual["profile"]["requested_edges"]
            .as_array_mut()
            .unwrap()
            .remove(first_row);
        manual["plan"]["bindings"]
            .as_array_mut()
            .unwrap()
            .remove(first_row);
        add_fixture_grant(
            &mut manual,
            "rumi_schedule_store_pack.calendar.manage",
            &SCHEDULER_CONTROL,
            &budget,
        );
        assert!(select(&manual).unwrap().is_none());
        assert!(select_saved_turn_start(&manual).unwrap().is_some());
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

    #[test]
    fn side_chat_start_requires_its_own_entry_and_shared_lifecycle_grants() {
        let fixture = records();
        let side_start =
            |records: &Value| select_saved_turn_start_for(records, SavedTurnStartCaller::SideChat);
        let (side_chat, [turn, lifecycle]) = side_start(&fixture).unwrap().unwrap();
        let side_chat = side_chat.unwrap();
        assert_eq!(side_chat.caller_function_id(), TURN_SHELL_CALLER);
        assert_eq!(side_chat.function_id(), SIDE_CHAT_TURN.function);
        assert_eq!(turn.caller_function_id(), side_chat.function_id());
        assert_eq!(turn.function_id(), SAVED_TURN.function);
        assert_eq!(lifecycle.caller_function_id(), turn.function_id());
        assert_eq!(lifecycle.function_id(), TURN_LIFECYCLE.function);
        assert_eq!(lifecycle.artifact_digest(), turn.artifact_digest());

        let edges = fixture["profile"]["requested_edges"].as_array().unwrap();
        for pin in [&side_chat, &turn, &lifecycle] {
            let row = edges
                .iter()
                .position(|edge| edge["authority_reference"] == pin.authority_reference())
                .unwrap();
            let mut interactive = fixture.clone();
            interactive["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            interactive["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            assert!(side_start(&interactive).unwrap().is_none());
            assert_eq!(
                select_saved_turn_start(&interactive).unwrap().is_none(),
                row == TURN_LIFECYCLE_ROW
            );

            let mut absent = fixture.clone();
            absent["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(matches!(side_start(&absent), Err(Inconsistent(_))));
            absent["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(side_start(&absent).unwrap().is_none());
        }

        let mut side_chat_only = fixture;
        side_chat_only["profile"]["requested_edges"]
            .as_array_mut()
            .unwrap()
            .remove(SAVED_TURN_SETTLEMENT_ROW);
        side_chat_only["plan"]["bindings"]
            .as_array_mut()
            .unwrap()
            .remove(SAVED_TURN_SETTLEMENT_ROW);
        assert!(side_start(&side_chat_only).unwrap().is_some());
        assert!(select_saved_turn_start(&side_chat_only).unwrap().is_none());
    }

    fn select_saved_turn_settlement(
        records: &Value,
        role: OriginalSavedTurnSettlementRole,
    ) -> Result<Option<TurnEntryPins>> {
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
            let (side_chat, pin) = select_saved_turn_settlement(&fixture, role)
                .unwrap()
                .unwrap();
            assert!(side_chat.is_none());
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
    fn side_chat_settlement_requires_its_own_shell_and_nested_grants() {
        use OriginalSavedTurnSettlementRole::{
            SavedReconcile, SavedTurn, SideChatReconcile, SideChatTurn,
        };

        let fixture = records();
        let edges = fixture["profile"]["requested_edges"].as_array().unwrap();
        for (role, target, other) in [
            (SideChatTurn, &SAVED_TURN, SideChatReconcile),
            (SideChatReconcile, &SAVED_RECONCILE, SideChatTurn),
        ] {
            let (side_chat, turn) = select_saved_turn_settlement(&fixture, role)
                .unwrap()
                .unwrap();
            let side_chat = side_chat.unwrap();
            assert_eq!(side_chat.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(side_chat.operation_id(), SIDE_CHAT_TURN.operation);
            assert_eq!(turn.caller_function_id(), side_chat.function_id());
            assert_eq!(turn.operation_id(), target.operation);

            for pin in [&side_chat, &turn] {
                let row = edges
                    .iter()
                    .position(|edge| edge["authority_reference"] == pin.authority_reference())
                    .unwrap();
                let mut candidate = fixture.clone();
                candidate["profile"]["requested_edges"][row]["authority_mode"] =
                    json!("interactive_only");
                candidate["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
                assert!(
                    select_saved_turn_settlement(&candidate, role)
                        .unwrap()
                        .is_none()
                );
                assert_eq!(
                    select_saved_turn_settlement(&candidate, other)
                        .unwrap()
                        .is_none(),
                    pin.operation_id() == SIDE_CHAT_TURN.operation
                );
                for ordinary in [SavedTurn, SavedReconcile] {
                    assert!(
                        select_saved_turn_settlement(&candidate, ordinary)
                            .unwrap()
                            .is_some()
                    );
                }

                candidate["profile"]["requested_edges"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                assert!(matches!(
                    select_saved_turn_settlement(&candidate, role),
                    Err(Inconsistent(_))
                ));
                candidate["plan"]["bindings"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                assert!(
                    select_saved_turn_settlement(&candidate, role)
                        .unwrap()
                        .is_none()
                );
            }

            let mut candidate = fixture.clone();
            for row in [SAVED_RECONCILE_SETTLEMENT_ROW, SAVED_TURN_SETTLEMENT_ROW] {
                candidate["profile"]["requested_edges"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                candidate["plan"]["bindings"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
            }
            assert!(
                select_saved_turn_settlement(&candidate, role)
                    .unwrap()
                    .is_some()
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
    fn side_chat_saved_receipt_requires_its_exact_entry_and_resource_grants() {
        use OriginalSavedReceiptRole::{
            SavedReconcile, SavedTurn, SideChatReconcile, SideChatTurn,
        };

        let fixture = records();
        let select = |fixture: &Value, role| {
            let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_side_chat_saved_receipt_records((&profile, &lock, &plan), &budget, role)
        };
        for (role, sibling, ordinary_role, ordinary_entry) in [
            (
                SideChatTurn,
                SideChatReconcile,
                SavedTurn,
                SAVED_TURN_SETTLEMENT_ROW,
            ),
            (
                SideChatReconcile,
                SideChatTurn,
                SavedReconcile,
                SAVED_RECONCILE_SETTLEMENT_ROW,
            ),
        ] {
            let pins = select(&fixture, role).unwrap().unwrap();
            let [side_chat, turn, receipt] = &pins;
            assert_eq!(side_chat.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(side_chat.function_id(), SIDE_CHAT_TURN.function);
            assert_eq!(turn.caller_function_id(), side_chat.function_id());
            assert_eq!(turn.function_id(), role.caller_function_id());
            assert_eq!(receipt.caller_function_id(), turn.function_id());
            assert_eq!(receipt.operation_id(), CONVERSATION_READ.operation);
            assert!(select(&fixture, ordinary_role).unwrap().is_none());

            let without = |row: usize| {
                let mut missing = fixture.clone();
                missing["profile"]["requested_edges"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                missing["plan"]["bindings"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                missing
            };
            assert!(select(&without(ordinary_entry), role).unwrap().is_some());

            for (index, pin) in pins.iter().enumerate() {
                let row = fixture["profile"]["requested_edges"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .position(|edge| edge["authority_reference"] == pin.authority_reference())
                    .unwrap();
                let missing = without(row);
                assert!(select(&missing, role).unwrap().is_none());
                assert_eq!(select(&missing, sibling).unwrap().is_some(), index != 0);
                assert_eq!(
                    select_saved_receipt(&missing).unwrap()[ordinary_role.index()].is_some(),
                    index != 2
                );

                let mut interactive = fixture.clone();
                interactive["profile"]["requested_edges"][row]["authority_mode"] =
                    json!("interactive_only");
                interactive["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
                assert!(select(&interactive, role).unwrap().is_none());
            }
        }
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
    fn side_chat_append_selects_its_own_signed_grants() {
        let fixture = records();
        let select =
            |records: &Value| select_append_for(records, ConversationAppendCaller::SideChat);
        let (side_chat, [turn, orchestration, manage]) = select(&fixture).unwrap().unwrap();
        let side_chat = side_chat.unwrap();
        assert_eq!(side_chat.caller_function_id(), TURN_SHELL_CALLER);
        assert_eq!(side_chat.function_id(), SIDE_CHAT_TURN.function);
        assert_eq!(turn.caller_function_id(), side_chat.function_id());
        assert_eq!(turn.function_id(), SAVED_TURN_CALLER);
        assert_eq!(orchestration.caller_function_id(), turn.function_id());
        assert_eq!(manage.caller_function_id(), orchestration.function_id());

        for row in [SIDE_CHAT_TURN_ROW, SIDE_CHAT_SAVED_TURN_ROW] {
            let mut denied = fixture.clone();
            denied["profile"]["requested_edges"][row]["authority_mode"] = json!("interactive_only");
            denied["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            assert!(select(&denied).unwrap().is_none());
            assert!(select_append(&denied).unwrap().is_some());
        }

        let mut side_chat_only = fixture;
        side_chat_only["profile"]["requested_edges"]
            .as_array_mut()
            .unwrap()
            .remove(SAVED_TURN_SETTLEMENT_ROW);
        side_chat_only["plan"]["bindings"]
            .as_array_mut()
            .unwrap()
            .remove(SAVED_TURN_SETTLEMENT_ROW);
        assert!(select(&side_chat_only).unwrap().is_some());
        assert!(select_append(&side_chat_only).unwrap().is_none());
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
            let (side_chat, [turn, completion, get]) =
                select_confirmation(&records(), role).unwrap().unwrap();
            assert!(side_chat.is_none());
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
    fn side_chat_confirmation_requires_each_exact_grant_without_borrowing_the_shell_turn() {
        use OriginalSavedReceiptRole::{
            SavedReconcile, SavedTurn, SideChatReconcile, SideChatTurn,
        };
        let fixture = records();
        for (role, sibling, ordinary, ordinary_row) in [
            (
                SideChatTurn,
                SideChatReconcile,
                SavedTurn,
                SAVED_TURN_SETTLEMENT_ROW,
            ),
            (
                SideChatReconcile,
                SideChatTurn,
                SavedReconcile,
                SAVED_RECONCILE_SETTLEMENT_ROW,
            ),
        ] {
            let (side_chat, [turn, completion, get]) =
                select_confirmation(&fixture, role).unwrap().unwrap();
            let side_chat = side_chat.unwrap();
            assert_eq!(side_chat.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(side_chat.function_id(), SIDE_CHAT_TURN.function);
            assert_eq!(turn.caller_function_id(), side_chat.function_id());
            assert_eq!(turn.function_id(), role.caller_function_id());
            assert_eq!(completion.caller_function_id(), turn.function_id());
            assert_eq!(completion.variant_id(), CONVERSATION_CONFIRMATION.variant);
            assert_eq!(get.caller_function_id(), completion.function_id());
            assert_eq!(get.operation_id(), TURN_GET.operation);

            let without = |row| {
                let mut missing = fixture.clone();
                missing["profile"]["requested_edges"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                missing["plan"]["bindings"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                missing
            };
            let without_ordinary = without(ordinary_row);
            assert!(
                select_confirmation(&without_ordinary, role)
                    .unwrap()
                    .is_some()
            );
            assert!(
                select_confirmation(&without_ordinary, ordinary)
                    .unwrap()
                    .is_none()
            );

            for (index, pin) in [side_chat, turn, completion, get].iter().enumerate() {
                let row = fixture["profile"]["requested_edges"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .position(|edge| edge["authority_reference"] == pin.authority_reference())
                    .unwrap();
                let missing = without(row);
                assert!(select_confirmation(&missing, role).unwrap().is_none());
                assert_eq!(
                    select_confirmation(&missing, sibling).unwrap().is_some(),
                    matches!(index, 1 | 2)
                );
                assert_eq!(
                    select_confirmation(&missing, ordinary).unwrap().is_some(),
                    index < 2
                );
                assert!(
                    select_saved_receipt(&missing)
                        .unwrap()
                        .iter()
                        .all(Option::is_some)
                );

                let mut interactive = fixture.clone();
                interactive["profile"]["requested_edges"][row]["authority_mode"] =
                    json!("interactive_only");
                interactive["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
                assert!(select_confirmation(&interactive, role).unwrap().is_none());
            }
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
    fn shell_provider_snapshot_requires_the_desktop_shells_own_settings_grant() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let select = |fixture: &Value| {
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_shell_provider_snapshot_records((&profile, &lock, &plan), &budget)
        };
        let mut fixture = records();
        let pin = select(&fixture).unwrap().unwrap();
        assert_eq!(pin.caller_function_id(), TURN_SHELL_CALLER);
        assert_eq!(pin.function_id(), PROVIDER_READ.function);
        assert_eq!(pin.contract_id(), PROVIDER_READ.contract);
        assert_eq!(pin.operation_id(), PROVIDER_READ.operation);
        assert_eq!(pin.variant_id(), PROVIDER_READ.variant);
        assert_eq!(
            pin.authority_reference(),
            fixture["profile"]["requested_edges"][SHELL_PROVIDER_READ_ROW]["authority_reference"]
        );

        let mut other_shell = fixture.clone();
        for record in ["profile", "lock", "plan"] {
            other_shell[record]["shell"]["provider_id"] = json!("unrelated.shell");
        }
        assert!(select(&other_shell).unwrap().is_none());

        fixture["profile"]["requested_edges"]
            .as_array_mut()
            .unwrap()
            .remove(SHELL_PROVIDER_READ_ROW);
        assert!(matches!(select(&fixture), Err(Inconsistent(_))));
        fixture["plan"]["bindings"]
            .as_array_mut()
            .unwrap()
            .remove(SHELL_PROVIDER_READ_ROW);
        assert!(select(&fixture).unwrap().is_none());
        assert!(select_model_save(&fixture).unwrap().is_some());
    }

    #[test]
    fn model_access_provider_snapshots_require_each_desktop_shell_grant() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let select = |fixture: &Value| {
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_model_access_provider_snapshot_records((&profile, &lock, &plan), &budget)
        };
        let mut fixture = records();
        assert!(select(&fixture).unwrap().iter().all(Option::is_none));
        for field in ["requested_edges", "authority_references"] {
            fixture["profile"][field] = json!([]);
        }
        fixture["plan"]["bindings"] = json!([]);
        const REGISTRY_REVISION: &str =
            "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d";
        for (route, reference, scope) in [
            (
                &MODEL_ACCESS_READ,
                "authority-ref:781ba2a0eb5861768c70456ae577184f298d82bf86a96379d7d10bba8837b3fa",
                "sha256:6e3d5ddd7eb15c58a7ba7bf9cc7622a6ef111f501a1b63ff26d66f55dec310c1",
            ),
            (
                &MODEL_ACCESS_CATALOG,
                "authority-ref:4ef7ff45f17f93f241e6bc38f1097ce88941c2d8b4e5ae0657e483b6ee710d9f",
                "sha256:82ba4dd024168f171cffb63e165f6c0093f9976747537a7e1e9cf9d9d95b2670",
            ),
        ] {
            add_record(
                &mut fixture,
                route,
                TURN_SHELL_CALLER,
                reference,
                REGISTRY_REVISION,
                scope,
            );
        }

        let operations = [&MODEL_ACCESS_READ, &MODEL_ACCESS_CATALOG];
        for (row, (route, pin)) in operations
            .into_iter()
            .zip(select(&fixture).unwrap())
            .enumerate()
        {
            let pin = pin.unwrap();
            assert_eq!(pin.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(pin.pack_id(), route.pack);
            assert_eq!(pin.function_id(), route.function);
            assert_eq!(pin.contract_id(), route.contract);
            assert_eq!(pin.operation_id(), route.operation);
            assert_eq!(pin.variant_id(), route.variant);

            let mut missing = fixture.clone();
            missing["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(matches!(select(&missing), Err(Inconsistent(_))));
            missing["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            let grants = select(&missing).unwrap();
            assert!(grants[row].is_none());
            assert!(grants[1 - row].is_some());

            let mut interactive = fixture.clone();
            interactive["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            interactive["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            let grants = select(&interactive).unwrap();
            assert!(grants[row].is_none());
            assert!(grants[1 - row].is_some());

            let mut wrong_scope = fixture.clone();
            let scope =
                &mut wrong_scope["profile"]["requested_edges"][row]["requested_scope_template"];
            scope["dimensions"]["operation"] = json!([operations[1 - row].operation]);
            assert!(matches!(select(&wrong_scope), Err(Inconsistent(_))));

            let mut wrong_variant = fixture.clone();
            wrong_variant["plan"]["bindings"][row]["variant_id"] =
                json!(operations[1 - row].variant);
            assert!(matches!(select(&wrong_variant), Err(Inconsistent(_))));
        }

        for field in ["provider_id", "pack_id"] {
            let mut other_shell = fixture.clone();
            for record in ["profile", "lock", "plan"] {
                other_shell[record]["shell"][field] = json!("unrelated.shell");
            }
            assert!(select(&other_shell).unwrap().iter().all(Option::is_none));
        }
    }

    #[test]
    fn query_provider_snapshots_require_the_exact_desktop_shell_and_both_grants() {
        let fixture = records();
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let select = |fixture: &Value| {
            let [profile, lock, plan] = parse_records(fixture, &budget);
            select_query_provider_snapshot_records((&profile, &lock, &plan), &budget)
        };

        let selected = select(&fixture).unwrap();
        for (role, (owner, pins)) in [&MODEL_SEARCH, &COMMAND_DATASOURCE]
            .into_iter()
            .zip(selected)
            .enumerate()
        {
            let [query, read] = pins.unwrap();
            assert_eq!(query.caller_function_id(), TURN_SHELL_CALLER);
            assert_eq!(read.caller_function_id(), owner.function);
            for (pin, route) in [(query, owner), (read, &PROVIDER_READ)] {
                assert_eq!(pin.pack_id(), route.pack);
                assert_eq!(pin.function_id(), route.function);
                assert_eq!(pin.contract_id(), route.contract);
                assert_eq!(pin.operation_id(), route.operation);
                assert_eq!(pin.variant_id(), route.variant);
                assert_eq!(pin.execution_kind(), "host_extension");

                let row = fixture["plan"]["bindings"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .position(|binding| binding["authority_reference"] == pin.authority_reference())
                    .unwrap();
                let mut missing = fixture.clone();
                missing["profile"]["requested_edges"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                assert!(matches!(select(&missing), Err(Inconsistent(_))));
                missing["plan"]["bindings"]
                    .as_array_mut()
                    .unwrap()
                    .remove(row);
                let grants = select(&missing).unwrap();
                assert!(grants[role].is_none());
                assert!(grants[1 - role].is_some());

                let mut interactive = fixture.clone();
                interactive["profile"]["requested_edges"][row]["authority_mode"] =
                    json!("interactive_only");
                interactive["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
                let grants = select(&interactive).unwrap();
                assert!(grants[role].is_none());
                assert!(grants[1 - role].is_some());

                let mut changed_pin = fixture.clone();
                changed_pin["plan"]["bindings"][row]["execution_kind"] = json!("pack_vm");
                assert!(matches!(select(&changed_pin), Err(Inconsistent(_))));
            }
        }

        for field in ["provider_id", "pack_id"] {
            let mut other_shell = fixture.clone();
            for record in ["profile", "lock", "plan"] {
                other_shell[record]["shell"][field] = json!("unrelated.shell");
            }
            assert!(select(&other_shell).unwrap().iter().all(Option::is_none));
        }
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
