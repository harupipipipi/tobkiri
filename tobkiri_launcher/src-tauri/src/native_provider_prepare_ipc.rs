//! Private Registry requests and live authorization checks for the managed Host.

use std::{
    io::{self, Read, Write},
    os::{fd::AsRawFd, unix::net::UnixStream},
    sync::{Arc, Mutex, TryLockError},
    thread,
    time::{Duration, Instant},
};

use anyhow::{anyhow, bail, ensure, Context, Result};
use serde::{de, Deserialize, Deserializer, Serialize};
use serde_json::value::RawValue;
use tauri::{AppHandle, Manager};
use tobkiri_core::{Digest, NativeArtifactError};
use tobkiri_harness_native::{
    LegacyLocalChatSourceError, LegacySavedTurnSettlementError, OriginalLocalChatError,
    OriginalLocalChatPolicyError, OriginalProviderSnapshotRole, OriginalSavedLocalChatRole,
    OriginalWorkflowSnapshotCaller, OriginalWorkflowSnapshotEntry,
};
use tobkiri_wasm::{
    LegacyLocalChatHttpError, NativeDataIoError, LEGACY_LOCAL_CHAT_REQUEST_LIMIT,
    LEGACY_LOCAL_CHAT_RESPONSE_LIMIT,
};

use crate::{
    defaultspack_manager::{ApplicationProcessManager, ProviderPrepareListener},
    host_contract::ExecutionProfileIdentity,
    native_harness_bootstrap::{
        start_provider_prepare, AiGatewayDataOperation, AiGatewayDataRequiresLegacyCodec,
        ApplicationPresentationRole, ConversationAppendCaller, ConversationCompletePhase,
        ConversationReadCaller, ConversationRegistryRequiresLegacyRead,
        ConversationWriteRequiresLegacyRead, LinkedConversationReadMode,
        ModelAccessProjectionPhase, ModelRegistryRequiresLegacyRead, NativeRegistryRequest,
        OriginalModelResolveRole, OriginalSavedReceiptRole, PendingProviderPrepare,
        ProviderPrepareDeadline, ProviderSnapshotRequiresLegacyRead, RegistryResponse,
        SavedInputCaller, SavedInputMode, SavedLocalChatRequiresLegacyRequest,
        SavedOrchestrationStepError, SavedTurnStartAction, SavedTurnStartCaller, SettlementCaller,
        TurnSettlementRequiresLegacyCodec, TurnStartRequiresLegacyCodec, AI_GATEWAY_DATA_LIMIT,
        APPLICATION_PRESENTATION_INPUT_LIMIT, APPLICATION_PRESENTATION_RESULT_LIMIT,
        CONVERSATION_APPEND_LIMIT, CONVERSATION_COMPLETE_RESULT_LIMIT,
        CONVERSATION_CONFIRMATION_LIMIT, CONVERSATION_READ_REPLY_LIMIT, MODEL_RESOLVE_REPLY_LIMIT,
        PROVIDER_SNAPSHOT_REPLY_LIMIT, SAVED_INPUT_FRAME_LIMIT, SAVED_ORCHESTRATION_FRAME_LIMIT,
        TURN_READ_REPLY_LIMIT, TURN_START_REPLY_LIMIT,
    },
    shell_handoff::{process_identity, LauncherProcessIdentity},
};

const START: u8 = 1;
const CHECK: u8 = 2;
const LIVE: u8 = 3;
const PLAN: u8 = 7;
const FAILURE: u8 = 8;
const CANCEL: u8 = 9;
const INAPPLICABLE: u8 = 10;
const PUBLISH_CHECK: u8 = 11;
const PUBLISH_PERMIT: u8 = 12;
const START_APPEND: u8 = 13;
const BODY_READY: u8 = 14;
const ORCHESTRATION_INPUT: u8 = 15;
const SAVED_LOCAL_CHAT_INPUT: u8 = 16;
const DISPATCH_CHECK: u8 = 17;
const DISPATCH_PERMIT: u8 = 18;
const PROJECTED_LOCAL_CHAT: u8 = 19;
const JSON_LIMIT: usize = 16 * 1024;
const MODEL_RESULT_LIMIT: usize = 1024 * 1024;
const SAVED_RECEIPT_TURN_ID_MAX_CHARS: usize = 2048;
const TURN_READ_ID_MAX_CHARS: usize = 256;
const MAX_SAFE_JSON_REVISION: u64 = (1 << 53) - 1;
const TURN_SETTLEMENT_RESULT_LIMIT: usize = 1024 * 1024;
const TURN_SETTLEMENT_REFERENCE_LIMIT: usize = 2 * 1024;
const FRAME_TIMEOUT: Duration = Duration::from_secs(3);
const POLL_INTERVAL: Duration = Duration::from_millis(10);
const CLEANUP_WAIT: Duration = Duration::from_secs(10);
const MAX_HORIZON: Duration = Duration::from_secs(300);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Start<'a> {
    identity: Identity,
    runtime_instance: String,
    security_epoch: u64,
    deadline_ns: u64,
    #[serde(default, deserialize_with = "deserialize_present")]
    request: Option<Request>,
    #[serde(borrow, default, deserialize_with = "deserialize_optional_object")]
    apply: Option<&'a RawValue>,
    #[serde(default, deserialize_with = "deserialize_present")]
    model_list: Option<ModelList>,
    #[serde(borrow, default, deserialize_with = "deserialize_present")]
    model_save: Option<ModelSave<'a>>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    conversation_get: Option<ConversationGet>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    conversation_list: Option<ConversationList>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    conversation_receipt: Option<ConversationReceipt>,
    #[serde(borrow, default, deserialize_with = "deserialize_present")]
    conversation_confirmation: Option<ConversationConfirmation<'a>>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    model_resolve: Option<ModelResolve>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    turn_read: Option<TurnRead>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    turn_start: Option<TurnStart>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    saved_input: Option<SavedInputStart>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    turn_settlement: Option<TurnSettlement>,
    #[serde(borrow, default, deserialize_with = "deserialize_present")]
    conversation_append: Option<ConversationAppend<'a>>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    saved_orchestration: Option<SavedOrchestrationStart>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    application_presentation: Option<ApplicationPresentationStart>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    conversation_complete: Option<ConversationCompleteStart>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    saved_local_chat: Option<SavedLocalChatStart>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    ai_route: Option<AiRoutingStart>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    provider_snapshot: Option<ProviderSnapshot>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    model_access_projection: Option<ModelAccessProjectionStart>,
}

enum RequestStart {
    Inline(NativeRegistryRequest),
    SavedInput(SavedInputStart),
    SavedOrchestration {
        input_bytes: usize,
    },
    ApplicationPresentation {
        role: ApplicationPresentationRole,
        input_bytes: usize,
    },
    ConversationComplete {
        phase: ConversationCompletePhase,
        input_bytes: usize,
    },
    ModelAccessProjection {
        phase: ModelAccessProjectionPhase,
        input_bytes: usize,
    },
    SavedLocalChat(SavedLocalChatStart),
    AiGatewayData {
        operation: AiGatewayDataOperation,
        input_bytes: usize,
    },
}

impl RequestStart {
    fn reject_missing_launch_proof(
        &self,
        stream: &mut UnixStream,
        deadline: Instant,
    ) -> Result<()> {
        let (tag, payload): (u8, &[u8]) = match self {
            Self::ConversationComplete { .. }
            | Self::ModelAccessProjection { .. }
            | Self::SavedInput(_) => (FAILURE, b"denied"),
            Self::ApplicationPresentation { .. } => (FAILURE, b"unavailable"),
            _ => (INAPPLICABLE, &[]),
        };
        send_frame(stream, tag, payload, deadline)
    }
}

enum BodyStart<'a> {
    SavedInput(&'a SavedInputStart),
    SavedOrchestration(&'a SavedOrchestrationStart),
    ApplicationPresentation(&'a ApplicationPresentationStart),
    ConversationComplete(&'a ConversationCompleteStart),
    ModelAccessProjection(&'a ModelAccessProjectionStart),
    SavedLocalChat(&'a SavedLocalChatStart),
    AiRouting(&'a AiRoutingStart),
}

impl Start<'_> {
    fn request_start(&self) -> Result<RequestStart> {
        let turn_mutation = match (&self.turn_start, &self.turn_settlement) {
            (None, None) => None,
            (Some(start), None) => Some(NativeRegistryRequest::TurnStart {
                caller: start.caller,
                action: start.action,
                turn_id: start.turn_id.clone(),
                conversation_id: start.conversation_id.clone(),
                conversation_revision: start.conversation_revision,
                input_digest: start.input_digest.clone(),
                source_digest: start.source_digest.clone(),
            }),
            (None, Some(settlement)) => {
                ensure!(
                    !matches!(
                        settlement.caller,
                        SettlementCaller::SavedReconcile | SettlementCaller::SideChatReconcile
                    ) || settlement.expected_revision.is_some(),
                    "Turn settlement reconciliation requires an expected revision"
                );
                Some(NativeRegistryRequest::TurnSettlement {
                    caller: settlement.caller,
                    turn_id: settlement.turn_id.clone(),
                    input_digest: settlement.input_digest.clone(),
                    expected_revision: settlement.expected_revision,
                    result_reference: settlement.result_reference.clone(),
                })
            }
            (Some(_), Some(_)) => bail!("native Registry start must name exactly one operation"),
        };
        let body = match (
            &self.saved_orchestration,
            &self.application_presentation,
            &self.conversation_complete,
            &self.model_access_projection,
            &self.saved_local_chat,
            &self.ai_route,
            &self.saved_input,
        ) {
            (None, None, None, None, None, None, None) => None,
            (Some(saved), None, None, None, None, None, None) => {
                Some(BodyStart::SavedOrchestration(saved))
            }
            (None, Some(presentation), None, None, None, None, None) => {
                Some(BodyStart::ApplicationPresentation(presentation))
            }
            (None, None, Some(complete), None, None, None, None) => {
                Some(BodyStart::ConversationComplete(complete))
            }
            (None, None, None, Some(projection), None, None, None) => {
                Some(BodyStart::ModelAccessProjection(projection))
            }
            (None, None, None, None, Some(chat), None, None) => {
                Some(BodyStart::SavedLocalChat(chat))
            }
            (None, None, None, None, None, Some(routing), None) => {
                Some(BodyStart::AiRouting(routing))
            }
            (None, None, None, None, None, None, Some(input)) => Some(BodyStart::SavedInput(input)),
            _ => bail!("native Registry start must name exactly one operation"),
        };
        let registry_read = match (
            &self.model_list,
            &self.provider_snapshot,
            &self.conversation_list,
        ) {
            (Some(_), None, None) => Some(NativeRegistryRequest::ModelList),
            (None, Some(snapshot), None) => Some(NativeRegistryRequest::ProviderSnapshot {
                role: snapshot.role,
            }),
            (None, None, Some(_)) => Some(NativeRegistryRequest::ConversationList),
            (None, None, None) => None,
            _ => bail!("native Registry start must name exactly one operation"),
        };
        let operation = match (
            &self.request,
            registry_read,
            &self.model_save,
            &self.conversation_get,
            &self.conversation_receipt,
            &self.conversation_confirmation,
            &self.model_resolve,
            &self.turn_read,
            turn_mutation,
            &self.conversation_append,
            body,
            self.apply,
        ) {
            (Some(request), None, None, None, None, None, None, None, None, None, None, apply) => {
                let request = serde_json::to_vec(request)?;
                ensure!(
                    request.len() <= JSON_LIMIT,
                    "Provider request exceeds its envelope"
                );
                // Preserve the frozen plan's exact JSON for the Core parser.
                let apply = apply.map(|plan| plan.get().as_bytes().to_vec());
                NativeRegistryRequest::Provider { request, apply }
            }
            (None, Some(read), None, None, None, None, None, None, None, None, None, None) => read,
            (None, None, Some(save), None, None, None, None, None, None, None, None, None) => {
                NativeRegistryRequest::ModelSave {
                    request: save.request.get().as_bytes().to_vec(),
                    provider: save.provider.get().as_bytes().to_vec(),
                }
            }
            (None, None, None, Some(get), None, None, None, None, None, None, None, None) => {
                get.operation()?
            }
            (None, None, None, None, Some(receipt), None, None, None, None, None, None, None) => {
                NativeRegistryRequest::ConversationReceipt {
                    caller: receipt.caller,
                    turn_id: receipt.turn_id.clone(),
                }
            }
            (
                None,
                None,
                None,
                None,
                None,
                Some(confirmation),
                None,
                None,
                None,
                None,
                None,
                None,
            ) => confirmation.operation(&self.identity.profile_id)?,
            (None, None, None, None, None, None, Some(resolve), None, None, None, None, None) => {
                NativeRegistryRequest::ModelResolve {
                    role: resolve.role,
                    identifier: resolve.identifier.clone(),
                }
            }
            (None, None, None, None, None, None, None, Some(read), None, None, None, None) => {
                NativeRegistryRequest::TurnRead {
                    caller: read.caller,
                    turn_id: read.turn_id.clone(),
                }
            }
            (None, None, None, None, None, None, None, None, Some(operation), None, None, None) => {
                operation
            }
            (None, None, None, None, None, None, None, None, None, Some(append), None, None) => {
                append.operation()?
            }
            (
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                Some(BodyStart::SavedInput(input)),
                None,
            ) => {
                ensure!(
                    (1..=SAVED_INPUT_FRAME_LIMIT).contains(&input.input_bytes),
                    "invalid saved input size"
                );
                return Ok(RequestStart::SavedInput(*input));
            }
            (
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                Some(BodyStart::SavedOrchestration(saved)),
                None,
            ) => {
                ensure!(
                    (1..=SAVED_ORCHESTRATION_FRAME_LIMIT).contains(&saved.input_bytes),
                    "invalid Saved Orchestration input size"
                );
                return Ok(RequestStart::SavedOrchestration {
                    input_bytes: saved.input_bytes,
                });
            }
            (
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                Some(BodyStart::ApplicationPresentation(presentation)),
                None,
            ) => {
                ensure!(
                    (1..=APPLICATION_PRESENTATION_INPUT_LIMIT).contains(&presentation.input_bytes),
                    "invalid application presentation input size"
                );
                return Ok(RequestStart::ApplicationPresentation {
                    role: presentation.role,
                    input_bytes: presentation.input_bytes,
                });
            }
            (
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                Some(BodyStart::ConversationComplete(complete)),
                None,
            ) => {
                complete.phase.check_input_size(complete.input_bytes)?;
                return Ok(RequestStart::ConversationComplete {
                    phase: complete.phase,
                    input_bytes: complete.input_bytes,
                });
            }
            (
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                Some(BodyStart::ModelAccessProjection(projection)),
                None,
            ) => {
                ModelAccessProjectionPhase::check_input_size(projection.input_bytes)?;
                return Ok(RequestStart::ModelAccessProjection {
                    phase: projection.phase,
                    input_bytes: projection.input_bytes,
                });
            }
            (
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                Some(BodyStart::SavedLocalChat(chat)),
                None,
            ) => return Ok(RequestStart::SavedLocalChat(chat.clone())),
            (
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                Some(BodyStart::AiRouting(routing)),
                None,
            ) => {
                ensure!(
                    (1..=AI_GATEWAY_DATA_LIMIT).contains(&routing.input_bytes),
                    "invalid AI Gateway DATA input size"
                );
                let operation = routing.operation()?;
                return Ok(RequestStart::AiGatewayData {
                    operation,
                    input_bytes: routing.input_bytes,
                });
            }
            _ => bail!("native Registry start must name exactly one operation"),
        };
        Ok(RequestStart::Inline(operation))
    }

    #[cfg(test)]
    fn operation(&self) -> Result<NativeRegistryRequest> {
        match self.request_start()? {
            RequestStart::Inline(operation) => Ok(operation),
            RequestStart::SavedOrchestration { .. }
            | RequestStart::SavedInput(_)
            | RequestStart::ApplicationPresentation { .. }
            | RequestStart::ConversationComplete { .. }
            | RequestStart::ModelAccessProjection { .. }
            | RequestStart::SavedLocalChat(_)
            | RequestStart::AiGatewayData { .. } => {
                bail!("request needs a body")
            }
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelList {}

#[derive(Deserialize)]
#[serde(try_from = "ProviderSnapshotWire")]
struct ProviderSnapshot {
    role: OriginalProviderSnapshotRole,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ProviderSnapshotWire {
    role: String,
    #[serde(default, deserialize_with = "deserialize_present")]
    entry: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelSave<'a> {
    #[serde(borrow, deserialize_with = "deserialize_object")]
    request: &'a RawValue,
    #[serde(borrow, deserialize_with = "deserialize_object")]
    provider: &'a RawValue,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConversationGet {
    #[serde(deserialize_with = "deserialize_conversation_caller")]
    caller: ConversationReadCaller,
    conversation_id: String,
    #[serde(default)]
    linked_context: bool,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    linked_parent: Option<LinkedParent>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConversationList {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LinkedParent {
    mode: LinkedConversationReadMode,
    nonce: String,
}

impl ConversationGet {
    fn operation(&self) -> Result<NativeRegistryRequest> {
        ensure!(
            !(self.linked_context && self.linked_parent.is_some()),
            "linked Conversation read modes are mutually exclusive"
        );
        if self.linked_context || self.linked_parent.is_some() {
            ensure!(
                self.caller == ConversationReadCaller::SavedOrchestration,
                "linked Conversation reads require the Saved Orchestration caller"
            );
            let mut bytes = self.conversation_id.bytes();
            ensure!(
                self.conversation_id.len() <= 256
                    && bytes
                        .next()
                        .is_some_and(|byte| byte.is_ascii_alphanumeric())
                    && bytes.all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte)),
                "invalid linked Conversation child ID"
            );
        }
        if let Some(parent) = &self.linked_parent {
            let nonce: [u8; 32] = parent
                .nonce
                .as_bytes()
                .try_into()
                .map_err(|_| anyhow!("invalid linked Conversation nonce"))?;
            ensure!(
                nonce
                    .iter()
                    .all(|&byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')),
                "invalid linked Conversation nonce"
            );
            return Ok(NativeRegistryRequest::LinkedConversationRead {
                conversation_id: self.conversation_id.clone(),
                nonce,
                mode: parent.mode,
            });
        }
        if self.linked_context {
            return Ok(NativeRegistryRequest::LinkedConversationContext {
                conversation_id: self.conversation_id.clone(),
            });
        }
        Ok(NativeRegistryRequest::ConversationGet {
            caller: self.caller,
            conversation_id: self.conversation_id.clone(),
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConversationReceipt {
    #[serde(deserialize_with = "deserialize_saved_caller")]
    caller: OriginalSavedReceiptRole,
    #[serde(deserialize_with = "deserialize_saved_receipt_turn_id")]
    turn_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TurnRead {
    #[serde(deserialize_with = "deserialize_saved_caller")]
    caller: OriginalSavedReceiptRole,
    #[serde(deserialize_with = "deserialize_turn_read_id")]
    turn_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TurnStart {
    #[serde(
        default = "default_turn_start_caller",
        deserialize_with = "deserialize_turn_start_caller"
    )]
    caller: SavedTurnStartCaller,
    #[serde(deserialize_with = "deserialize_turn_start_action")]
    action: SavedTurnStartAction,
    #[serde(deserialize_with = "deserialize_turn_start_id")]
    turn_id: String,
    #[serde(deserialize_with = "deserialize_turn_start_id")]
    conversation_id: String,
    #[serde(deserialize_with = "deserialize_turn_start_revision")]
    conversation_revision: u64,
    #[serde(deserialize_with = "deserialize_digest")]
    input_digest: String,
    #[serde(deserialize_with = "deserialize_digest")]
    source_digest: String,
}

#[derive(Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
struct SavedInputStart {
    #[serde(
        default = "default_saved_input_caller",
        deserialize_with = "deserialize_saved_input_caller"
    )]
    caller: SavedInputCaller,
    mode: SavedInputMode,
    input_bytes: usize,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SavedInputReply<'a> {
    #[serde(borrow)]
    accepted: &'a RawValue,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TurnStartReply<'a> {
    claimed: bool,
    #[serde(borrow, deserialize_with = "deserialize_object")]
    turn: &'a RawValue,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SavedOrchestrationStart {
    input_bytes: usize,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ApplicationPresentationStart {
    #[serde(deserialize_with = "deserialize_application_presentation_role")]
    role: ApplicationPresentationRole,
    input_bytes: usize,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConversationCompleteStart {
    phase: ConversationCompletePhase,
    input_bytes: usize,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelAccessProjectionStart {
    phase: ModelAccessProjectionPhase,
    input_bytes: usize,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AiRoutingStart {
    #[serde(deserialize_with = "deserialize_model_resolve_role")]
    role: OriginalModelResolveRole,
    input_bytes: usize,
    #[serde(default)]
    stream_result: bool,
    #[serde(default)]
    usage_cost: bool,
    #[serde(default)]
    tool_intents: bool,
    #[serde(default)]
    pipeline: AiPipelineStart,
    #[serde(default)]
    gateway_result: bool,
    #[serde(default)]
    gateway_policy: bool,
}

impl AiRoutingStart {
    fn operation(&self) -> Result<AiGatewayDataOperation> {
        use AiGatewayDataOperation as Operation;
        use OriginalModelResolveRole::{Generate, Stream};

        let role = self.role;
        let mut operations = [
            self.stream_result.then_some(Operation::StreamResult),
            self.usage_cost.then_some(Operation::UsageCost(role)),
            self.tool_intents.then_some(Operation::ToolIntents(role)),
            self.gateway_result
                .then_some(Operation::GatewayResult(role)),
            self.gateway_policy
                .then_some(Operation::GatewayPolicy(role)),
            match self.pipeline {
                AiPipelineStart::Off => None,
                AiPipelineStart::Prepare => Some(Operation::RequestPrepare(role)),
                AiPipelineStart::Failover => Some(Operation::Failover),
            },
        ]
        .into_iter()
        .flatten();
        let operation = operations.next().unwrap_or(Operation::Route(role));
        ensure!(
            operations.next().is_none(),
            "AI Gateway DATA request must name one operation"
        );
        let role_matches = match operation {
            Operation::StreamResult => role == Stream,
            Operation::Failover => role == Generate,
            Operation::ToolIntents(_) | Operation::UsageCost(_) | Operation::GatewayResult(_) => {
                matches!(role, Generate | Stream)
            }
            Operation::Route(_) | Operation::RequestPrepare(_) | Operation::GatewayPolicy(_) => {
                true
            }
        };
        ensure!(
            role_matches,
            "AI Gateway DATA operation does not match its role"
        );
        Ok(operation)
    }
}

#[derive(Clone, Copy, Default, Deserialize)]
#[serde(rename_all = "kebab-case")]
enum AiPipelineStart {
    #[default]
    #[serde(skip)]
    Off,
    Prepare,
    Failover,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct SavedLocalChatStart {
    #[serde(deserialize_with = "deserialize_saved_local_chat_role")]
    role: OriginalSavedLocalChatRole,
    connection_id: String,
    record_revision: u64,
    endpoint: String,
    input_bytes: usize,
    #[serde(default)]
    project_response: bool,
    #[serde(default)]
    compile_request: bool,
    #[serde(default)]
    compile_policy: bool,
}

impl SavedLocalChatStart {
    fn validate(&self) -> Result<(), &'static str> {
        if self.input_bytes > LEGACY_LOCAL_CHAT_REQUEST_LIMIT {
            return Err("limit-exceeded");
        }
        if self.input_bytes == 0
            || (self.compile_request && !self.project_response)
            || (self.compile_policy && !self.compile_request)
            || !(1..=256).contains(&self.connection_id.len())
            || self.record_revision == 0
            || !(1..=2048).contains(&self.endpoint.len())
        {
            return Err("invalid");
        }
        Ok(())
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConversationConfirmation<'a> {
    #[serde(deserialize_with = "deserialize_saved_caller")]
    caller: OriginalSavedReceiptRole,
    #[serde(borrow, deserialize_with = "deserialize_object")]
    terminal: &'a RawValue,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConversationAppend<'a> {
    #[serde(
        default = "default_conversation_append_caller",
        deserialize_with = "deserialize_conversation_append_caller"
    )]
    caller: ConversationAppendCaller,
    #[serde(borrow, deserialize_with = "deserialize_object")]
    request: &'a RawValue,
}

fn default_conversation_append_caller() -> ConversationAppendCaller {
    ConversationAppendCaller::SavedTurn
}

fn deserialize_conversation_append_caller<'de, D>(
    deserializer: D,
) -> std::result::Result<ConversationAppendCaller, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "saved-turn" => Ok(ConversationAppendCaller::SavedTurn),
        "side-chat" => Ok(ConversationAppendCaller::SideChat),
        _ => Err(de::Error::custom("unknown Conversation append caller")),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AppendRequest {
    conversation_id: String,
    expected_conversation_revision: i64,
    message: AppendMessageIdentity,
    saved_input: AppendSavedInput,
}

#[derive(Deserialize)]
struct AppendMessageIdentity {
    id: String,
    role: AppendRole,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum AppendRole {
    User,
    Assistant,
}

#[derive(Deserialize)]
struct AppendSavedInput {
    request: AppendSavedIdentity,
}

#[derive(Deserialize)]
struct AppendSavedIdentity {
    conversation_id: String,
    turn_id: String,
}

impl ConversationAppend<'_> {
    fn operation(&self) -> Result<NativeRegistryRequest> {
        let raw = self.request.get();
        let request: AppendRequest = serde_json::from_str(raw)
            .map_err(|_| anyhow!("invalid Conversation append request"))?;
        let saved = request.saved_input.request;
        ensure!(
            request.conversation_id == saved.conversation_id
                && valid_saved_identifier(&request.conversation_id)
                && valid_saved_identifier(&saved.turn_id)
                && request.expected_conversation_revision > 0,
            "invalid Conversation append identity or revision"
        );
        let role = match request.message.role {
            AppendRole::User => "user",
            AppendRole::Assistant => "assistant",
        };
        // All three values are ASCII with no JSON escape characters.
        let identity =
            serde_json::to_vec(&[request.conversation_id.as_str(), &saved.turn_id, role])?;
        ensure!(
            request.message.id.strip_prefix("message:") == Some(Digest::hash(&identity).hex()),
            "invalid Conversation append message identity"
        );
        Ok(NativeRegistryRequest::ConversationAppend {
            caller: self.caller,
            // The locked owner validates the complete input and receipt evidence.
            request: raw.as_bytes().to_vec(),
            expected_revision: request.expected_conversation_revision as u64,
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfirmationTerminal {
    id: String,
    profile_id: String,
    conversation_id: String,
    request_id: String,
    status: String,
    revision: u64,
    conversation_revision: u64,
    input_digest: String,
    result_reference: ConfirmationReference,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfirmationReference {
    conversation_id: String,
    conversation_revision: u64,
    user_message_id: String,
    assistant_message_id: String,
    outcome_digest: String,
}

impl ConversationConfirmation<'_> {
    fn operation(&self, profile_id: &str) -> Result<NativeRegistryRequest> {
        let raw = self.terminal.get();
        ensure!(
            raw.len() <= CONVERSATION_CONFIRMATION_LIMIT,
            "native Conversation confirmation terminal exceeds its envelope"
        );
        let terminal: ConfirmationTerminal = serde_json::from_str(raw)
            .map_err(|_| anyhow!("invalid Conversation confirmation terminal"))?;
        let reference = terminal.result_reference;
        ensure!(
            terminal.profile_id == profile_id
                && valid_saved_identifier(&terminal.id)
                && valid_saved_identifier(&terminal.conversation_id)
                && reference.conversation_id == terminal.conversation_id
                && has_hex_suffix(&terminal.request_id, "saved-turn.")
                && terminal.status == "completed"
                && terminal.revision >= 3
                && terminal.conversation_revision > 0
                && terminal.conversation_revision.checked_add(2)
                    == Some(reference.conversation_revision)
                && has_hex_suffix(&reference.user_message_id, "message:")
                && has_hex_suffix(&reference.assistant_message_id, "message:")
                && Digest::parse(&terminal.input_digest).is_ok()
                && Digest::parse(&reference.outcome_digest).is_ok(),
            "invalid Conversation confirmation terminal identity or evidence"
        );
        Ok(NativeRegistryRequest::ConversationConfirmation {
            caller: self.caller,
            conversation_id: terminal.conversation_id,
            // The guest authoritatively compares the exact owner evidence.
            terminal: raw.as_bytes().to_vec(),
        })
    }
}

fn has_hex_suffix(value: &str, prefix: &str) -> bool {
    value.strip_prefix(prefix).is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    })
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelResolve {
    #[serde(deserialize_with = "deserialize_model_resolve_role")]
    role: OriginalModelResolveRole,
    #[serde(deserialize_with = "deserialize_model_identifier")]
    identifier: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TurnSettlement {
    #[serde(deserialize_with = "deserialize_settlement_caller")]
    caller: SettlementCaller,
    #[serde(deserialize_with = "deserialize_settlement_turn_id")]
    turn_id: String,
    #[serde(deserialize_with = "deserialize_digest")]
    input_digest: String,
    #[serde(deserialize_with = "deserialize_settlement_revision")]
    expected_revision: Option<u64>,
    #[serde(deserialize_with = "deserialize_settlement_reference")]
    result_reference: String,
}

fn deserialize_settlement_caller<'de, D>(
    deserializer: D,
) -> std::result::Result<SettlementCaller, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "saved-turn" => Ok(SettlementCaller::SavedTurn),
        "saved-reconcile" => Ok(SettlementCaller::SavedReconcile),
        "side-chat-turn" => Ok(SettlementCaller::SideChatTurn),
        "side-chat-reconcile" => Ok(SettlementCaller::SideChatReconcile),
        _ => Err(de::Error::custom("unknown Turn settlement caller")),
    }
}

fn deserialize_settlement_turn_id<'de, D>(deserializer: D) -> std::result::Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    let turn_id = String::deserialize(deserializer)?;
    if valid_saved_identifier(&turn_id) {
        Ok(turn_id)
    } else {
        Err(de::Error::custom("invalid Turn settlement identifier"))
    }
}

fn valid_saved_identifier(value: &str) -> bool {
    let mut bytes = value.bytes();
    value.len() <= 256
        && bytes
            .next()
            .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte))
}

fn default_saved_input_caller() -> SavedInputCaller {
    SavedInputCaller::SavedTurn
}

fn deserialize_saved_input_caller<'de, D>(
    deserializer: D,
) -> std::result::Result<SavedInputCaller, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "saved-turn" => Ok(SavedInputCaller::SavedTurn),
        "calendar" => Ok(SavedInputCaller::Calendar),
        "calendar-manual" => Ok(SavedInputCaller::CalendarManual),
        _ => Err(de::Error::custom("unknown saved input caller")),
    }
}

fn default_turn_start_caller() -> SavedTurnStartCaller {
    SavedTurnStartCaller::SavedTurn
}

fn deserialize_turn_start_caller<'de, D>(
    deserializer: D,
) -> std::result::Result<SavedTurnStartCaller, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "saved-turn" => Ok(SavedTurnStartCaller::SavedTurn),
        "side-chat" => Ok(SavedTurnStartCaller::SideChat),
        "calendar" => Ok(SavedTurnStartCaller::Calendar),
        "calendar-manual" => Ok(SavedTurnStartCaller::CalendarManual),
        _ => Err(de::Error::custom("unknown Turn start caller")),
    }
}

fn deserialize_turn_start_action<'de, D>(
    deserializer: D,
) -> std::result::Result<SavedTurnStartAction, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "begin" => Ok(SavedTurnStartAction::Begin),
        "claim" => Ok(SavedTurnStartAction::Claim),
        _ => Err(de::Error::custom("unknown Turn start action")),
    }
}

fn deserialize_turn_start_id<'de, D>(deserializer: D) -> std::result::Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    let id = String::deserialize(deserializer)?;
    if !valid_saved_identifier(&id) {
        return Err(de::Error::custom("invalid Turn start identifier"));
    }
    Ok(id)
}

fn deserialize_turn_start_revision<'de, D>(deserializer: D) -> std::result::Result<u64, D::Error>
where
    D: Deserializer<'de>,
{
    let revision = u64::deserialize(deserializer)?;
    if !(1..=MAX_SAFE_JSON_REVISION).contains(&revision) {
        return Err(de::Error::custom(
            "invalid Turn start conversation revision",
        ));
    }
    Ok(revision)
}

fn deserialize_digest<'de, D>(deserializer: D) -> std::result::Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    let digest = String::deserialize(deserializer)?;
    Digest::parse(&digest).map_err(de::Error::custom)?;
    Ok(digest)
}

fn deserialize_settlement_revision<'de, D>(
    deserializer: D,
) -> std::result::Result<Option<u64>, D::Error>
where
    D: Deserializer<'de>,
{
    let revision = Option::deserialize(deserializer)?;
    if revision == Some(0) {
        return Err(de::Error::custom("invalid Turn settlement revision"));
    }
    Ok(revision)
}

fn deserialize_settlement_reference<'de, D>(
    deserializer: D,
) -> std::result::Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    let reference = String::deserialize(deserializer)?;
    if reference.len() > TURN_SETTLEMENT_REFERENCE_LIMIT || !reference.starts_with('{') {
        return Err(de::Error::custom(
            "invalid Turn settlement reference envelope",
        ));
    }
    serde_json::from_str::<&RawValue>(&reference).map_err(de::Error::custom)?;
    // Preserve the exact owner JSON and allow whitespace within string values.
    let mut in_string = false;
    let mut escaped = false;
    for byte in reference.bytes() {
        if escaped {
            escaped = false;
        } else if in_string && byte == b'\\' {
            escaped = true;
        } else if byte == b'"' {
            in_string = !in_string;
        } else if !in_string && byte.is_ascii_whitespace() {
            return Err(de::Error::custom(
                "Turn settlement reference must be compact JSON",
            ));
        }
    }
    Ok(reference)
}

fn deserialize_application_presentation_role<'de, D>(
    deserializer: D,
) -> std::result::Result<ApplicationPresentationRole, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "settings" => Ok(ApplicationPresentationRole::Settings),
        "catalog" => Ok(ApplicationPresentationRole::Catalog),
        "commands" => Ok(ApplicationPresentationRole::Commands),
        _ => Err(de::Error::custom("unknown application presentation role")),
    }
}

fn deserialize_model_resolve_role<'de, D>(
    deserializer: D,
) -> std::result::Result<OriginalModelResolveRole, D::Error>
where
    D: Deserializer<'de>,
{
    let role = String::deserialize(deserializer)?;
    model_resolve_role(&role).ok_or_else(|| de::Error::custom("unknown Model Resolve role"))
}

fn model_resolve_role(role: &str) -> Option<OriginalModelResolveRole> {
    match role {
        "quote-generate" => Some(OriginalModelResolveRole::QuoteGenerate),
        "quote-stream" => Some(OriginalModelResolveRole::QuoteStream),
        "generate" => Some(OriginalModelResolveRole::Generate),
        "stream" => Some(OriginalModelResolveRole::Stream),
        _ => None,
    }
}

fn deserialize_model_identifier<'de, D>(deserializer: D) -> std::result::Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    let identifier = String::deserialize(deserializer)?;
    let mut bytes = identifier.bytes();
    if identifier.len() <= 256
        && bytes
            .next()
            .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || b"._:/-".contains(&byte))
    {
        Ok(identifier)
    } else {
        Err(de::Error::custom("invalid Model Resolve identifier"))
    }
}

fn deserialize_present_object<'de, T, D>(
    deserializer: D,
) -> std::result::Result<Option<T>, D::Error>
where
    T: Deserialize<'de>,
    D: Deserializer<'de>,
{
    let object = deserialize_object(deserializer)?;
    serde_json::from_str(object.get())
        .map(Some)
        .map_err(de::Error::custom)
}

fn deserialize_saved_local_chat_role<'de, D>(
    deserializer: D,
) -> std::result::Result<OriginalSavedLocalChatRole, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "generate" => Ok(OriginalSavedLocalChatRole::Generate),
        "stream" => Ok(OriginalSavedLocalChatRole::Stream),
        "side-chat-generate" => Ok(OriginalSavedLocalChatRole::SideChatGenerate),
        "side-chat-stream" => Ok(OriginalSavedLocalChatRole::SideChatStream),
        _ => Err(de::Error::custom("unknown Saved Local Chat role")),
    }
}

impl TryFrom<ProviderSnapshotWire> for ProviderSnapshot {
    type Error = &'static str;

    fn try_from(wire: ProviderSnapshotWire) -> std::result::Result<Self, Self::Error> {
        use tobkiri_harness_native::{OriginalReviewerHealthRole, OriginalWorkflowHealthCaller};
        use OriginalProviderSnapshotRole::{
            Adapter, CommandDatasource, ConversationCompleteHealth, Gateway, GatewayHealth,
            ModelAccessCatalog, ModelAccessRead, ModelSave, ModelSearch, ReviewerHealth,
            ShellSettings, Workflow, WorkflowHealth,
        };
        use OriginalWorkflowSnapshotCaller::{
            AudioSpeech, AudioTranscribe, Messages, MessagesAdapter, Text, TextAdapter,
        };
        use OriginalWorkflowSnapshotEntry::{
            Advance, Cancel, Selected, StepExecute, StepResume, StepRetry,
        };

        if let Some(entry) = wire.entry {
            let entry = match entry.as_str() {
                "run.advance" => Advance,
                "run.cancel" => Cancel,
                "run.selected" => Selected,
                "run.step.execute" => StepExecute,
                "run.step.resume" => StepResume,
                "run.step.retry" => StepRetry,
                _ => return Err("unknown Workflow Provider Snapshot entry"),
            };
            let workflow = |caller| Workflow { caller, entry };
            let health = |caller| WorkflowHealth { caller, entry };
            let role = match wire.role.as_str() {
                "workflow-text" => workflow(Text),
                "workflow-messages" => workflow(Messages),
                "workflow-text-adapter" => workflow(TextAdapter),
                "workflow-messages-adapter" => workflow(MessagesAdapter),
                "workflow-audio-transcribe" => workflow(AudioTranscribe),
                "workflow-audio-speech" => workflow(AudioSpeech),
                "workflow-health-text" => health(OriginalWorkflowHealthCaller::Text),
                "workflow-health-messages" => health(OriginalWorkflowHealthCaller::Messages),
                _ => return Err("Provider Snapshot entry requires a Workflow role"),
            };
            return Ok(Self { role });
        }
        let snapshot = |role| Ok(Self { role });
        match wire.role.as_str() {
            "generate" => return snapshot(Adapter(OriginalSavedLocalChatRole::Generate)),
            "stream" => return snapshot(Adapter(OriginalSavedLocalChatRole::Stream)),
            "side-chat-generate" => {
                return snapshot(Adapter(OriginalSavedLocalChatRole::SideChatGenerate))
            }
            "side-chat-stream" => {
                return snapshot(Adapter(OriginalSavedLocalChatRole::SideChatStream))
            }
            "shell-settings" => return snapshot(ShellSettings),
            "model-save" => return snapshot(ModelSave),
            "model-search" => return snapshot(ModelSearch),
            "command-datasource" => return snapshot(CommandDatasource),
            "model-access-read" => return snapshot(ModelAccessRead),
            "model-access-catalog" => return snapshot(ModelAccessCatalog),
            "conversation-complete-health" => return snapshot(ConversationCompleteHealth),
            "reviewer-health-quote" => {
                return snapshot(ReviewerHealth(OriginalReviewerHealthRole::Quote))
            }
            "reviewer-health-generate" => {
                return snapshot(ReviewerHealth(OriginalReviewerHealthRole::Generate))
            }
            _ => {}
        }
        let unknown_role = "unknown Provider Snapshot role or missing Workflow entry";
        let (wire_role, side_chat) = wire
            .role
            .strip_prefix("side-chat-")
            .map_or((wire.role.as_str(), false), |role| (role, true));
        let gateway_role = wire_role.strip_prefix("gateway-").ok_or(unknown_role)?;
        let (delivery, health) = gateway_role
            .strip_prefix("health-")
            .map_or((gateway_role, false), |role| (role, true));
        let role = model_resolve_role(delivery).ok_or(unknown_role)?;
        snapshot(if health {
            GatewayHealth { role, side_chat }
        } else {
            Gateway { role, side_chat }
        })
    }
}

fn deserialize_conversation_caller<'de, D>(
    deserializer: D,
) -> std::result::Result<ConversationReadCaller, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "shell" => Ok(ConversationReadCaller::Shell),
        "saved-turn" => Ok(ConversationReadCaller::SavedTurn),
        "saved-orchestration" => Ok(ConversationReadCaller::SavedOrchestration),
        _ => Err(de::Error::custom("unknown Conversation caller")),
    }
}

fn deserialize_saved_caller<'de, D>(
    deserializer: D,
) -> std::result::Result<OriginalSavedReceiptRole, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "saved-turn" => Ok(OriginalSavedReceiptRole::SavedTurn),
        "saved-reconcile" => Ok(OriginalSavedReceiptRole::SavedReconcile),
        "side-chat-turn" => Ok(OriginalSavedReceiptRole::SideChatTurn),
        "side-chat-reconcile" => Ok(OriginalSavedReceiptRole::SideChatReconcile),
        _ => Err(de::Error::custom("unknown saved caller")),
    }
}

fn deserialize_saved_receipt_turn_id<'de, D>(
    deserializer: D,
) -> std::result::Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    let turn_id = String::deserialize(deserializer)?;
    if turn_id.chars().count() > SAVED_RECEIPT_TURN_ID_MAX_CHARS {
        return Err(de::Error::custom(
            "saved receipt turn ID exceeds its envelope",
        ));
    }
    // The legacy owner validates the original identifier after reading the document.
    Ok(turn_id)
}

fn deserialize_turn_read_id<'de, D>(deserializer: D) -> std::result::Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    let turn_id = String::deserialize(deserializer)?;
    // Match the Python Turn Resource identifier envelope, including str.strip().
    let trimmed = turn_id
        .trim_matches(|ch: char| ch.is_whitespace() || matches!(ch, '\u{001c}'..='\u{001f}'));
    if turn_id.is_empty() || turn_id != trimmed || turn_id.chars().count() > TURN_READ_ID_MAX_CHARS
    {
        return Err(de::Error::custom("invalid Turn read identifier"));
    }
    Ok(turn_id)
}

fn deserialize_present<'de, T, D>(deserializer: D) -> std::result::Result<Option<T>, D::Error>
where
    T: Deserialize<'de>,
    D: Deserializer<'de>,
{
    T::deserialize(deserializer).map(Some)
}

fn deserialize_object<'de, D>(deserializer: D) -> std::result::Result<&'de RawValue, D::Error>
where
    D: Deserializer<'de>,
{
    let plan = <&RawValue>::deserialize(deserializer)?;
    if !plan.get().starts_with('{') {
        return Err(de::Error::custom("Registry input must be an object"));
    }
    Ok(plan)
}

fn deserialize_optional_object<'de, D>(
    deserializer: D,
) -> std::result::Result<Option<&'de RawValue>, D::Error>
where
    D: Deserializer<'de>,
{
    deserialize_object(deserializer).map(Some)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    profile_id: String,
    profile_revision: String,
    activation_id: String,
    plan_digest: String,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct Request {
    connection_name: String,
    endpoint: String,
    catalog_provider_id: Option<String>,
    display_name: Option<String>,
    capabilities: Option<Vec<String>>,
}

#[derive(Clone, Copy)]
enum Stop {
    Cancelled,
    Deadline,
    Invalid,
    Unavailable,
    Broken,
}

impl Stop {
    fn as_error(self) -> anyhow::Error {
        let error = match self {
            Self::Cancelled => NativeDataIoError::Cancelled,
            Self::Deadline => NativeDataIoError::Deadline,
            Self::Invalid => return anyhow!("invalid Registry authorization frame"),
            Self::Unavailable => NativeDataIoError::Unavailable,
            Self::Broken => NativeDataIoError::Io,
        };
        NativeArtifactError::from(error).into()
    }

    fn reply(self) -> Option<&'static str> {
        match self {
            Self::Cancelled | Self::Deadline => Some("cancelled"),
            Self::Invalid => Some("invalid"),
            Self::Unavailable => Some("unavailable"),
            Self::Broken => None,
        }
    }
}

fn validate_start_size(tag: u8, size: usize) -> Result<()> {
    let limit = match tag {
        START => JSON_LIMIT,
        START_APPEND => CONVERSATION_APPEND_LIMIT,
        _ => bail!("invalid native Registry start tag"),
    };
    ensure!(
        (1..=limit).contains(&size),
        "invalid native Registry start frame size"
    );
    Ok(())
}

fn parse_start(tag: u8, bytes: &[u8]) -> Result<Start<'_>> {
    let start: Start<'_> =
        serde_json::from_slice(bytes).map_err(|_| anyhow!("invalid native Registry start"))?;
    ensure!(
        (start.turn_start.is_none()
            && start.saved_local_chat.is_none()
            && start.provider_snapshot.is_none()
            && start.application_presentation.is_none()
            && start.conversation_complete.is_none())
            || bytes.iter().find(|byte| !byte.is_ascii_whitespace()) == Some(&b'{'),
        "native Registry start requires a named operation"
    );
    ensure!(
        (tag == START_APPEND) == start.conversation_append.is_some(),
        "native Registry start tag does not match its operation"
    );
    Ok(start)
}

/// Authenticate and check launch proof before a connection may accept its body.
pub(crate) fn admit_connection(
    app: &AppHandle,
    mut stream: UnixStream,
) -> Result<Option<AdmittedConnection>> {
    let peer = authenticate_peer(&stream)?;
    suppress_sigpipe(&stream)?;
    stream.set_nonblocking(true)?;
    let start_deadline = Instant::now() + FRAME_TIMEOUT;
    let (tag, size) = read_header(&mut stream, start_deadline)?;
    validate_start_size(tag, size)?;
    let mut bytes = vec![0; size];
    read_exact(&mut stream, &mut bytes, start_deadline)?;
    let start = parse_start(tag, &bytes)?;
    ensure!(
        start.runtime_instance.len() == 32
            && start
                .runtime_instance
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')),
        "invalid Provider prepare runtime instance"
    );
    ensure!(start.security_epoch != 0, "invalid Provider prepare epoch");
    let deadline = host_deadline(start.deadline_ns)?;
    let request = match start.request_start() {
        Ok(request) => request,
        Err(error)
            if (start.conversation_complete.is_some()
                || start.model_access_projection.is_some())
                && error.is::<SavedOrchestrationStepError>() =>
        {
            let reply = if start.model_access_projection.is_some() {
                ReplyRole::ModelAccessProjection
            } else {
                ReplyRole::ConversationComplete
            };
            send_frame(
                &mut stream,
                FAILURE,
                reply
                    .failure_code(WireAuthority::AwaitingLive, &error)
                    .as_bytes(),
                deadline.min(Instant::now() + FRAME_TIMEOUT),
            )?;
            return Ok(None);
        }
        Err(error) => return Err(error),
    };
    let needs_active_host = matches!(
        &request,
        RequestStart::SavedOrchestration { .. }
            | RequestStart::SavedInput(_)
            | RequestStart::ApplicationPresentation { .. }
            | RequestStart::ConversationComplete { .. }
            | RequestStart::ModelAccessProjection { .. }
            | RequestStart::AiGatewayData { .. }
            | RequestStart::SavedLocalChat(_)
            | RequestStart::Inline(
                NativeRegistryRequest::ProviderSnapshot { .. }
                    | NativeRegistryRequest::ConversationList
                    | NativeRegistryRequest::TurnRead { .. }
                    | NativeRegistryRequest::LinkedConversationContext { .. }
                    | NativeRegistryRequest::LinkedConversationRead { .. }
                    | NativeRegistryRequest::TurnStart { .. }
                    | NativeRegistryRequest::TurnSettlement { .. }
                    | NativeRegistryRequest::ConversationAppend { .. }
                    | NativeRegistryRequest::ConversationConfirmation { .. }
            )
    );
    let identity = ExecutionProfileIdentity::new(
        start.identity.profile_id,
        start.identity.profile_revision,
        start.identity.activation_id,
        start.identity.plan_digest,
    )?;
    let manager = app
        .try_state::<Arc<ApplicationProcessManager>>()
        .context("native Provider preparation Application manager is unavailable")?
        .inner()
        .clone();
    let Some(candidate) = manager.admitted_provider_prepare(&identity)? else {
        // An absent or stale admission cannot authorize fallback for an active Host operation.
        let (tag, payload): (u8, &[u8]) = if needs_active_host {
            (FAILURE, b"unavailable")
        } else {
            (INAPPLICABLE, &[])
        };
        send_frame(
            &mut stream,
            tag,
            payload,
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        )?;
        return Ok(None);
    };
    manager.require_provider_prepare_peer(&identity, &peer)?;
    ensure!(
        candidate.matches_process(&peer),
        "Provider prepare peer differs from the admitted launch"
    );
    let matches_runtime = candidate.matches_runtime_instance(&start.runtime_instance);
    if candidate.verified_source().security_epoch != start.security_epoch
        || (needs_active_host && !matches_runtime)
    {
        send_frame(
            &mut stream,
            FAILURE,
            b"unavailable",
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        )?;
        return Ok(None);
    }
    manager.require_provider_prepare_admission(&candidate)?;
    if let RequestStart::SavedLocalChat(chat) = &request {
        if let Err(code) = chat.validate() {
            send_frame(
                &mut stream,
                FAILURE,
                code.as_bytes(),
                deadline.min(Instant::now() + FRAME_TIMEOUT),
            )?;
            return Ok(None);
        }
    }
    let proof = candidate.verified_source();
    let has_launch_proof = match &request {
        RequestStart::Inline(operation) => operation.has_launch_proof(proof),
        RequestStart::SavedInput(input) => proof.permits_saved_input_for(input.caller),
        RequestStart::SavedOrchestration { .. } => proof.permits_saved_orchestration(),
        RequestStart::ApplicationPresentation { role, .. } => {
            proof.permits_application_presentation(*role)
        }
        RequestStart::ConversationComplete { .. } => proof.permits_conversation_complete(),
        RequestStart::ModelAccessProjection { phase, .. } => {
            proof.permits_model_access_projection(phase.role())
        }
        RequestStart::AiGatewayData { operation, .. } => operation.has_launch_proof(proof),
        RequestStart::SavedLocalChat(chat) => proof.permits_saved_local_chat(chat.role),
    };
    if !matches_runtime || !has_launch_proof {
        request.reject_missing_launch_proof(
            &mut stream,
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        )?;
        return Ok(None);
    }
    Ok(Some(AdmittedConnection {
        stream,
        request,
        manager,
        candidate,
        deadline,
    }))
}

/// A managed Host connection authenticated against its current launch.
pub(crate) struct AdmittedConnection {
    stream: UnixStream,
    request: RequestStart,
    manager: Arc<ApplicationProcessManager>,
    candidate: Arc<ProviderPrepareListener>,
    deadline: Instant,
}

impl AdmittedConnection {
    /// Return whether the exchange owns the saved local chat transport.
    pub(crate) fn is_saved_local_chat(&self) -> bool {
        matches!(self.request, RequestStart::SavedLocalChat(_))
    }

    /// Reject an authenticated exchange before accepting any body.
    pub(crate) fn reject_unavailable(mut self) -> Result<()> {
        send_frame(
            &mut self.stream,
            FAILURE,
            b"unavailable",
            self.deadline.min(Instant::now() + FRAME_TIMEOUT),
        )
    }

    /// Complete the admitted Registry exchange.
    pub(crate) fn run(self, app: &AppHandle) -> Result<()> {
        if self.is_saved_local_chat()
            && self
                .manager
                .require_provider_prepare_admission(&self.candidate)
                .is_err()
        {
            return self.reject_unavailable();
        }
        let Self {
            mut stream,
            request,
            manager,
            candidate,
            deadline,
        } = self;
        let operation = match request {
            RequestStart::Inline(operation) => operation,
            RequestStart::SavedInput(SavedInputStart {
                caller,
                mode,
                input_bytes,
            }) => {
                let connection = Connection::new(
                    manager,
                    candidate,
                    stream,
                    deadline,
                    ReplyRole::SavedInput(mode),
                );
                let input_connection = Arc::clone(&connection);
                return connection.run(
                    app,
                    NativeRegistryRequest::SavedInput {
                        caller,
                        mode,
                        input_bytes,
                        receive_input: Box::new(move |deadline| {
                            input_connection.receive_funded_input(input_bytes, deadline)
                        }),
                    },
                );
            }
            RequestStart::SavedOrchestration { input_bytes } => {
                let body_deadline = deadline.min(Instant::now() + FRAME_TIMEOUT);
                let Some(input) =
                    receive_orchestration_input(&mut stream, input_bytes, body_deadline)?
                else {
                    return send_frame(&mut stream, FAILURE, b"cancelled", body_deadline);
                };
                NativeRegistryRequest::SavedOrchestration { input }
            }
            RequestStart::ApplicationPresentation { role, input_bytes } => {
                let connection = Connection::new(
                    manager,
                    candidate,
                    stream,
                    deadline,
                    ReplyRole::ApplicationPresentation,
                );
                let input_connection = Arc::clone(&connection);
                return connection.run(
                    app,
                    NativeRegistryRequest::ApplicationPresentation {
                        role,
                        input_bytes,
                        receive_input: Box::new(move |deadline| {
                            input_connection.receive_funded_input(input_bytes, deadline)
                        }),
                    },
                );
            }
            RequestStart::ConversationComplete { phase, input_bytes } => {
                let connection = Connection::new(
                    manager,
                    candidate,
                    stream,
                    deadline,
                    ReplyRole::ConversationComplete,
                );
                let input_connection = Arc::clone(&connection);
                return connection.run(
                    app,
                    NativeRegistryRequest::ConversationComplete {
                        phase,
                        input_bytes,
                        receive_input: Box::new(move |deadline| {
                            input_connection.receive_funded_input(input_bytes, deadline)
                        }),
                    },
                );
            }
            RequestStart::ModelAccessProjection { phase, input_bytes } => {
                let connection = Connection::new(
                    manager,
                    candidate,
                    stream,
                    deadline,
                    ReplyRole::ModelAccessProjection,
                );
                let input_connection = Arc::clone(&connection);
                return connection.run(
                    app,
                    NativeRegistryRequest::ModelAccessProjection {
                        phase,
                        input_bytes,
                        receive_input: Box::new(move |deadline| {
                            input_connection.receive_funded_input(input_bytes, deadline)
                        }),
                    },
                );
            }
            RequestStart::AiGatewayData {
                operation,
                input_bytes,
            } => {
                let body_deadline = deadline.min(Instant::now() + FRAME_TIMEOUT);
                let Some(input) =
                    receive_orchestration_input(&mut stream, input_bytes, body_deadline)?
                else {
                    return send_frame(&mut stream, FAILURE, b"cancelled", body_deadline);
                };
                NativeRegistryRequest::AiGatewayData { operation, input }
            }
            RequestStart::SavedLocalChat(chat) => {
                let body_deadline = deadline.min(Instant::now() + FRAME_TIMEOUT);
                let Some(input) =
                    receive_saved_local_chat_input(&mut stream, chat.input_bytes, body_deadline)?
                else {
                    return Ok(());
                };
                NativeRegistryRequest::SavedLocalChat {
                    role: chat.role,
                    connection_id: chat.connection_id,
                    record_revision: chat.record_revision,
                    endpoint: chat.endpoint,
                    input,
                    project_response: chat.project_response,
                    compile_request: chat.compile_request,
                    compile_policy: chat.compile_policy,
                }
            }
        };
        let reply = match &operation {
            NativeRegistryRequest::Provider { .. } => ReplyRole::Provider,
            NativeRegistryRequest::ModelList => ReplyRole::ModelList,
            NativeRegistryRequest::ProviderSnapshot { .. } => ReplyRole::ProviderSnapshot,
            NativeRegistryRequest::ModelAccessProjection { .. } => ReplyRole::ModelAccessProjection,
            NativeRegistryRequest::ModelSave { .. } => ReplyRole::ModelSave,
            NativeRegistryRequest::ConversationGet { .. }
            | NativeRegistryRequest::ConversationList
            | NativeRegistryRequest::LinkedConversationContext { .. }
            | NativeRegistryRequest::LinkedConversationRead { .. }
            | NativeRegistryRequest::ConversationReceipt { .. } => ReplyRole::ConversationRead,
            NativeRegistryRequest::ConversationConfirmation { .. } => {
                ReplyRole::ConversationConfirmation
            }
            NativeRegistryRequest::ConversationAppend { .. } => ReplyRole::ConversationAppend,
            NativeRegistryRequest::ModelResolve { .. } => ReplyRole::ModelResolve,
            NativeRegistryRequest::TurnRead { .. } => ReplyRole::TurnRead,
            NativeRegistryRequest::SavedInput { mode, .. } => ReplyRole::SavedInput(*mode),
            NativeRegistryRequest::TurnStart { action, .. } => ReplyRole::TurnStart(*action),
            NativeRegistryRequest::TurnSettlement { .. } => ReplyRole::TurnSettlement,
            NativeRegistryRequest::SavedOrchestration { .. } => ReplyRole::SavedOrchestration,
            NativeRegistryRequest::ApplicationPresentation { .. } => {
                ReplyRole::ApplicationPresentation
            }
            NativeRegistryRequest::ConversationComplete { .. } => ReplyRole::ConversationComplete,
            NativeRegistryRequest::AiGatewayData { .. } => ReplyRole::AiGatewayData,
            NativeRegistryRequest::SavedLocalChat { .. } => ReplyRole::SavedLocalChat,
        };
        Connection::new(manager, candidate, stream, deadline, reply).run(app, operation)
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ReplyRole {
    Provider,
    ModelList,
    ProviderSnapshot,
    ModelAccessProjection,
    ModelSave,
    ConversationRead,
    ConversationConfirmation,
    ConversationAppend,
    ModelResolve,
    TurnRead,
    TurnStart(SavedTurnStartAction),
    SavedInput(SavedInputMode),
    TurnSettlement,
    SavedOrchestration,
    ApplicationPresentation,
    ConversationComplete,
    AiGatewayData,
    SavedLocalChat,
}

impl ReplyRole {
    fn uses_funded_input(self) -> bool {
        matches!(
            self,
            Self::ApplicationPresentation
                | Self::ConversationComplete
                | Self::ModelAccessProjection
                | Self::SavedInput(_)
        )
    }

    fn effect_check(self) -> Option<AuthorityCheck> {
        if self == Self::SavedLocalChat {
            Some(AuthorityCheck::Dispatch)
        } else if self.permits_publication() {
            Some(AuthorityCheck::Publication)
        } else {
            None
        }
    }

    fn permits_publication(self) -> bool {
        self.uses_conversation_publication()
            || matches!(
                self,
                Self::ModelSave
                    | Self::TurnStart(_)
                    | Self::TurnSettlement
                    | Self::SavedInput(SavedInputMode::Bind)
            )
    }

    fn uses_conversation_publication(self) -> bool {
        matches!(
            self,
            Self::ConversationConfirmation | Self::ConversationAppend
        )
    }

    fn result_limit(self, authority: WireAuthority) -> (usize, &'static str) {
        match self {
            Self::Provider => (JSON_LIMIT, "limit-exceeded"),
            Self::ModelList => (MODEL_RESULT_LIMIT, "legacy-read-required"),
            Self::ProviderSnapshot | Self::ModelAccessProjection => (
                PROVIDER_SNAPSHOT_REPLY_LIMIT,
                provider_snapshot_fallback(authority),
            ),
            Self::ModelSave => (MODEL_RESULT_LIMIT, "limit-exceeded"),
            Self::ConversationRead => (CONVERSATION_READ_REPLY_LIMIT, "legacy-read-required"),
            Self::ConversationConfirmation => (
                CONVERSATION_CONFIRMATION_LIMIT,
                conversation_publication_fallback(authority),
            ),
            Self::ConversationAppend => (
                CONVERSATION_APPEND_LIMIT,
                conversation_publication_fallback(authority),
            ),
            Self::ModelResolve => (MODEL_RESOLVE_REPLY_LIMIT, "legacy-read-required"),
            Self::TurnRead => (TURN_READ_REPLY_LIMIT, "limit-exceeded"),
            Self::SavedInput(_) => (SAVED_INPUT_FRAME_LIMIT, "limit-exceeded"),
            Self::SavedOrchestration => (SAVED_ORCHESTRATION_FRAME_LIMIT, "limit-exceeded"),
            Self::ApplicationPresentation => {
                (APPLICATION_PRESENTATION_RESULT_LIMIT, "limit-exceeded")
            }
            Self::ConversationComplete => (CONVERSATION_COMPLETE_RESULT_LIMIT, "limit-exceeded"),
            Self::AiGatewayData => (AI_GATEWAY_DATA_LIMIT, "limit-exceeded"),
            Self::SavedLocalChat => (LEGACY_LOCAL_CHAT_RESPONSE_LIMIT, "invalid-response"),
            Self::TurnStart(_) if authority == WireAuthority::PublicationPermitted => {
                (TURN_START_REPLY_LIMIT, "unavailable")
            }
            Self::TurnStart(_) => (TURN_START_REPLY_LIMIT, "limit-exceeded"),
            Self::TurnSettlement if authority == WireAuthority::PublicationPermitted => {
                (TURN_SETTLEMENT_RESULT_LIMIT, "unavailable")
            }
            Self::TurnSettlement => (TURN_SETTLEMENT_RESULT_LIMIT, "limit-exceeded"),
        }
    }

    fn failure_code(self, authority: WireAuthority, error: &anyhow::Error) -> &'static str {
        if matches!(self, Self::ProviderSnapshot | Self::ModelAccessProjection) {
            return if error.is::<ProviderSnapshotRequiresLegacyRead>() {
                provider_snapshot_fallback(authority)
            } else {
                match (self, error.downcast_ref::<SavedOrchestrationStepError>()) {
                    (Self::ModelAccessProjection, Some(SavedOrchestrationStepError::Invalid)) => {
                        "invalid"
                    }
                    (Self::ModelAccessProjection, Some(SavedOrchestrationStepError::Capacity)) => {
                        "limit-exceeded"
                    }
                    _ => match failure_code(error) {
                        "legacy-read-required" => "unavailable",
                        code => code,
                    },
                }
            };
        }

        if self == Self::SavedLocalChat {
            if let Some(error) = error.downcast_ref::<OriginalLocalChatPolicyError>() {
                return match (authority, error) {
                    (WireAuthority::PublicationPermitted, _) => "unavailable",
                    (_, OriginalLocalChatPolicyError::Denied) => "policy-model-denied",
                    (_, OriginalLocalChatPolicyError::Model) => "policy-model-invalid",
                    (_, OriginalLocalChatPolicyError::Parameters) => "policy-parameters-invalid",
                };
            }
            if authority != WireAuthority::PublicationPermitted
                && error.is::<SavedLocalChatRequiresLegacyRequest>()
            {
                return "legacy-request";
            }
            use LegacyLocalChatHttpError as Transport;
            use LegacyLocalChatSourceError as Source;
            return match error.downcast_ref::<OriginalLocalChatError>() {
                Some(OriginalLocalChatError::Source(Source::SelectionChanged)) => "denied",
                Some(
                    OriginalLocalChatError::Source(
                        Source::InvalidSelection | Source::InvalidDocument,
                    )
                    | OriginalLocalChatError::Transport(Transport::InvalidRequest),
                ) => "invalid",
                Some(OriginalLocalChatError::Transport(
                    Transport::Deadline | Transport::Revoked,
                )) => "cancelled",
                Some(OriginalLocalChatError::Transport(Transport::ResponseTooLarge)) => {
                    "invalid-response"
                }
                Some(OriginalLocalChatError::Transport(Transport::Capacity)) => "limit-exceeded",
                Some(
                    OriginalLocalChatError::Transport(Transport::Unavailable)
                    | OriginalLocalChatError::RegistryUnavailable,
                ) => "unavailable",
                None => match failure_code(error) {
                    code @ ("cancelled" | "denied" | "invalid" | "limit-exceeded") => code,
                    _ => "unavailable",
                },
            };
        }
        if self == Self::AiGatewayData
            && authority == WireAuthority::Live
            && error.is::<AiGatewayDataRequiresLegacyCodec>()
        {
            return "legacy-read-required";
        }
        if matches!(
            self,
            Self::SavedOrchestration
                | Self::ApplicationPresentation
                | Self::ConversationComplete
                | Self::AiGatewayData
        ) {
            return match error.downcast_ref::<SavedOrchestrationStepError>() {
                Some(SavedOrchestrationStepError::Invalid) => "invalid",
                Some(SavedOrchestrationStepError::Capacity) => "limit-exceeded",
                _ => match failure_code(error) {
                    "cancelled" => "cancelled",
                    "denied" => "denied",
                    "limit-exceeded" => "limit-exceeded",
                    _ => "unavailable",
                },
            };
        }
        if self.uses_conversation_publication() {
            if authority == WireAuthority::PublicationPermitted {
                return "publication-unconfirmed";
            }
            if error.is::<ConversationWriteRequiresLegacyRead>()
                || matches!(
                    error.downcast_ref::<NativeArtifactError>(),
                    Some(NativeArtifactError::ConversationLimitBeforePublish)
                )
            {
                return conversation_publication_fallback(authority);
            }
            return match error.downcast_ref::<NativeArtifactError>() {
                Some(NativeArtifactError::ConversationPublicationUnconfirmed(_)) => {
                    "publication-unconfirmed"
                }
                _ => failure_code(error),
            };
        }
        use LegacySavedTurnSettlementError as SettlementError;
        if self == Self::TurnRead {
            return match error.downcast_ref::<SettlementError>() {
                Some(SettlementError::RequiresLegacyCodec) if authority == WireAuthority::Live => {
                    "legacy-read-required"
                }
                Some(SettlementError::InvalidRequest | SettlementError::InvalidRecord) => "invalid",
                Some(SettlementError::Capacity) => "limit-exceeded",
                Some(SettlementError::Revoked) => "cancelled",
                Some(SettlementError::Storage) => "internal",
                Some(_) => "unavailable",
                None => match failure_code(error) {
                    "legacy-read-required" => "unavailable",
                    code => code,
                },
            };
        }
        if !matches!(
            self,
            Self::TurnStart(_) | Self::TurnSettlement | Self::SavedInput(_)
        ) {
            return failure_code(error);
        }
        if authority == WireAuthority::PublicationPermitted {
            return "unavailable";
        }
        let needs_legacy_codec = match self {
            Self::TurnStart(_) => error.is::<TurnStartRequiresLegacyCodec>(),
            Self::SavedInput(_) => matches!(
                error.downcast_ref::<SettlementError>(),
                Some(SettlementError::RequiresLegacyCodec)
            ),
            _ => error.is::<TurnSettlementRequiresLegacyCodec>(),
        };
        if authority == WireAuthority::Live && needs_legacy_codec {
            return "legacy-read-required";
        }
        match error.downcast_ref::<SettlementError>() {
            Some(SettlementError::Conflict) => "conflict",
            Some(
                SettlementError::InvalidRequest
                | SettlementError::InvalidRecord
                | SettlementError::InvalidReference,
            ) => "invalid",
            Some(SettlementError::NotFound) => "not-found",
            Some(SettlementError::Capacity) => "limit-exceeded",
            Some(SettlementError::Revoked) => "cancelled",
            Some(SettlementError::Storage) => "internal",
            Some(_) => "unavailable",
            None => match failure_code(error) {
                "legacy-read-required" => "unavailable",
                code => code,
            },
        }
    }

    fn validate_result(self, authority: WireAuthority, output: &[u8]) -> Result<(), &'static str> {
        let (limit, overflow_code) = self.result_limit(authority);
        if output.len() > limit {
            return Err(overflow_code);
        }
        if matches!(self, Self::ProviderSnapshot | Self::ModelAccessProjection) {
            if authority != WireAuthority::Live {
                return Err("unavailable");
            }
            // The owner can emit Python numeric literals outside serde_json.
            if !(output.starts_with(b"{") && output.ends_with(b"}")) {
                return Err("internal");
            }
        }
        if self == Self::SavedLocalChat && authority != WireAuthority::PublicationPermitted {
            return Err("unavailable");
        }
        if let Self::TurnStart(action) = self {
            if authority == WireAuthority::AwaitingLive {
                return Err("unavailable");
            }
            let permitted = authority == WireAuthority::PublicationPermitted;
            let malformed = if permitted { "unavailable" } else { "internal" };
            let text = std::str::from_utf8(output).map_err(|_| malformed)?;
            if !text.trim_start().starts_with('{') {
                return Err(malformed);
            }
            let result: TurnStartReply<'_> = serde_json::from_str(text).map_err(|_| malformed)?;
            if result.claimed != (action == SavedTurnStartAction::Claim && permitted) {
                return Err(malformed);
            }
            if result.turn.get().len() > TURN_READ_REPLY_LIMIT {
                return Err(overflow_code);
            }
        }
        if let Self::SavedInput(mode) = self {
            let live = match mode {
                SavedInputMode::Restore => authority == WireAuthority::Live,
                SavedInputMode::Bind => matches!(
                    authority,
                    WireAuthority::Live | WireAuthority::PublicationPermitted
                ),
            };
            if !live {
                return Err("unavailable");
            }
            let reply: SavedInputReply<'_> =
                serde_json::from_slice(output).map_err(|_| "internal")?;
            let accepted = reply.accepted.get();
            if !(accepted.starts_with('{')
                || (mode == SavedInputMode::Restore && accepted == "null"))
            {
                return Err("internal");
            }
        }
        if matches!(
            self,
            Self::TurnRead
                | Self::SavedOrchestration
                | Self::ApplicationPresentation
                | Self::ConversationComplete
                | Self::AiGatewayData
        ) {
            if authority != WireAuthority::Live {
                return Err("unavailable");
            }
            let text = std::str::from_utf8(output).map_err(|_| "internal")?;
            let raw = serde_json::from_str::<&RawValue>(text).map_err(|_| "internal")?;
            if !(raw.get().starts_with('{') || (self == Self::TurnRead && raw.get() == "null")) {
                return Err("internal");
            }
        }
        Ok(())
    }
}

fn provider_snapshot_fallback(authority: WireAuthority) -> &'static str {
    match authority {
        WireAuthority::Live => "legacy-read-required",
        _ => "unavailable",
    }
}

fn conversation_publication_fallback(authority: WireAuthority) -> &'static str {
    match authority {
        WireAuthority::AwaitingLive => "unavailable",
        WireAuthority::Live => "requires-legacy-read",
        WireAuthority::PublicationPermitted => "publication-unconfirmed",
    }
}

struct Connection {
    manager: Arc<ApplicationProcessManager>,
    candidate: Arc<ProviderPrepareListener>,
    // The worker's authorization checks and the idle actor share one reader.
    wire: Mutex<Wire>,
    deadline: Arc<ProviderPrepareDeadline>,
    reply: ReplyRole,
}

struct Wire {
    stream: UnixStream,
    stop: Option<Stop>,
    authority: WireAuthority,
    funded_input_received: bool,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum WireAuthority {
    AwaitingLive,
    Live,
    PublicationPermitted,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum AuthorityCheck {
    Live,
    Publication,
    Dispatch,
}

impl Connection {
    fn new(
        manager: Arc<ApplicationProcessManager>,
        candidate: Arc<ProviderPrepareListener>,
        stream: UnixStream,
        deadline: Instant,
        reply: ReplyRole,
    ) -> Arc<Self> {
        Arc::new(Self {
            manager,
            candidate,
            wire: Mutex::new(Wire::new(stream)),
            deadline: Arc::new(ProviderPrepareDeadline::new(deadline)),
            reply,
        })
    }

    fn run(self: &Arc<Self>, app: &AppHandle, operation: NativeRegistryRequest) -> Result<()> {
        let check_connection = Arc::clone(self);
        let publish_permit = self.reply.effect_check().map(|check| {
            let connection = Arc::clone(self);
            Arc::new(move || connection.authorize(check))
                as Arc<dyn Fn() -> Result<()> + Send + Sync>
        });
        let pending = match start_provider_prepare(
            app,
            Arc::clone(&self.candidate),
            operation,
            Arc::clone(&self.deadline),
            Arc::new(move || check_connection.check_current()),
            publish_permit,
        ) {
            Ok(pending) => pending,
            Err(error) => {
                return self.send_failure(
                    self.reply.failure_code(WireAuthority::AwaitingLive, &error),
                    self.deadline.current(),
                )
            }
        };
        self.finish(pending)
    }

    fn receive_funded_input(&self, input_bytes: usize, deadline: Instant) -> Result<Vec<u8>> {
        let mut wire = self.lock_wire()?;
        self.checkpoint(&mut wire)?;
        let input =
            wire.receive_funded_input(input_bytes, deadline.min(Instant::now() + FRAME_TIMEOUT))?;
        self.checkpoint(&mut wire)?;
        Ok(input)
    }

    fn checkpoint(&self, wire: &mut Wire) -> Result<()> {
        if let Some(stop) = wire.stop {
            return Err(stop.as_error());
        }
        if Instant::now() >= self.deadline.current() {
            wire.stop = Some(Stop::Deadline);
            return Err(Stop::Deadline.as_error());
        }
        if self
            .manager
            .require_provider_prepare_admission(&self.candidate)
            .is_err()
        {
            wire.stop = Some(Stop::Unavailable);
            return Err(Stop::Unavailable.as_error());
        }
        Ok(())
    }

    fn check_current(&self) -> Result<()> {
        self.authorize(AuthorityCheck::Live)
    }

    fn authorize(&self, check: AuthorityCheck) -> Result<()> {
        let mut wire = self.lock_wire()?;
        self.checkpoint(&mut wire)?;
        if self.reply.uses_funded_input() {
            ensure!(
                wire.funded_input_received
                    && (check == AuthorityCheck::Live
                        || (self.reply == ReplyRole::SavedInput(SavedInputMode::Bind)
                            && check == AuthorityCheck::Publication)),
                "native Registry DATA request is outside its live phase"
            );
        }
        let deadline = self.deadline.current().min(Instant::now() + FRAME_TIMEOUT);
        let result = wire.exchange_authority(check, deadline);
        if result.is_err() && wire.stop.is_none() {
            wire.stop = Some(if Instant::now() >= self.deadline.current() {
                Stop::Deadline
            } else {
                Stop::Broken
            });
        }
        result?;
        self.checkpoint(&mut wire)
    }

    fn lock_wire(&self) -> Result<std::sync::MutexGuard<'_, Wire>> {
        self.wire
            .lock()
            .map_err(|_| anyhow!("Provider prepare connection is unavailable"))
    }

    fn observe_stop(&self) -> Result<Option<Stop>> {
        match self.wire.try_lock() {
            Ok(mut wire) => {
                if self.checkpoint(&mut wire).is_ok() {
                    let deadline = self.deadline.current().min(Instant::now() + FRAME_TIMEOUT);
                    let _ = wire.check_idle(deadline);
                }
                Ok(wire.stop)
            }
            Err(TryLockError::WouldBlock) => {
                Ok((Instant::now() >= self.deadline.current()).then_some(Stop::Deadline))
            }
            Err(TryLockError::Poisoned(_)) => {
                bail!("Provider prepare connection is unavailable")
            }
        }
    }

    fn finish(&self, pending: PendingProviderPrepare) -> Result<()> {
        loop {
            match self.observe_stop() {
                Ok(Some(stop)) => {
                    pending.cancel()?;
                    pending.join_until(Instant::now() + CLEANUP_WAIT)?;
                    return self.send_stopped(stop);
                }
                Ok(None) => {}
                Err(error) => {
                    pending.cancel()?;
                    pending.join_until(Instant::now() + CLEANUP_WAIT)?;
                    return Err(error);
                }
            }
            if pending.try_join()? {
                break;
            }
            thread::sleep(POLL_INTERVAL);
        }

        // finish_until makes one final CHECK against the issuing Host lease.
        // Only the physically joined worker may now yield a result to send.
        let result = pending.finish_until(self.deadline.current());
        let mut wire = self.lock_wire()?;
        let deadline = self.deadline.current().min(Instant::now() + FRAME_TIMEOUT);
        let ready = self
            .checkpoint(&mut wire)
            .and_then(|()| wire.check_idle(deadline));
        if ready.is_err() {
            let stop = wire.stop.unwrap_or(Stop::Broken);
            drop(wire);
            return self.send_stopped(stop);
        }
        match result {
            Ok(output) => {
                let (tag, body, publication) = match output {
                    RegistryResponse::Wire(body) => (PLAN, body, None),
                    RegistryResponse::ProjectedLocalChat(body) => {
                        ensure!(self.reply == ReplyRole::SavedLocalChat);
                        (PROJECTED_LOCAL_CHAT, body, None)
                    }
                    RegistryResponse::LinkedConversationRead {
                        wire: body,
                        publication,
                    } => {
                        ensure!(self.reply == ReplyRole::ConversationRead);
                        (PLAN, body, Some(publication))
                    }
                };
                match self.reply.validate_result(wire.authority, body.as_bytes()) {
                    Ok(()) => {
                        send_frame(&mut wire.stream, tag, body.as_bytes(), deadline)?;
                        if let Some(publication) = publication {
                            publication.commit();
                        }
                        Ok(())
                    }
                    Err(code) => send_frame(&mut wire.stream, FAILURE, code.as_bytes(), deadline),
                }
            }
            Err(error) => {
                let code = self.reply.failure_code(wire.authority, &error);
                send_frame(&mut wire.stream, FAILURE, code.as_bytes(), deadline)
            }
        }
    }

    fn send_failure(&self, code: &str, deadline: Instant) -> Result<()> {
        let mut wire = self.lock_wire()?;
        let code = if self.reply.uses_conversation_publication()
            && wire.authority == WireAuthority::PublicationPermitted
        {
            "publication-unconfirmed"
        } else {
            code
        };
        send_frame(
            &mut wire.stream,
            FAILURE,
            code.as_bytes(),
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        )
    }

    fn send_stopped(&self, stop: Stop) -> Result<()> {
        if let Some(code) = stop.reply() {
            self.send_failure(code, Instant::now() + FRAME_TIMEOUT)?;
        }
        Ok(())
    }
}

impl Wire {
    fn new(stream: UnixStream) -> Self {
        Self {
            stream,
            stop: None,
            authority: WireAuthority::AwaitingLive,
            funded_input_received: false,
        }
    }

    fn receive_funded_input(&mut self, input_bytes: usize, deadline: Instant) -> Result<Vec<u8>> {
        ensure!(
            !self.funded_input_received && self.authority == WireAuthority::AwaitingLive,
            "native Registry DATA body is outside its input phase"
        );
        self.check_idle(deadline)?;
        let received = (|| {
            send_frame(&mut self.stream, BODY_READY, &[], deadline)?;
            match read_header(&mut self.stream, deadline)? {
                (CANCEL, 0) => Ok(Err(Stop::Cancelled)),
                (ORCHESTRATION_INPUT, size) if size == input_bytes => {
                    read_input_bytes(&mut self.stream, input_bytes, deadline).map(Ok)
                }
                _ => Ok(Err(Stop::Invalid)),
            }
        })();
        let stop = match received {
            Ok(Ok(input)) => {
                self.funded_input_received = true;
                return Ok(input);
            }
            Ok(Err(stop)) => stop,
            Err(error)
                if error
                    .downcast_ref::<io::Error>()
                    .is_some_and(|error| error.kind() == io::ErrorKind::TimedOut) =>
            {
                Stop::Deadline
            }
            Err(error) => {
                self.stop = Some(Stop::Broken);
                return Err(error);
            }
        };
        self.stop = Some(stop);
        Err(stop.as_error())
    }

    fn exchange_authority(&mut self, check: AuthorityCheck, deadline: Instant) -> Result<()> {
        self.check_idle(deadline)?;
        if check != AuthorityCheck::Live {
            ensure!(
                self.authority == WireAuthority::Live,
                "Registry effect is outside its live phase"
            );
        }
        let (challenge, expected) = match check {
            AuthorityCheck::Live => (CHECK, LIVE),
            AuthorityCheck::Publication => (PUBLISH_CHECK, PUBLISH_PERMIT),
            AuthorityCheck::Dispatch => (DISPATCH_CHECK, DISPATCH_PERMIT),
        };
        send_frame(&mut self.stream, challenge, &[], deadline)?;
        let header = read_header(&mut self.stream, deadline)?;
        if header == (CANCEL, 0) {
            self.stop = Some(Stop::Cancelled);
            return Err(Stop::Cancelled.as_error());
        }
        if check == AuthorityCheck::Dispatch && header != (expected, 0) {
            self.stop = Some(Stop::Invalid);
            return Err(Stop::Invalid.as_error());
        }
        ensure!(
            header == (expected, 0),
            "invalid Registry authorization frame"
        );
        if check != AuthorityCheck::Live {
            self.authority = WireAuthority::PublicationPermitted;
        } else if self.authority == WireAuthority::AwaitingLive {
            self.authority = WireAuthority::Live;
        }
        Ok(())
    }

    fn check_idle(&mut self, deadline: Instant) -> Result<()> {
        if let Some(stop) = self.stop {
            return Err(stop.as_error());
        }
        let outcome = (|| {
            if poll(&self.stream, libc::POLLIN, 0)? {
                let header = read_header(&mut self.stream, deadline)?;
                if header == (CANCEL, 0) {
                    return Ok(Some(Stop::Cancelled));
                }
                bail!("unsolicited Provider prepare frame");
            }
            Ok(None)
        })();
        self.stop = match outcome {
            Ok(stop) => stop,
            Err(_) => Some(Stop::Broken),
        };
        match self.stop {
            Some(stop) => Err(stop.as_error()),
            None => Ok(()),
        }
    }
}

fn failure_code(error: &anyhow::Error) -> &'static str {
    if error.is::<ModelRegistryRequiresLegacyRead>()
        || error.is::<ConversationRegistryRequiresLegacyRead>()
    {
        return "legacy-read-required";
    }
    match error.downcast_ref::<NativeArtifactError>() {
        Some(
            NativeArtifactError::ProviderRegistryLimitBeforePublish
            | NativeArtifactError::ModelRegistryLimitBeforePublish,
        ) => "legacy-read-required",
        Some(NativeArtifactError::ModelRegistryPublicationUnconfirmed(_)) => "unavailable",
        Some(NativeArtifactError::ProviderRejected(error)) => error.code(),
        Some(NativeArtifactError::ModelRegistryRejected(error)) => error.code(),
        Some(NativeArtifactError::ConversationRegistryRejected(error)) => error.code(),
        Some(NativeArtifactError::Execution(
            NativeDataIoError::Cancelled | NativeDataIoError::Deadline,
        )) => "cancelled",
        Some(NativeArtifactError::TooLarge) => "limit-exceeded",
        Some(NativeArtifactError::Denied) => "denied",
        _ => "unavailable",
    }
}

fn authenticate_peer(stream: &UnixStream) -> Result<LauncherProcessIdentity> {
    let descriptor = stream.as_raw_fd();
    let mut uid: libc::uid_t = 0;
    let mut gid: libc::gid_t = 0;
    // Both pointers name writable native IDs and the stream owns the descriptor.
    if unsafe { libc::getpeereid(descriptor, &mut uid, &mut gid) } != 0 {
        return Err(io::Error::last_os_error().into());
    }
    // geteuid takes no pointers and returns the Launcher's effective user ID.
    ensure!(
        uid == unsafe { libc::geteuid() },
        "Provider prepare peer has a different effective user"
    );
    let mut pid: libc::pid_t = 0;
    let expected = std::mem::size_of_val(&pid) as libc::socklen_t;
    let mut length = expected;
    // LOCAL_PEERPID writes the kernel-authenticated pid_t into the fixed buffer.
    if unsafe {
        libc::getsockopt(
            descriptor,
            libc::SOL_LOCAL,
            libc::LOCAL_PEERPID,
            (&mut pid as *mut libc::pid_t).cast(),
            &mut length,
        )
    } != 0
    {
        return Err(io::Error::last_os_error().into());
    }
    ensure!(
        length == expected && pid > 0,
        "Provider prepare peer process is unavailable"
    );
    process_identity(pid as u32)
}

fn suppress_sigpipe(stream: &UnixStream) -> io::Result<()> {
    let enabled: libc::c_int = 1;
    // The kernel copies the fixed socket option from this initialized integer.
    let result = unsafe {
        libc::setsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_NOSIGPIPE,
            (&enabled as *const libc::c_int).cast(),
            std::mem::size_of_val(&enabled) as libc::socklen_t,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn host_deadline(deadline_ns: u64) -> Result<Instant> {
    // Python monotonic_ns uses the same macOS uptime clock. Sample Instant first
    // to avoid accidentally extending the Host deadline by conversion latency.
    let anchor = Instant::now();
    let remaining = deadline_ns
        .checked_sub(uptime_ns()?)
        .filter(|remaining| *remaining != 0)
        .map(Duration::from_nanos)
        .filter(|duration| *duration <= MAX_HORIZON)
        .context("Provider prepare deadline is outside its envelope")?;
    Ok(anchor + remaining)
}

fn uptime_ns() -> Result<u64> {
    let mut now = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // The syscall writes a single timespec into this initialized local.
    if unsafe { libc::clock_gettime(libc::CLOCK_UPTIME_RAW, &mut now) } != 0 {
        return Err(io::Error::last_os_error().into());
    }
    Ok(u64::try_from(
        Duration::new(now.tv_sec.try_into()?, now.tv_nsec.try_into()?).as_nanos(),
    )?)
}

fn read_header(stream: &mut UnixStream, deadline: Instant) -> Result<(u8, usize)> {
    let mut header = [0u8; 5];
    read_exact(stream, &mut header, deadline)?;
    Ok((
        header[0],
        u32::from_be_bytes([header[1], header[2], header[3], header[4]]) as usize,
    ))
}

fn receive_orchestration_input(
    stream: &mut UnixStream,
    input_bytes: usize,
    deadline: Instant,
) -> Result<Option<Vec<u8>>> {
    send_frame(stream, BODY_READY, &[], deadline)?;
    let header = read_header(stream, deadline)?;
    if header == (CANCEL, 0) {
        return Ok(None);
    }
    ensure!(
        header == (ORCHESTRATION_INPUT, input_bytes),
        "invalid Saved Orchestration input frame"
    );
    read_input_bytes(stream, input_bytes, deadline).map(Some)
}

fn receive_saved_local_chat_input(
    stream: &mut UnixStream,
    input_bytes: usize,
    deadline: Instant,
) -> Result<Option<Vec<u8>>> {
    send_frame(stream, BODY_READY, &[], deadline)?;
    let read = (|| {
        let header = read_header(stream, deadline)?;
        let failure = match header {
            (CANCEL, 0) => "cancelled",
            (SAVED_LOCAL_CHAT_INPUT, size) if size > LEGACY_LOCAL_CHAT_REQUEST_LIMIT => {
                "limit-exceeded"
            }
            (SAVED_LOCAL_CHAT_INPUT, size) if size > 0 && size == input_bytes => {
                return read_input_bytes(stream, size, deadline).map(Ok);
            }
            _ => "invalid",
        };
        Ok(Err(failure))
    })();
    let code = match read {
        Ok(Ok(input)) => return Ok(Some(input)),
        Ok(Err(code)) => code,
        Err(error)
            if error
                .downcast_ref::<io::Error>()
                .is_some_and(|error| error.kind() == io::ErrorKind::TimedOut) =>
        {
            "cancelled"
        }
        Err(error) => return Err(error),
    };
    send_frame(
        stream,
        FAILURE,
        code.as_bytes(),
        Instant::now() + FRAME_TIMEOUT,
    )?;
    Ok(None)
}

fn read_input_bytes(
    stream: &mut UnixStream,
    input_bytes: usize,
    deadline: Instant,
) -> Result<Vec<u8>> {
    let mut input = vec![0; input_bytes];
    read_exact(stream, &mut input, deadline)?;
    Ok(input)
}

fn read_exact(stream: &mut UnixStream, mut bytes: &mut [u8], deadline: Instant) -> Result<()> {
    while !bytes.is_empty() {
        wait_ready(stream, libc::POLLIN, deadline)?;
        match stream.read(bytes) {
            Ok(0) => return Err(io::Error::from(io::ErrorKind::UnexpectedEof).into()),
            Ok(read) => bytes = &mut bytes[read..],
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
                ) => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn send_frame(stream: &mut UnixStream, tag: u8, bytes: &[u8], deadline: Instant) -> Result<()> {
    let size = u32::try_from(bytes.len())?;
    let [a, b, c, d] = size.to_be_bytes();
    for mut part in [&[tag, a, b, c, d][..], bytes] {
        while !part.is_empty() {
            wait_ready(stream, libc::POLLOUT, deadline)?;
            match stream.write(part) {
                Ok(0) => return Err(io::Error::from(io::ErrorKind::WriteZero).into()),
                Ok(sent) => part = &part[sent..],
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
                    ) => {}
                Err(error) => return Err(error.into()),
            }
        }
    }
    Ok(())
}

fn wait_ready(stream: &UnixStream, events: libc::c_short, deadline: Instant) -> Result<()> {
    loop {
        let duration = deadline.saturating_duration_since(Instant::now());
        if duration.is_zero() {
            return Err(io::Error::from(io::ErrorKind::TimedOut).into());
        }
        let milliseconds = duration.as_micros().div_ceil(1_000).max(1) as libc::c_int;
        match poll(stream, events, milliseconds) {
            Ok(true) if Instant::now() < deadline => return Ok(()),
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => return Err(error.into()),
        }
    }
}

fn poll(stream: &UnixStream, events: libc::c_short, milliseconds: libc::c_int) -> io::Result<bool> {
    let mut descriptor = libc::pollfd {
        fd: stream.as_raw_fd(),
        events,
        revents: 0,
    };
    // The descriptor array comprises exactly one live borrow of this stream.
    match unsafe { libc::poll(&mut descriptor, 1, milliseconds) } {
        -1 => Err(io::Error::last_os_error()),
        ready => Ok(ready != 0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PROVIDER_REQUEST: &str =
        r#", "request":{"connection-name":"local","endpoint":"http://localhost"}"#;

    fn start_frame(operation: &str) -> String {
        format!(
            r#"{{"identity":{{"profile_id":"profile","profile_revision":"revision","activation_id":"activation","plan_digest":"digest"}},"runtime_instance":"0123456789abcdef0123456789abcdef","security_epoch":1,"deadline_ns":1{operation}}}"#
        )
    }

    #[test]
    fn saved_input_wire_separates_read_and_publication() {
        for (name, mode) in [
            ("restore", SavedInputMode::Restore),
            ("bind", SavedInputMode::Bind),
        ] {
            let start = start_frame(&format!(
                r#","saved_input":{{"mode":"{name}","input_bytes":{SAVED_INPUT_FRAME_LIMIT}}}"#
            ));
            let descriptor = serde_json::from_str::<Start<'_>>(&start).unwrap();
            assert!(descriptor.operation().is_err());
            let request = descriptor.request_start().unwrap();
            assert!(matches!(request, RequestStart::SavedInput(saved)
                if saved.caller == SavedInputCaller::SavedTurn
                    && saved.mode == mode && saved.input_bytes == SAVED_INPUT_FRAME_LIMIT));
            let role = ReplyRole::SavedInput(mode);
            assert!(role.uses_funded_input());
            assert_eq!(role.permits_publication(), mode == SavedInputMode::Bind);
            assert_eq!(
                role.validate_result(WireAuthority::AwaitingLive, br#"{"accepted":{}}"#),
                Err("unavailable")
            );
            assert_eq!(
                role.validate_result(WireAuthority::Live, br#"{"accepted":{}}"#),
                Ok(())
            );
            assert!(role
                .validate_result(WireAuthority::Live, br#"{"accepted":{},"extra":true}"#)
                .is_err());
            let compatibility = LegacySavedTurnSettlementError::RequiresLegacyCodec.into();
            assert_eq!(
                role.failure_code(WireAuthority::Live, &compatibility),
                "legacy-read-required"
            );
            assert_eq!(
                role.failure_code(WireAuthority::AwaitingLive, &compatibility),
                "unavailable"
            );
            assert_eq!(
                role.failure_code(WireAuthority::PublicationPermitted, &compatibility),
                "unavailable"
            );
        }
        for (name, expected) in [
            ("calendar", SavedInputCaller::Calendar),
            ("calendar-manual", SavedInputCaller::CalendarManual),
        ] {
            let calendar = start_frame(&format!(
                r#","saved_input":{{"caller":"{name}","mode":"bind","input_bytes":1}}"#
            ));
            let calendar = serde_json::from_str::<Start<'_>>(&calendar).unwrap();
            assert!(matches!(
                calendar.request_start().unwrap(),
                RequestStart::SavedInput(SavedInputStart {caller, mode: SavedInputMode::Bind, input_bytes: 1})
                    if caller == expected
            ));
        }
        for invalid in [
            r#","saved_input":null"#.to_owned(),
            r#","saved_input":{"mode":"bind","input_bytes":0}"#.to_owned(),
            format!(
                r#","saved_input":{{"mode":"bind","input_bytes":{}}}"#,
                SAVED_INPUT_FRAME_LIMIT + 1
            ),
            r#","saved_input":{"mode":"read","input_bytes":1}"#.to_owned(),
            r#","saved_input":{"caller":"side-chat","mode":"bind","input_bytes":1}"#.to_owned(),
            r#","saved_input":{"caller":"calendar-status","mode":"bind","input_bytes":1}"#
                .to_owned(),
            r#","saved_input":{"caller":null,"mode":"bind","input_bytes":1}"#.to_owned(),
            r#","saved_input":{"mode":"restore","input_bytes":1,"publication":true}"#.to_owned(),
            format!(r#","saved_input":{{"mode":"restore","input_bytes":1}}{PROVIDER_REQUEST}"#),
        ] {
            let candidate = start_frame(&invalid);
            assert!(serde_json::from_str::<Start<'_>>(&candidate)
                .ok()
                .and_then(|start| start.request_start().ok())
                .is_none());
        }
        let restore = ReplyRole::SavedInput(SavedInputMode::Restore);
        let bind = ReplyRole::SavedInput(SavedInputMode::Bind);
        assert_eq!(
            restore.validate_result(WireAuthority::Live, br#"{"accepted":null}"#),
            Ok(())
        );
        assert_eq!(
            restore.validate_result(WireAuthority::PublicationPermitted, br#"{"accepted":{}}"#),
            Err("unavailable")
        );
        assert_eq!(
            bind.validate_result(WireAuthority::Live, br#"{"accepted":null}"#),
            Err("internal")
        );
        assert_eq!(
            bind.validate_result(WireAuthority::PublicationPermitted, br#"{"accepted":{}}"#),
            Ok(())
        );
    }

    #[test]
    fn saved_local_chat_start_names_one_exact_operation_and_bounds_its_descriptor() {
        use serde_json::json;

        let mut descriptor = json!({
            "role": "generate",
            "connection_id": "local/model",
            "record_revision": u64::MAX,
            "endpoint": "http://[::1]:1234/v1/",
            "input_bytes": LEGACY_LOCAL_CHAT_REQUEST_LIMIT,
        });
        let operation =
            |descriptor: &serde_json::Value| format!(r#", "saved_local_chat":{descriptor}"#);
        for (role, expected) in [
            ("generate", OriginalSavedLocalChatRole::Generate),
            ("stream", OriginalSavedLocalChatRole::Stream),
            (
                "side-chat-generate",
                OriginalSavedLocalChatRole::SideChatGenerate,
            ),
            (
                "side-chat-stream",
                OriginalSavedLocalChatRole::SideChatStream,
            ),
        ] {
            descriptor["role"] = json!(role);
            let named = operation(&descriptor);
            let frame = start_frame(&named);
            let RequestStart::SavedLocalChat(chat) = parse_start(START, frame.as_bytes())
                .unwrap()
                .request_start()
                .unwrap()
            else {
                panic!("expected Saved Local Chat");
            };
            assert_eq!(chat.role, expected);
            assert_eq!(chat.connection_id, "local/model");
            assert_eq!(chat.record_revision, u64::MAX);
            assert_eq!(chat.endpoint, "http://[::1]:1234/v1/");
            assert_eq!(chat.input_bytes, LEGACY_LOCAL_CHAT_REQUEST_LIMIT);
            assert!(!chat.project_response);
            assert!(!chat.compile_request);
            assert!(!chat.compile_policy);
            assert_eq!(chat.validate(), Ok(()));
            assert!(parse_start(START_APPEND, frame.as_bytes()).is_err());
            for extra in [
                named.as_str(),
                PROVIDER_REQUEST,
                r#", "saved_orchestration":{"input_bytes":1}"#,
                r#", "apply":{}"#,
            ] {
                let mixed = start_frame(&format!("{named}{extra}"));
                assert!(parse_start(START, mixed.as_bytes())
                    .and_then(|parsed| parsed.request_start())
                    .is_err());
            }
        }
        let mut requested = descriptor.clone();
        requested["project_response"] = json!(true);
        requested["compile_request"] = json!(true);
        requested["compile_policy"] = json!(true);
        let frame = start_frame(&operation(&requested));
        let RequestStart::SavedLocalChat(chat) = parse_start(START, frame.as_bytes())
            .unwrap()
            .request_start()
            .unwrap()
        else {
            panic!("expected Saved Local Chat");
        };
        assert!(chat.project_response);
        assert!(chat.compile_request);
        assert!(chat.compile_policy);
        assert_eq!(chat.validate(), Ok(()));
        let mut missing_request = chat.clone();
        missing_request.compile_request = false;
        assert_eq!(missing_request.validate(), Err("invalid"));
        let mut missing_projection = chat;
        missing_projection.project_response = false;
        assert_eq!(missing_projection.validate(), Err("invalid"));
        for (key, value) in [
            ("role", json!("shell")),
            ("role", json!("side-chat")),
            ("role", json!("side-chat-turn")),
            ("role", json!("side-chat-reconcile")),
            ("record_revision", json!(true)),
            ("record_revision", json!(-1)),
            ("input_bytes", json!(1.5)),
            ("project_response", json!("true")),
            ("compile_request", json!(1)),
            ("compile_policy", json!(1)),
            ("profile_id", json!("override")),
            ("input", json!({})),
        ] {
            let mut tampered = descriptor.clone();
            tampered[key] = value;
            let frame = start_frame(&operation(&tampered));
            assert!(parse_start(START, frame.as_bytes()).is_err(), "{key}");
        }
        for (key, value, error) in [
            ("record_revision", json!(0), "invalid"),
            ("connection_id", json!(""), "invalid"),
            ("endpoint", json!(""), "invalid"),
            ("input_bytes", json!(0), "invalid"),
            (
                "input_bytes",
                json!(LEGACY_LOCAL_CHAT_REQUEST_LIMIT + 1),
                "limit-exceeded",
            ),
        ] {
            let mut tampered = descriptor.clone();
            tampered[key] = value;
            let frame = start_frame(&operation(&tampered));
            let RequestStart::SavedLocalChat(chat) = parse_start(START, frame.as_bytes())
                .unwrap()
                .request_start()
                .unwrap()
            else {
                panic!("expected Saved Local Chat");
            };
            assert_eq!(chat.validate(), Err(error), "{key}");
        }
    }

    #[test]
    fn saved_local_chat_preserves_exact_bounded_input_and_uses_its_own_single_dispatch() {
        let (mut client, mut server) = UnixStream::pair().unwrap();
        client.set_nonblocking(true).unwrap();
        server.set_nonblocking(true).unwrap();
        let mut input = vec![b' '; LEGACY_LOCAL_CHAT_REQUEST_LIMIT];
        input[..5].copy_from_slice(&[b'{', b'\n', 0, 255, b'}']);
        let deadline = Instant::now() + FRAME_TIMEOUT;
        let worker = thread::spawn(move || {
            let input = receive_saved_local_chat_input(
                &mut server,
                LEGACY_LOCAL_CHAT_REQUEST_LIMIT,
                deadline,
            )
            .unwrap()
            .unwrap();
            let mut wire = Wire::new(server);
            let dispatch = ReplyRole::SavedLocalChat.effect_check().unwrap();
            assert!(wire.exchange_authority(dispatch, deadline).is_err());
            wire.exchange_authority(AuthorityCheck::Live, deadline)
                .unwrap();
            wire.exchange_authority(dispatch, deadline).unwrap();
            wire.exchange_authority(AuthorityCheck::Live, deadline)
                .unwrap();
            assert!(wire.exchange_authority(dispatch, deadline).is_err());
            assert!(wire.authority == WireAuthority::PublicationPermitted);
            input
        });
        assert_eq!(read_header(&mut client, deadline).unwrap(), (BODY_READY, 0));
        send_frame(&mut client, SAVED_LOCAL_CHAT_INPUT, &input, deadline).unwrap();
        for (challenge, response) in [
            (CHECK, LIVE),
            (DISPATCH_CHECK, DISPATCH_PERMIT),
            (CHECK, LIVE),
        ] {
            assert_eq!(read_header(&mut client, deadline).unwrap(), (challenge, 0));
            send_frame(&mut client, response, &[], deadline).unwrap();
        }
        assert_eq!(worker.join().unwrap(), input);
    }

    #[test]
    fn saved_local_chat_rejects_wrong_body_and_dispatch_frames() {
        for (tag, size, code) in [
            (ORCHESTRATION_INPUT, 5, "invalid"),
            (SAVED_LOCAL_CHAT_INPUT, 4, "invalid"),
            (SAVED_LOCAL_CHAT_INPUT, u32::MAX, "limit-exceeded"),
            (CANCEL, 0, "cancelled"),
        ] {
            let (mut client, mut server) = UnixStream::pair().unwrap();
            let [a, b, c, d] = size.to_be_bytes();
            client.write_all(&[tag, a, b, c, d]).unwrap();
            let deadline = Instant::now() + FRAME_TIMEOUT;
            assert!(receive_saved_local_chat_input(&mut server, 5, deadline)
                .unwrap()
                .is_none());
            assert_eq!(read_header(&mut client, deadline).unwrap(), (BODY_READY, 0));
            assert_eq!(
                read_header(&mut client, deadline).unwrap(),
                (FAILURE, code.len())
            );
            assert_eq!(
                read_input_bytes(&mut client, code.len(), deadline).unwrap(),
                code.as_bytes()
            );
        }
        for (response, payload) in [
            (CANCEL, b"".as_slice()),
            (PUBLISH_PERMIT, b""),
            (DISPATCH_PERMIT, b"x"),
        ] {
            let (mut client, server) = UnixStream::pair().unwrap();
            let deadline = Instant::now() + FRAME_TIMEOUT;
            let peer = thread::spawn(move || {
                assert_eq!(
                    read_header(&mut client, deadline).unwrap(),
                    (DISPATCH_CHECK, 0)
                );
                send_frame(&mut client, response, payload, deadline).unwrap();
                client
            });
            let mut wire = Wire::new(server);
            wire.authority = WireAuthority::Live;
            assert!(wire
                .exchange_authority(AuthorityCheck::Dispatch, deadline)
                .is_err());
            assert!(wire.authority == WireAuthority::Live);
            assert_eq!(
                wire.stop.and_then(Stop::reply),
                Some(if response == CANCEL {
                    "cancelled"
                } else {
                    "invalid"
                })
            );
            assert!(wire
                .exchange_authority(AuthorityCheck::Dispatch, deadline)
                .is_err());
            let _client = peer.join().unwrap();
        }
    }

    #[test]
    fn saved_local_chat_result_requires_dispatch_and_keeps_raw_or_empty_responses() {
        use LegacyLocalChatHttpError as Transport;
        use LegacyLocalChatSourceError as Source;

        let role = ReplyRole::SavedLocalChat;
        assert!(!role.permits_publication());
        let dispatched = WireAuthority::PublicationPermitted;
        assert_eq!(role.validate_result(dispatched, b""), Ok(()));
        assert_eq!(role.validate_result(dispatched, &[255]), Ok(()));
        let mut result = vec![0; LEGACY_LOCAL_CHAT_RESPONSE_LIMIT];
        assert_eq!(role.validate_result(dispatched, &result), Ok(()));
        result.push(0);
        assert_eq!(
            role.validate_result(dispatched, &result),
            Err("invalid-response")
        );
        for unpermitted in [WireAuthority::AwaitingLive, WireAuthority::Live] {
            assert_eq!(role.validate_result(unpermitted, b"{}"), Err("unavailable"));
        }
        let declined = anyhow::Error::new(SavedLocalChatRequiresLegacyRequest);
        assert_eq!(
            role.failure_code(WireAuthority::Live, &declined),
            "legacy-request"
        );
        assert_eq!(role.failure_code(dispatched, &declined), "unavailable");
        for (error, code) in [
            (OriginalLocalChatPolicyError::Denied, "policy-model-denied"),
            (OriginalLocalChatPolicyError::Model, "policy-model-invalid"),
            (
                OriginalLocalChatPolicyError::Parameters,
                "policy-parameters-invalid",
            ),
        ] {
            let error = anyhow::Error::new(error);
            assert_eq!(role.failure_code(WireAuthority::Live, &error), code);
            assert_eq!(role.failure_code(dispatched, &error), "unavailable");
        }
        for (error, code) in [
            (
                OriginalLocalChatError::from(Source::SelectionChanged).into(),
                "denied",
            ),
            (
                OriginalLocalChatError::from(Transport::InvalidRequest).into(),
                "invalid",
            ),
            (
                OriginalLocalChatError::from(Transport::Revoked).into(),
                "cancelled",
            ),
            (
                OriginalLocalChatError::from(Transport::ResponseTooLarge).into(),
                "invalid-response",
            ),
            (
                OriginalLocalChatError::RegistryUnavailable.into(),
                "unavailable",
            ),
            (ConversationWriteRequiresLegacyRead.into(), "unavailable"),
        ] {
            assert_eq!(role.failure_code(WireAuthority::Live, &error), code);
            assert_eq!(role.failure_code(dispatched, &error), code);
        }
    }

    fn confirmation_terminal() -> serde_json::Value {
        let oracle: serde_json::Value = serde_json::from_str(include_str!(
            "../../../packs/local.saved-conversation-rust/fixtures/legacy-confirmation-oracle.json"
        ))
        .unwrap();
        let mut terminal = oracle["turn"].clone();
        terminal["profile_id"] = "profile".into();
        terminal
    }

    fn confirmation_operation(caller: &str, terminal: &str) -> String {
        format!(r#", "conversation_confirmation":{{"caller":"{caller}","terminal":{terminal}}}"#)
    }

    fn append_oracle() -> serde_json::Value {
        serde_json::from_str(include_str!(
            "../../../packs/local.saved-conversation-rust/fixtures/legacy-append-oracle.json"
        ))
        .unwrap()
    }

    fn append_operation(request: &str) -> String {
        format!(r#", "conversation_append":{{"request":{request}}}"#)
    }

    #[test]
    fn append_uses_its_own_start_tag_and_size_limit() {
        let oracle = append_oracle();
        for role in ["user", "assistant"] {
            let request = serde_json::to_string_pretty(&oracle[role]["request"]).unwrap();
            let operation = append_operation(&request);
            let message = start_frame(&operation);
            assert!(parse_start(START, message.as_bytes()).is_err());
            let start = parse_start(START_APPEND, message.as_bytes()).unwrap();
            let NativeRegistryRequest::ConversationAppend {
                caller,
                request: raw,
                expected_revision,
            } = start.operation().unwrap()
            else {
                panic!("expected Conversation append");
            };
            assert_eq!(caller, ConversationAppendCaller::SavedTurn);
            assert_eq!(raw, request.as_bytes());
            assert_eq!(
                expected_revision,
                oracle[role]["request"]["expected_conversation_revision"]
                    .as_u64()
                    .unwrap(),
            );
            let mixed = start_frame(&format!("{operation}{PROVIDER_REQUEST}"));
            assert!(parse_start(START_APPEND, mixed.as_bytes())
                .unwrap()
                .operation()
                .is_err());
        }
        assert!(parse_start(START_APPEND, start_frame(PROVIDER_REQUEST).as_bytes()).is_err());
        for (tag, limit) in [
            (START, JSON_LIMIT),
            (START_APPEND, CONVERSATION_APPEND_LIMIT),
        ] {
            assert!(validate_start_size(tag, 0).is_err());
            assert!(validate_start_size(tag, limit).is_ok());
            assert!(validate_start_size(tag, limit + 1).is_err());
        }
        assert!(validate_start_size(PLAN, 2).is_err());
    }

    #[test]
    fn append_caller_is_exact_and_preserves_the_legacy_request() {
        let request = serde_json::to_string_pretty(&append_oracle()["user"]["request"]).unwrap();
        let message = |caller_field: &str| {
            start_frame(&format!(
                r#", "conversation_append":{{{caller_field},"request":{request}}}"#
            ))
        };
        for (field, expected) in [
            (
                r#""caller":"saved-turn""#,
                ConversationAppendCaller::SavedTurn,
            ),
            (
                r#""caller":"side-chat""#,
                ConversationAppendCaller::SideChat,
            ),
        ] {
            let wire = message(field);
            let start = parse_start(START_APPEND, wire.as_bytes()).unwrap();
            let NativeRegistryRequest::ConversationAppend {
                caller,
                request: raw,
                ..
            } = start.operation().unwrap()
            else {
                panic!("expected Conversation append");
            };
            assert_eq!(caller, expected);
            assert_eq!(raw, request.as_bytes());
        }
        for field in [
            r#""caller":"side_chat""#,
            r#""caller":"saved-reconcile""#,
            r#""caller":null"#,
            r#""caller":"saved-turn","caller":"side-chat""#,
            r#""Caller":"side-chat""#,
        ] {
            assert!(parse_start(START_APPEND, message(field).as_bytes()).is_err());
        }
    }

    #[test]
    fn append_rejects_ambiguous_fields_and_unbound_message_identity() {
        let oracle = append_oracle();
        let request = &oracle["user"]["request"];
        let rejected = |request: &str| {
            let message = start_frame(&append_operation(request));
            assert!(parse_start(START_APPEND, message.as_bytes())
                .and_then(|start| start.operation())
                .is_err());
        };
        rejected(&format!(
            r#"{{"profile_id":"defaults",{}"#,
            &request.to_string()[1..]
        ));
        rejected(&format!(
            r#"{{"conversation_id":"override",{}"#,
            &request.to_string()[1..]
        ));
        for (field, value) in [
            (
                "/message/id",
                serde_json::json!(oracle["assistant"]["request"]["message"]["id"]),
            ),
            ("/message/role", serde_json::json!("system")),
            ("/expected_conversation_revision", serde_json::json!(0)),
            ("/expected_conversation_revision", serde_json::json!(true)),
            (
                "/saved_input/request/conversation_id",
                serde_json::json!("other"),
            ),
            (
                "/saved_input/request/turn_id",
                serde_json::json!("invalid/id"),
            ),
        ] {
            let mut invalid = request.clone();
            *invalid.pointer_mut(field).unwrap() = value;
            rejected(&invalid.to_string());
        }
    }

    #[test]
    fn confirmation_start_preserves_the_exact_terminal_json_for_each_caller() {
        let terminal = serde_json::to_string_pretty(&confirmation_terminal()).unwrap();
        for (wire_role, expected) in [
            ("saved-turn", OriginalSavedReceiptRole::SavedTurn),
            ("saved-reconcile", OriginalSavedReceiptRole::SavedReconcile),
            ("side-chat-turn", OriginalSavedReceiptRole::SideChatTurn),
            (
                "side-chat-reconcile",
                OriginalSavedReceiptRole::SideChatReconcile,
            ),
        ] {
            let operation = confirmation_operation(wire_role, &terminal);
            let message = start_frame(&operation);
            let start = parse_start(START, message.as_bytes()).unwrap();
            assert!(parse_start(START_APPEND, message.as_bytes()).is_err());
            let NativeRegistryRequest::ConversationConfirmation {
                caller,
                conversation_id,
                terminal: raw,
            } = start.operation().unwrap()
            else {
                panic!("expected Conversation confirmation");
            };
            assert_eq!(caller, expected);
            assert_eq!(conversation_id, "conversation-1");
            assert_eq!(raw, terminal.as_bytes());
            let mixed = start_frame(&format!("{operation}{PROVIDER_REQUEST}"));
            assert!(serde_json::from_str::<Start<'_>>(&mixed)
                .unwrap()
                .operation()
                .is_err());
            let duplicate = start_frame(&format!("{operation}{operation}"));
            assert!(serde_json::from_str::<Start<'_>>(&duplicate).is_err());
        }
    }

    #[test]
    fn confirmation_start_rejects_unbound_or_malformed_terminal_evidence() {
        use serde_json::json;

        let terminal = confirmation_terminal();
        let check = |raw: &str| {
            let message = start_frame(&confirmation_operation("saved-turn", raw));
            let start: Start<'_> = serde_json::from_str(&message).unwrap();
            assert!(start.operation().is_err(), "{raw}");
        };
        for (field, invalid) in [
            ("profile_id", json!("other")),
            ("id", json!("../turn")),
            ("conversation_id", json!("different-conversation")),
            ("request_id", json!("saved-turn.unknown")),
            ("status", json!("running")),
            ("revision", json!(2)),
            ("conversation_revision", json!(true)),
            ("input_digest", json!("sha256:short")),
            ("result_reference", json!({})),
            ("unexpected", json!(0)),
        ] {
            let mut tampered = terminal.clone();
            tampered[field] = invalid;
            check(&tampered.to_string());
        }
        for (field, invalid) in [
            ("conversation_revision", json!(0)),
            ("conversation_revision", json!(42)),
            ("user_message_id", json!("bad")),
            ("outcome_digest", json!("bad")),
            ("unexpected", json!(null)),
        ] {
            let mut tampered = terminal.clone();
            tampered["result_reference"][field] = invalid;
            check(&tampered.to_string());
        }
        let mut missing = terminal.clone();
        missing.as_object_mut().unwrap().remove("result_reference");
        check(&missing.to_string());
        let raw = terminal.to_string();
        check(&format!(r#"{{"id":"duplicate",{}"#, &raw[1..]));
        check(&format!(
            "{{{}{}",
            " ".repeat(CONVERSATION_CONFIRMATION_LIMIT),
            &raw[1..]
        ));
        for operation in [
            confirmation_operation("shell", &raw),
            confirmation_operation("side-chat", &raw),
            confirmation_operation("side-chat-reconcile ", &raw),
            confirmation_operation("saved-turn", "null"),
            r#", "conversation_confirmation":null"#.to_owned(),
        ] {
            assert!(serde_json::from_str::<Start<'_>>(&start_frame(&operation)).is_err());
        }
    }

    #[test]
    fn conversation_write_fallback_requires_live_and_cannot_follow_publication() {
        for (role, limit) in [
            (
                ReplyRole::ConversationConfirmation,
                CONVERSATION_CONFIRMATION_LIMIT,
            ),
            (ReplyRole::ConversationAppend, CONVERSATION_APPEND_LIMIT),
        ] {
            assert!(role.permits_publication());
            for (authority, code) in [
                (WireAuthority::AwaitingLive, "unavailable"),
                (WireAuthority::Live, "requires-legacy-read"),
                (
                    WireAuthority::PublicationPermitted,
                    "publication-unconfirmed",
                ),
            ] {
                for error in [
                    ConversationWriteRequiresLegacyRead.into(),
                    NativeArtifactError::ConversationLimitBeforePublish.into(),
                ] {
                    assert_eq!(role.failure_code(authority, &error), code);
                }
                assert_eq!(role.result_limit(authority), (limit, code));
            }
            let denied = NativeArtifactError::Denied.into();
            assert_eq!(role.failure_code(WireAuthority::Live, &denied), "denied");
            assert_eq!(
                role.failure_code(WireAuthority::PublicationPermitted, &denied),
                "publication-unconfirmed"
            );
        }
    }

    #[test]
    fn start_distinguishes_provider_prepare_apply_and_registry_reads() {
        let prepare = start_frame(PROVIDER_REQUEST);
        let prepare: Start<'_> = serde_json::from_str(&prepare).unwrap();
        assert!(prepare.apply.is_none());
        assert_eq!(prepare.request.as_ref().unwrap().connection_name, "local");
        assert!(matches!(
            prepare.operation().unwrap(),
            NativeRegistryRequest::Provider { apply: None, .. }
        ));

        // Leave semantic checks, including duplicate object keys, to the Core.
        let plan = r#"{"record": {"name":"ä", "name":"same"}}"#;
        let apply = start_frame(&format!(r#"{PROVIDER_REQUEST}, "apply": {plan}"#));
        let apply: Start<'_> = serde_json::from_str(&apply).unwrap();
        assert_eq!(apply.apply.unwrap().get(), plan);
        let list = start_frame(r#", "model_list": {}"#);
        let list: Start<'_> = serde_json::from_str(&list).unwrap();
        assert!(matches!(
            list.operation().unwrap(),
            NativeRegistryRequest::ModelList
        ));
    }

    #[test]
    fn provider_snapshot_start_decodes_the_closed_role_set() {
        use tobkiri_harness_native::OriginalReviewerHealthRole;
        use OriginalProviderSnapshotRole::{
            Adapter, CommandDatasource, ConversationCompleteHealth, Gateway, GatewayHealth,
            ModelAccessCatalog, ModelAccessRead, ModelSave, ModelSearch, ReviewerHealth,
            ShellSettings,
        };

        let assert_role = |name: &str, expected| {
            let frame = start_frame(&format!(r#", "provider_snapshot":{{"role":"{name}"}}"#));
            let start = parse_start(START, frame.as_bytes()).unwrap();
            assert!(parse_start(START_APPEND, frame.as_bytes()).is_err());
            assert!(matches!(
                start.operation().unwrap(),
                NativeRegistryRequest::ProviderSnapshot { role } if role == expected
            ));
        };
        for (name, expected) in [
            ("generate", OriginalSavedLocalChatRole::Generate),
            ("stream", OriginalSavedLocalChatRole::Stream),
            (
                "side-chat-generate",
                OriginalSavedLocalChatRole::SideChatGenerate,
            ),
            (
                "side-chat-stream",
                OriginalSavedLocalChatRole::SideChatStream,
            ),
        ] {
            assert_role(name, Adapter(expected));
        }
        for (name, role) in [
            ("generate", OriginalModelResolveRole::Generate),
            ("stream", OriginalModelResolveRole::Stream),
            ("quote-generate", OriginalModelResolveRole::QuoteGenerate),
            ("quote-stream", OriginalModelResolveRole::QuoteStream),
        ] {
            for (prefix, side_chat) in [("", false), ("side-chat-", true)] {
                assert_role(
                    &format!("{prefix}gateway-{name}"),
                    Gateway { role, side_chat },
                );
                assert_role(
                    &format!("{prefix}gateway-health-{name}"),
                    GatewayHealth { role, side_chat },
                );
            }
        }
        assert_role("shell-settings", ShellSettings);
        assert_role("model-save", ModelSave);
        assert_role("model-search", ModelSearch);
        assert_role("command-datasource", CommandDatasource);
        assert_role("model-access-read", ModelAccessRead);
        assert_role("model-access-catalog", ModelAccessCatalog);
        assert_role("conversation-complete-health", ConversationCompleteHealth);
        assert_role(
            "reviewer-health-quote",
            ReviewerHealth(OriginalReviewerHealthRole::Quote),
        );
        assert_role(
            "reviewer-health-generate",
            ReviewerHealth(OriginalReviewerHealthRole::Generate),
        );
    }

    #[test]
    fn model_access_provider_snapshot_accepts_only_its_named_role() {
        for role in ["model-access-read", "model-access-catalog"] {
            let positional = start_frame(&format!(r#", "provider_snapshot":["{role}"]"#));
            assert!(parse_start(START, positional.as_bytes()).is_err());
            for extra in [
                r#", "entry":"run.advance""#,
                r#", "entry":null"#,
                r#", "input_bytes":32"#,
                r#", "body":{}"#,
                r#", "profile_id":"elsewhere""#,
                r#", "connection_id":"provider""#,
                r#", "settings_projection":true"#,
                r#", "role":"model-access-read""#,
            ] {
                let frame = start_frame(&format!(
                    r#", "provider_snapshot":{{"role":"{role}"{extra}}}"#
                ));
                assert!(
                    parse_start(START, frame.as_bytes()).is_err(),
                    "{role}{extra}"
                );
            }
            let multiple = start_frame(&format!(
                r#", "provider_snapshot":{{"role":"{role}"}}, "saved_orchestration":{{"input_bytes":32}}"#
            ));
            let start = parse_start(START, multiple.as_bytes()).unwrap();
            assert!(start.operation().is_err());
        }
    }

    #[test]
    fn workflow_provider_snapshot_start_decodes_exact_callers_and_entries() {
        use tobkiri_harness_native::OriginalWorkflowHealthCaller;
        use OriginalWorkflowSnapshotCaller::{
            AudioSpeech, AudioTranscribe, Messages, MessagesAdapter, Text, TextAdapter,
        };
        use OriginalWorkflowSnapshotEntry::{
            Advance, Cancel, Selected, StepExecute, StepResume, StepRetry,
        };

        for (entry_name, entry) in [
            ("run.advance", Advance),
            ("run.cancel", Cancel),
            ("run.selected", Selected),
            ("run.step.execute", StepExecute),
            ("run.step.resume", StepResume),
            ("run.step.retry", StepRetry),
        ] {
            let workflow = |caller| OriginalProviderSnapshotRole::Workflow { caller, entry };
            let health = |caller| OriginalProviderSnapshotRole::WorkflowHealth { caller, entry };
            for (role_name, expected) in [
                ("workflow-text", workflow(Text)),
                ("workflow-messages", workflow(Messages)),
                ("workflow-text-adapter", workflow(TextAdapter)),
                ("workflow-messages-adapter", workflow(MessagesAdapter)),
                ("workflow-audio-transcribe", workflow(AudioTranscribe)),
                ("workflow-audio-speech", workflow(AudioSpeech)),
                (
                    "workflow-health-text",
                    health(OriginalWorkflowHealthCaller::Text),
                ),
                (
                    "workflow-health-messages",
                    health(OriginalWorkflowHealthCaller::Messages),
                ),
            ] {
                let fields = format!(
                    r#", "provider_snapshot":{{"role":"{role_name}","entry":"{entry_name}"}}"#
                );
                let frame = start_frame(&fields);
                let start = parse_start(START, frame.as_bytes()).unwrap();
                assert!(parse_start(START_APPEND, frame.as_bytes()).is_err());
                assert!(matches!(
                    start.operation().unwrap(),
                    NativeRegistryRequest::ProviderSnapshot { role } if role == expected
                ));
            }
        }
    }

    #[test]
    fn start_rejects_nonobject_plans_and_extra_envelope_fields() {
        for extra in [
            r#","plan":{}"#,
            r#","plan":{},"apply":{}"#,
            r#","apply":"{}""#,
            r#","apply":42"#,
            r#","apply":{},"apply":{}"#,
            r#","apply":{},"approved":true"#,
            r#","apply":null"#,
            r#","apply":[] "#,
        ] {
            let payload = start_frame(&format!("{PROVIDER_REQUEST}{extra}"));
            assert!(
                serde_json::from_str::<Start<'_>>(&payload).is_err(),
                "{extra}"
            );
        }
        for extra in [
            r#", "request":null, "model_list":{}"#,
            r#", "model_list":null"#,
            r#", "model_list":true"#,
            r#", "model_list":{"profile_id":"elsewhere"}"#,
            r#", "model_list":{}, "model_list":{}"#,
            r#", "provider_snapshot":null"#,
            r#", "provider_snapshot":["stream"]"#,
            r#", "provider_snapshot":{}"#,
            r#", "provider_snapshot":{"role":"quote-generate"}"#,
            r#", "provider_snapshot":{"role":"side-chat-quote-stream"}"#,
            r#", "provider_snapshot":{"role":"gateway-side-chat-stream"}"#,
            r#", "provider_snapshot":{"role":"side-chat-gateway-quote-health-stream"}"#,
            r#", "provider_snapshot":{"role":"gateway-health-quote-reconcile"}"#,
            r#", "provider_snapshot":{"role":"conversation-complete-health","entry":"run.advance"}"#,
            r#", "provider_snapshot":{"role":"conversation-complete-health","input_bytes":32}"#,
            r#", "provider_snapshot":{"role":"conversation-complete-health","profile_id":"elsewhere"}"#,
            r#", "provider_snapshot":{"role":"conversation-complete-health-stream"}"#,
            r#", "provider_snapshot":{"role":"reviewer-health-stream"}"#,
            r#", "provider_snapshot":{"role":"reviewer-health-quote","entry":"run.advance"}"#,
            r#", "provider_snapshot":{"role":"reviewer-health-quote","input_bytes":32}"#,
            r#", "provider_snapshot":{"role":"reviewer-health-generate","profile_id":"elsewhere"}"#,
            r#", "provider_snapshot":{"role":"reviewer-health-generate","settings_projection":true}"#,
            r#", "provider_snapshot":{"role":"gateway-health-quote-stream","delivery_mode":"incremental"}"#,
            r#", "provider_snapshot":{"role":"gateway-health-stream","entry":"run.advance"}"#,
            r#", "provider_snapshot":{"role":"gateway-health-stream","profile_id":"elsewhere"}"#,
            r#", "provider_snapshot":{"role":"side-chat-gateway-health-stream","entry":"run.advance"}"#,
            r#", "provider_snapshot":{"role":"side-chat-gateway-health-stream","profile_id":"elsewhere"}"#,
            r#", "provider_snapshot":{"role":"side-chat-gateway-health-stream","role":"gateway-health-stream"}"#,
            r#", "provider_snapshot":{"role":{"role":"generate","side_chat":true}}"#,
            r#", "provider_snapshot":{"role":"gateway-quote-stream","side_chat":true}"#,
            r#", "provider_snapshot":{"role":"stream","role":"generate"}"#,
            r#", "provider_snapshot":{"role":"stream","profile_id":"elsewhere"}"#,
            r#", "provider_snapshot":{"role":"stream","settings_projection":true}"#,
            r#", "provider_snapshot":{"role":"shell-settings","settings_projection":false}"#,
            r#", "provider_snapshot":{"role":"model-save","settings_projection":false}"#,
            r#", "provider_snapshot":{"role":"model-search","settings_projection":false}"#,
            r#", "provider_snapshot":{"role":"model-search","query":"example"}"#,
            r#", "provider_snapshot":{"role":"command-datasource","settings_projection":false}"#,
            r#", "provider_snapshot":{"role":"command-datasource","profile_id":"elsewhere"}"#,
            r#", "provider_snapshot":{"role":"command-data-source"}"#,
            r#", "provider_snapshot":{"role":"stream","entry":"run.advance"}"#,
            r#", "provider_snapshot":{"role":"gateway-generate","entry":null}"#,
            r#", "provider_snapshot":{"role":"model-search","entry":"run.step.resume"}"#,
            r#", "provider_snapshot":{"role":"workflow-text"}"#,
            r#", "provider_snapshot":{"role":"workflow-text","entry":null}"#,
            r#", "provider_snapshot":{"role":"workflow-text","entry":false}"#,
            r#", "provider_snapshot":{"role":"workflow-text","entry":"advance"}"#,
            r#", "provider_snapshot":{"role":"workflow-text","entry":"run.step.cancel"}"#,
            r#", "provider_snapshot":{"role":"workflow-text","entry":"run.advance","entry":"run.cancel"}"#,
            r#", "provider_snapshot":{"role":"workflow-text","entry":"run.advance","input_bytes":32}"#,
            r#", "provider_snapshot":{"role":"workflow-text","entry":"run.advance","settings_projection":true}"#,
            r#", "provider_snapshot":{"role":"workflow-text","entry":"run.advance","profile_id":"elsewhere"}"#,
            r#", "provider_snapshot":{"role":"workflow-text-adapter"}"#,
            r#", "provider_snapshot":{"role":"workflow-messages-adapter","entry":null}"#,
            r#", "provider_snapshot":{"role":"workflow-text-adapter","entry":"run.step.cancel"}"#,
            r#", "provider_snapshot":{"role":"workflow-messages-adapter","entry":"run.advance","profile_id":"elsewhere"}"#,
            r#", "provider_snapshot":{"role":"workflow-generate-adapter","entry":"run.advance"}"#,
            r#", "provider_snapshot":{"role":"workflow-stream","entry":"run.advance"}"#,
            r#", "provider_snapshot":{"role":"workflow-health-text"}"#,
            r#", "provider_snapshot":{"role":"workflow-health-messages","entry":null}"#,
            r#", "provider_snapshot":{"role":"workflow-health-text","entry":"run.resume"}"#,
            r#", "provider_snapshot":{"role":"workflow-health-text","entry":"run.advance","input_bytes":32}"#,
            r#", "provider_snapshot":{"role":"workflow-health-messages","entry":"run.advance","profile_id":"elsewhere"}"#,
            r#", "provider_snapshot":{"role":"workflow-health-text-adapter","entry":"run.advance"}"#,
            r#", "provider_snapshot":{"role":"workflow-health-audio-speech","entry":"run.advance"}"#,
            r#", "provider_snapshot":["workflow-text","run.advance"]"#,
            r#", "provider_snapshot":{"role":"stream"}, "provider_snapshot":{"role":"stream"}"#,
        ] {
            let payload = start_frame(extra);
            assert!(
                serde_json::from_str::<Start<'_>>(&payload).is_err(),
                "{extra}"
            );
        }
        for extra in [
            String::new(),
            format!(r#"{PROVIDER_REQUEST}, "model_list":{{}}"#),
            r#", "apply":{}, "model_list":{}"#.to_owned(),
            r#", "apply":{}, "provider_snapshot":{"role":"stream"}"#.to_owned(),
            r#", "model_list":{}, "provider_snapshot":{"role":"stream"}"#.to_owned(),
            format!(r#"{PROVIDER_REQUEST}, "provider_snapshot":{{"role":"stream"}}"#),
            r#", "model_list":{}, "provider_snapshot":{"role":"workflow-text","entry":"run.advance"}"#.to_owned(),
            r#", "saved_orchestration":{"input_bytes":32}, "provider_snapshot":{"role":"workflow-text","entry":"run.advance"}"#.to_owned(),
        ] {
            let payload = start_frame(&extra);
            let parsed: Start<'_> = serde_json::from_str(&payload).unwrap();
            assert!(parsed.operation().is_err(), "{extra}");
        }
    }

    #[test]
    fn frames_preserve_utf8_payload_bytes() {
        let (mut sender, mut receiver) = UnixStream::pair().unwrap();
        sender.set_nonblocking(true).unwrap();
        receiver.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        let utf8 = "{\"ä\":null}".as_bytes();
        send_frame(&mut sender, PLAN, utf8, deadline).unwrap();
        assert_eq!(
            read_header(&mut receiver, deadline).unwrap(),
            (PLAN, utf8.len())
        );
        let mut received = [0u8; 11];
        read_exact(&mut receiver, &mut received[..utf8.len()], deadline).unwrap();
        assert_eq!(received[..utf8.len()], *utf8);
    }

    #[test]
    fn ai_routing_requires_one_known_role_and_a_bounded_body() {
        for (role, expected) in [
            ("quote-generate", OriginalModelResolveRole::QuoteGenerate),
            ("quote-stream", OriginalModelResolveRole::QuoteStream),
            ("generate", OriginalModelResolveRole::Generate),
            ("stream", OriginalModelResolveRole::Stream),
        ] {
            let frame = start_frame(&format!(
                r#", "ai_route":{{"role":"{role}","input_bytes":{AI_GATEWAY_DATA_LIMIT}}}"#
            ));
            assert!(matches!(
                parse_start(START, frame.as_bytes()).unwrap().request_start(),
                Ok(RequestStart::AiGatewayData {
                    operation: AiGatewayDataOperation::Route(role),
                    input_bytes,
                }) if role == expected && input_bytes == AI_GATEWAY_DATA_LIMIT
            ));
        }
        for fields in [
            r#", "ai_route":null"#.to_owned(),
            r#", "ai_route":{"role":"stream","input_bytes":0}"#.to_owned(),
            r#", "ai_route":{"role":"shell","input_bytes":2}"#.to_owned(),
            r#", "ai_route":{"role":"generate","input_bytes":2},"model_list":{}"#.to_owned(),
            r#", "ai_route":{"role":"generate","input_bytes":2},"saved_orchestration":{"input_bytes":2}"#.to_owned(),
            format!(
                r#", "ai_route":{{"role":"stream","input_bytes":{}}}"#,
                AI_GATEWAY_DATA_LIMIT + 1
            ),
        ] {
            let frame = start_frame(&fields);
            assert!(parse_start(START, frame.as_bytes())
                .and_then(|start| start.request_start())
                .is_err());
        }
    }

    #[test]
    fn ai_stream_result_selects_an_independent_stream_only_operation() {
        let start = |role, flag| {
            start_frame(&format!(
                r#", "ai_route":{{"role":"{role}","input_bytes":12,"stream_result":{flag}}}"#
            ))
        };
        let frame = start("stream", "true");
        assert!(matches!(
            parse_start(START, frame.as_bytes())
                .unwrap()
                .request_start(),
            Ok(RequestStart::AiGatewayData {
                operation: AiGatewayDataOperation::StreamResult,
                input_bytes: 12,
            })
        ));
        for (role, flag) in [
            ("generate", "true"),
            ("quote-generate", "true"),
            ("quote-stream", "true"),
            ("stream", "1"),
            ("stream", "null"),
        ] {
            let frame = start(role, flag);
            assert!(parse_start(START, frame.as_bytes())
                .and_then(|start| start.request_start())
                .is_err());
        }
        let frame = start("stream", "false");
        assert!(matches!(
            parse_start(START, frame.as_bytes())
                .unwrap()
                .request_start(),
            Ok(RequestStart::AiGatewayData {
                operation: AiGatewayDataOperation::Route(OriginalModelResolveRole::Stream),
                input_bytes: 12,
            })
        ));
    }

    #[test]
    fn ai_usage_cost_requires_a_generation_role_and_excludes_stream_result() {
        let start = |role, flags| {
            start_frame(&format!(
                r#", "ai_route":{{"role":"{role}","input_bytes":12{flags}}}"#
            ))
        };
        for (role, expected) in [
            ("generate", OriginalModelResolveRole::Generate),
            ("stream", OriginalModelResolveRole::Stream),
        ] {
            let frame = start(role, r#","usage_cost":true"#);
            assert!(matches!(
                parse_start(START, frame.as_bytes()).unwrap().request_start(),
                Ok(RequestStart::AiGatewayData {
                    operation: AiGatewayDataOperation::UsageCost(role),
                    input_bytes: 12,
                }) if role == expected
            ));
        }
        for (role, flags) in [
            ("quote-generate", r#","usage_cost":true"#),
            ("quote-stream", r#","usage_cost":true"#),
            ("stream", r#","usage_cost":true,"stream_result":true"#),
            ("stream", r#","usage_cost":1"#),
            ("stream", r#","usage_cost":null"#),
        ] {
            let frame = start(role, flags);
            assert!(parse_start(START, frame.as_bytes())
                .and_then(|start| start.request_start())
                .is_err());
        }
    }

    #[test]
    fn ai_tool_intents_require_a_generation_role_and_exclude_other_operations() {
        let start = |role, flags| {
            start_frame(&format!(
                r#", "ai_route":{{"role":"{role}","input_bytes":12{flags}}}"#
            ))
        };
        for (role, expected) in [
            ("generate", OriginalModelResolveRole::Generate),
            ("stream", OriginalModelResolveRole::Stream),
        ] {
            let frame = start(role, r#","tool_intents":true"#);
            assert!(matches!(
                parse_start(START, frame.as_bytes()).unwrap().request_start(),
                Ok(RequestStart::AiGatewayData {
                    operation: AiGatewayDataOperation::ToolIntents(role),
                    input_bytes: 12,
                }) if role == expected
            ));
        }
        for (role, flags) in [
            ("quote-generate", r#","tool_intents":true"#),
            ("quote-stream", r#","tool_intents":true"#),
            ("stream", r#","tool_intents":true,"stream_result":true"#),
            ("stream", r#","tool_intents":true,"usage_cost":true"#),
            ("stream", r#","tool_intents":1"#),
            ("stream", r#","tool_intents":null"#),
        ] {
            let frame = start(role, flags);
            assert!(parse_start(START, frame.as_bytes())
                .and_then(|start| start.request_start())
                .is_err());
        }
        let frame = start("generate", r#","tool_intents":false,"usage_cost":true"#);
        assert!(matches!(
            parse_start(START, frame.as_bytes())
                .unwrap()
                .request_start(),
            Ok(RequestStart::AiGatewayData {
                operation: AiGatewayDataOperation::UsageCost(OriginalModelResolveRole::Generate),
                input_bytes: 12,
            })
        ));
    }

    #[test]
    fn ai_pipeline_start_preserves_each_signed_role_and_selects_one_operation() {
        let start = |role, flags| {
            start_frame(&format!(
                r#", "ai_route":{{"role":"{role}","input_bytes":12{flags}}}"#
            ))
        };
        for (role, expected) in [
            ("quote-generate", OriginalModelResolveRole::QuoteGenerate),
            ("quote-stream", OriginalModelResolveRole::QuoteStream),
            ("generate", OriginalModelResolveRole::Generate),
            ("stream", OriginalModelResolveRole::Stream),
        ] {
            let frame = start(role, r#","pipeline":"prepare""#);
            assert!(matches!(
                parse_start(START, frame.as_bytes()).unwrap().request_start(),
                Ok(RequestStart::AiGatewayData {
                    operation: AiGatewayDataOperation::RequestPrepare(role),
                    input_bytes: 12,
                }) if role == expected
            ));
        }
        let frame = start("generate", r#","pipeline":"failover""#);
        assert!(matches!(
            parse_start(START, frame.as_bytes())
                .unwrap()
                .request_start(),
            Ok(RequestStart::AiGatewayData {
                operation: AiGatewayDataOperation::Failover,
                input_bytes: 12,
            })
        ));
        for (role, flags) in [
            ("quote-generate", r#","pipeline":"failover""#),
            ("quote-stream", r#","pipeline":"failover""#),
            ("stream", r#","pipeline":"failover""#),
            ("stream", r#","pipeline":"prepare","stream_result":true"#),
            ("generate", r#","pipeline":"prepare","usage_cost":true"#),
            ("generate", r#","pipeline":"failover","tool_intents":true"#),
            ("generate", r#","pipeline":"off""#),
            ("generate", r#","pipeline":true"#),
            ("generate", r#","pipeline":null"#),
            ("generate", r#","pipeline":"prepare","pipeline":"failover""#),
        ] {
            let frame = start(role, flags);
            assert!(parse_start(START, frame.as_bytes())
                .and_then(|start| start.request_start())
                .is_err());
        }
    }

    #[test]
    fn ai_gateway_result_requires_an_original_generation_role_without_siblings() {
        let start = |role, flags| {
            start_frame(&format!(
                r#", "ai_route":{{"role":"{role}","input_bytes":12{flags}}}"#
            ))
        };
        for (role, expected) in [
            ("generate", OriginalModelResolveRole::Generate),
            ("stream", OriginalModelResolveRole::Stream),
        ] {
            let frame = start(role, r#","gateway_result":true"#);
            assert!(matches!(
                parse_start(START, frame.as_bytes()).unwrap().request_start(),
                Ok(RequestStart::AiGatewayData {
                    operation: AiGatewayDataOperation::GatewayResult(role),
                    input_bytes: 12,
                }) if role == expected
            ));
        }
        for (role, flags) in [
            ("quote-generate", r#","gateway_result":true"#),
            ("quote-stream", r#","gateway_result":true"#),
            ("stream", r#","gateway_result":true,"stream_result":true"#),
            ("generate", r#","gateway_result":true,"usage_cost":true"#),
            ("generate", r#","gateway_result":true,"tool_intents":true"#),
            ("generate", r#","gateway_result":true,"pipeline":"prepare""#),
            ("generate", r#","gateway_result":"true""#),
            ("generate", r#","gateway_result":null"#),
        ] {
            let frame = start(role, flags);
            assert!(parse_start(START, frame.as_bytes())
                .and_then(|start| start.request_start())
                .is_err());
        }
    }

    #[test]
    fn ai_gateway_policy_selects_each_original_role_and_excludes_other_operations() {
        let start = |role, flags| {
            start_frame(&format!(
                r#", "ai_route":{{"role":"{role}","input_bytes":12{flags}}}"#
            ))
        };
        for (role, expected) in [
            ("generate", OriginalModelResolveRole::Generate),
            ("stream", OriginalModelResolveRole::Stream),
            ("quote-generate", OriginalModelResolveRole::QuoteGenerate),
            ("quote-stream", OriginalModelResolveRole::QuoteStream),
        ] {
            let frame = start(role, r#","gateway_policy":true"#);
            assert!(matches!(
                parse_start(START, frame.as_bytes()).unwrap().request_start(),
                Ok(RequestStart::AiGatewayData {
                    operation: AiGatewayDataOperation::GatewayPolicy(role),
                    input_bytes: 12,
                }) if role == expected
            ));
        }
        for flags in [
            r#","gateway_policy":true,"gateway_result":true"#,
            r#","gateway_policy":true,"tool_intents":true"#,
            r#","gateway_policy":true,"usage_cost":true"#,
            r#","gateway_policy":true,"stream_result":true"#,
            r#","gateway_policy":true,"pipeline":"prepare""#,
            r#","gateway_policy":true,"pipeline":"failover""#,
            r#","gateway_policy":1"#,
            r#","gateway_policy":null"#,
        ] {
            let frame = start("stream", flags);
            assert!(parse_start(START, frame.as_bytes())
                .and_then(|start| start.request_start())
                .is_err());
        }
    }

    #[test]
    fn ai_gateway_data_replies_require_live_read_only_authority() {
        let role = ReplyRole::AiGatewayData;
        assert!(role.effect_check().is_none());
        let declined = AiGatewayDataRequiresLegacyCodec.into();
        assert_eq!(
            role.failure_code(WireAuthority::Live, &declined),
            "legacy-read-required"
        );
        for phase in [
            WireAuthority::AwaitingLive,
            WireAuthority::PublicationPermitted,
        ] {
            assert_eq!(role.validate_result(phase, b"{}"), Err("unavailable"));
            assert_eq!(role.failure_code(phase, &declined), "unavailable");
        }
        let live = WireAuthority::Live;
        assert_eq!(role.validate_result(live, b"{}"), Ok(()));
        assert_eq!(role.validate_result(live, b"[]"), Err("internal"));
        assert_eq!(
            role.result_limit(live),
            (AI_GATEWAY_DATA_LIMIT, "limit-exceeded")
        );
        assert_eq!(
            role.failure_code(live, &ModelRegistryRequiresLegacyRead.into()),
            "unavailable"
        );
    }

    #[test]
    fn application_presentation_start_requires_an_exact_role_and_bounded_length() {
        let start = |descriptor: &str, extra: &str| {
            start_frame(&format!(
                r#", "application_presentation":{descriptor}{extra}"#
            ))
        };
        for (name, role) in [
            ("settings", ApplicationPresentationRole::Settings),
            ("catalog", ApplicationPresentationRole::Catalog),
            ("commands", ApplicationPresentationRole::Commands),
        ] {
            for input_bytes in [1, APPLICATION_PRESENTATION_INPUT_LIMIT] {
                let frame = start(
                    &format!(r#"{{"role":"{name}","input_bytes":{input_bytes}}}"#),
                    "",
                );
                assert!(matches!(
                    parse_start(START, frame.as_bytes()).unwrap().request_start(),
                    Ok(RequestStart::ApplicationPresentation { role: parsed, input_bytes: declared })
                        if parsed == role && declared == input_bytes
                ));
                assert!(parse_start(START_APPEND, frame.as_bytes()).is_err());
            }
        }
        for descriptor in [
            "null".to_owned(),
            r#"["settings",1]"#.to_owned(),
            r#"{"input_bytes":1}"#.to_owned(),
            r#"{"role":"Settings","input_bytes":1}"#.to_owned(),
            r#"{"role":"settings","input_bytes":0}"#.to_owned(),
            r#"{"role":"settings","input_bytes":-1}"#.to_owned(),
            r#"{"role":"settings","input_bytes":true}"#.to_owned(),
            r#"{"role":"settings","role":"catalog","input_bytes":1}"#.to_owned(),
            r#"{"role":"settings","input_bytes":1,"manifest":"sha256:untrusted"}"#.to_owned(),
            format!(
                r#"{{"role":"settings","input_bytes":{}}}"#,
                APPLICATION_PRESENTATION_INPUT_LIMIT + 1
            ),
        ] {
            assert!(
                parse_start(START, start(&descriptor, "").as_bytes())
                    .and_then(|start| start.request_start())
                    .is_err(),
                "{descriptor}"
            );
        }
        for extra in [
            PROVIDER_REQUEST,
            r#", "model_list":{}"#,
            r#", "apply":{}"#,
            r#", "saved_orchestration":{"input_bytes":1}"#,
            r#", "ai_route":{"role":"generate","input_bytes":1}"#,
            r#", "application_presentation":{"role":"catalog","input_bytes":1}"#,
        ] {
            let frame = start(r#"{"role":"settings","input_bytes":1}"#, extra);
            assert!(
                parse_start(START, frame.as_bytes())
                    .and_then(|start| start.request_start())
                    .is_err(),
                "{extra}"
            );
        }
    }

    #[test]
    fn model_access_projection_start_requires_one_exact_phase_and_bounded_length() {
        let start = |descriptor: &str, extra: &str| {
            start_frame(&format!(
                r#", "model_access_projection":{descriptor}{extra}"#
            ))
        };
        let limit = 1024 * 1024;
        for (name, expected) in [
            ("policy", ModelAccessProjectionPhase::Policy),
            (
                "prepare-catalog",
                ModelAccessProjectionPhase::PrepareCatalog,
            ),
            (
                "project-catalog",
                ModelAccessProjectionPhase::ProjectCatalog,
            ),
        ] {
            for input_bytes in [1, limit] {
                let frame = start(
                    &format!(r#"{{"phase":"{name}","input_bytes":{input_bytes}}}"#),
                    "",
                );
                assert!(matches!(
                    parse_start(START, frame.as_bytes()).unwrap().request_start(),
                    Ok(RequestStart::ModelAccessProjection { phase, input_bytes: declared })
                        if phase == expected && declared == input_bytes
                ));
                assert!(parse_start(START_APPEND, frame.as_bytes()).is_err());
            }
        }
        for (descriptor, extra) in [
            ("null", ""),
            (r#"["policy",1]"#, ""),
            (r#"{"phase":"Policy","input_bytes":1}"#, ""),
            (r#"{"phase":"policy","input_bytes":0}"#, ""),
            (r#"{"phase":"policy","input_bytes":1048577}"#, ""),
            (r#"{"phase":"policy","input_bytes":true}"#, ""),
            (
                r#"{"phase":"policy","phase":"prepare-catalog","input_bytes":1}"#,
                "",
            ),
            (
                r#"{"phase":"policy","input_bytes":1,"role":"model-access-catalog"}"#,
                "",
            ),
            (
                r#"{"phase":"policy","input_bytes":1}"#,
                r#", "provider_snapshot":{"role":"model-access-read"}"#,
            ),
            (
                r#"{"phase":"policy","input_bytes":1}"#,
                r#", "saved_orchestration":{"input_bytes":1}"#,
            ),
        ] {
            let frame = start(descriptor, extra);
            assert!(parse_start(START, frame.as_bytes())
                .and_then(|parsed| parsed.request_start())
                .is_err());
        }
    }

    #[test]
    fn model_access_projection_requires_live_authority_and_only_falls_back_for_compatibility() {
        let role = ReplyRole::ModelAccessProjection;
        let live = WireAuthority::Live;
        let compatibility = ProviderSnapshotRequiresLegacyRead.into();
        let opaque = br#"{"result":{"number":NaN},"snapshot":{}}"#;
        assert!(role.uses_funded_input());
        assert!(role.effect_check().is_none());
        assert_eq!(
            role.failure_code(live, &compatibility),
            "legacy-read-required"
        );
        assert_eq!(
            role.failure_code(live, &NativeArtifactError::Denied.into()),
            "denied"
        );
        assert_eq!(
            role.failure_code(live, &SavedOrchestrationStepError::Invalid.into()),
            "invalid"
        );
        assert_eq!(
            role.failure_code(live, &ModelRegistryRequiresLegacyRead.into()),
            "unavailable"
        );
        assert_eq!(role.validate_result(live, opaque), Ok(()));
        assert_eq!(
            role.result_limit(live),
            (PROVIDER_SNAPSHOT_REPLY_LIMIT, "legacy-read-required")
        );
        for authority in [
            WireAuthority::AwaitingLive,
            WireAuthority::PublicationPermitted,
        ] {
            assert_eq!(role.failure_code(authority, &compatibility), "unavailable");
            assert_eq!(role.validate_result(authority, opaque), Err("unavailable"));
        }

        let (mut listener, mut host) = UnixStream::pair().unwrap();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        RequestStart::ModelAccessProjection {
            phase: ModelAccessProjectionPhase::Policy,
            input_bytes: 1,
        }
        .reject_missing_launch_proof(&mut listener, deadline)
        .unwrap();
        assert_eq!(read_header(&mut host, deadline).unwrap(), (FAILURE, 6));
        let mut denied = [0; 6];
        read_exact(&mut host, &mut denied, deadline).unwrap();
        assert_eq!(&denied, b"denied");
    }

    #[test]
    fn conversation_complete_start_requires_one_exact_phase_and_its_length() {
        let start = |descriptor: &str, extra: &str| {
            start_frame(&format!(r#", "conversation_complete":{descriptor}{extra}"#))
        };
        for (name, phase, limit) in [
            ("prepare", ConversationCompletePhase::Prepare, 1024 * 1024),
            ("finish", ConversationCompletePhase::Finish, 512 * 1024),
        ] {
            for input_bytes in [1, limit] {
                let frame = start(
                    &format!(r#"{{"phase":"{name}","input_bytes":{input_bytes}}}"#),
                    "",
                );
                assert!(matches!(
                    parse_start(START, frame.as_bytes()).unwrap().request_start(),
                    Ok(RequestStart::ConversationComplete { phase: actual, input_bytes: declared })
                        if actual == phase && declared == input_bytes
                ));
                assert!(parse_start(START_APPEND, frame.as_bytes()).is_err());
            }
            for (input_bytes, expected) in [
                (0, SavedOrchestrationStepError::Invalid),
                (limit + 1, SavedOrchestrationStepError::Capacity),
            ] {
                let frame = start(
                    &format!(r#"{{"phase":"{name}","input_bytes":{input_bytes}}}"#),
                    "",
                );
                let result = parse_start(START, frame.as_bytes())
                    .unwrap()
                    .request_start();
                assert!(
                    matches!(result, Err(error) if error.is::<SavedOrchestrationStepError>()
                    && error.downcast_ref() == Some(&expected))
                );
            }
        }
        for (descriptor, extra) in [
            ("null", ""),
            (r#"["prepare",1]"#, ""),
            (r#"{"input_bytes":1}"#, ""),
            (r#"{"phase":"Prepare","input_bytes":1}"#, ""),
            (r#"{"phase":"finalize","input_bytes":1}"#, ""),
            (r#"{"phase":"prepare","input_bytes":true}"#, ""),
            (
                r#"{"phase":"prepare","phase":"finish","input_bytes":1}"#,
                "",
            ),
            (
                r#"{"phase":"prepare","input_bytes":1,"manifest":"sha256:untrusted"}"#,
                "",
            ),
            (r#"{"phase":"prepare","input_bytes":1}"#, PROVIDER_REQUEST),
            (
                r#"{"phase":"prepare","input_bytes":1}"#,
                r#", "model_list":{}"#,
            ),
            (r#"{"phase":"prepare","input_bytes":1}"#, r#", "apply":{}"#),
            (
                r#"{"phase":"prepare","input_bytes":1}"#,
                r#", "application_presentation":{"role":"catalog","input_bytes":1}"#,
            ),
            (
                r#"{"phase":"prepare","input_bytes":1}"#,
                r#", "conversation_complete":{"phase":"finish","input_bytes":1}"#,
            ),
        ] {
            let frame = start(descriptor, extra);
            assert!(parse_start(START, frame.as_bytes())
                .and_then(|start| start.request_start())
                .is_err());
        }
    }

    #[test]
    fn conversation_complete_denies_missing_proof_before_body_admission() {
        for phase in [
            ConversationCompletePhase::Prepare,
            ConversationCompletePhase::Finish,
        ] {
            let (mut client, mut server) = UnixStream::pair().unwrap();
            let deadline = Instant::now() + FRAME_TIMEOUT;
            RequestStart::ConversationComplete {
                phase,
                input_bytes: 2,
            }
            .reject_missing_launch_proof(&mut server, deadline)
            .unwrap();
            let (tag, bytes) = read_header(&mut client, deadline).unwrap();
            assert_eq!(tag, FAILURE);
            assert_eq!(
                read_input_bytes(&mut client, bytes, deadline).unwrap(),
                b"denied"
            );
        }
    }

    #[test]
    fn funded_data_reads_exact_body_before_the_live_check() {
        for input_bytes in [
            APPLICATION_PRESENTATION_INPUT_LIMIT,
            ConversationCompletePhase::Prepare.input_limit(),
            ConversationCompletePhase::Finish.input_limit(),
        ] {
            let (mut client, server) = UnixStream::pair().unwrap();
            client.set_nonblocking(true).unwrap();
            server.set_nonblocking(true).unwrap();
            let input = format!(
                r#"{{"example":"猫{}"}}"#,
                "a".repeat(input_bytes - r#"{"example":"猫"}"#.len())
            );
            assert_eq!(input.len(), input_bytes);
            let deadline = Instant::now() + FRAME_TIMEOUT;
            let worker = thread::spawn(move || {
                let mut wire = Wire::new(server);
                assert!(!wire.funded_input_received);
                let input = wire.receive_funded_input(input_bytes, deadline).unwrap();
                assert!(wire.funded_input_received);
                wire.exchange_authority(AuthorityCheck::Live, deadline)
                    .unwrap();
                assert!(wire.authority == WireAuthority::Live);
                assert!(wire.receive_funded_input(input_bytes, deadline).is_err());
                input
            });
            assert_eq!(read_header(&mut client, deadline).unwrap(), (BODY_READY, 0));
            send_frame(&mut client, ORCHESTRATION_INPUT, input.as_bytes(), deadline).unwrap();
            assert_eq!(read_header(&mut client, deadline).unwrap(), (CHECK, 0));
            send_frame(&mut client, LIVE, &[], deadline).unwrap();
            assert_eq!(worker.join().unwrap(), input.as_bytes());
        }
    }

    #[test]
    fn funded_data_body_stops_on_cancel_bad_framing_or_deadline() {
        for (tag, size, expected) in [
            (CANCEL, 0u32, "cancelled"),
            (ORCHESTRATION_INPUT, 1u32, "invalid"),
            (PUBLISH_PERMIT, 0u32, "invalid"),
        ] {
            let (mut client, server) = UnixStream::pair().unwrap();
            let deadline = Instant::now() + FRAME_TIMEOUT;
            let worker = thread::spawn(move || {
                let mut wire = Wire::new(server);
                assert!(wire.receive_funded_input(2, deadline).is_err());
                assert!(!wire.funded_input_received);
                assert_eq!(wire.stop.unwrap().reply(), Some(expected));
            });
            assert_eq!(read_header(&mut client, deadline).unwrap(), (BODY_READY, 0));
            let [a, b, c, d] = size.to_be_bytes();
            client.write_all(&[tag, a, b, c, d]).unwrap();
            worker.join().unwrap();
        }
        let (_client, server) = UnixStream::pair().unwrap();
        let mut wire = Wire::new(server);
        let deadline = Instant::now() + Duration::from_millis(30);
        assert!(wire.receive_funded_input(2, deadline).is_err());
        assert!(matches!(wire.stop, Some(Stop::Deadline)));
        assert!(!wire.funded_input_received);
    }

    #[test]
    fn application_presentation_returns_live_bounded_objects_without_effects_or_fallback() {
        let role = ReplyRole::ApplicationPresentation;
        assert!(role.effect_check().is_none());
        for authority in [
            WireAuthority::AwaitingLive,
            WireAuthority::PublicationPermitted,
        ] {
            assert_eq!(role.validate_result(authority, b"{}"), Err("unavailable"));
        }
        let live = WireAuthority::Live;
        let at_limit = format!(
            r#"{{"data":"{}"}}"#,
            "a".repeat(APPLICATION_PRESENTATION_RESULT_LIMIT - r#"{"data":""}"#.len())
        );
        assert_eq!(at_limit.len(), APPLICATION_PRESENTATION_RESULT_LIMIT);
        assert_eq!(role.validate_result(live, at_limit.as_bytes()), Ok(()));
        assert_eq!(
            role.validate_result(live, (at_limit + " ").as_bytes()),
            Err("limit-exceeded")
        );
        for invalid in [b"null".as_slice(), b"[]", b"{}{}", b"{\"\xff\":1}"] {
            assert_eq!(role.validate_result(live, invalid), Err("internal"));
        }
        assert_eq!(
            role.failure_code(live, &ModelRegistryRequiresLegacyRead.into()),
            "unavailable"
        );
        assert_eq!(
            role.failure_code(live, &SavedOrchestrationStepError::Invalid.into()),
            "invalid"
        );
        assert_eq!(
            role.failure_code(live, &SavedOrchestrationStepError::Capacity.into()),
            "limit-exceeded"
        );
    }

    #[test]
    fn conversation_complete_returns_live_bounded_objects_without_effects_or_fallback() {
        let role = ReplyRole::ConversationComplete;
        assert!(role.uses_funded_input());
        assert!(role.effect_check().is_none());
        assert!(role
            .validate_result(WireAuthority::AwaitingLive, b"{}")
            .is_err());
        assert!(role
            .validate_result(WireAuthority::PublicationPermitted, b"{}")
            .is_err());
        let live = WireAuthority::Live;
        let mut output = format!(
            r#"{{"data":"{}"}}"#,
            "a".repeat(CONVERSATION_COMPLETE_RESULT_LIMIT - r#"{"data":""}"#.len())
        );
        assert!(role.validate_result(live, output.as_bytes()).is_ok());
        output.push(' ');
        assert_eq!(
            role.validate_result(live, output.as_bytes()),
            Err("limit-exceeded")
        );
        assert!(role.validate_result(live, b"[]").is_err());
        for (error, code) in [
            (ModelRegistryRequiresLegacyRead.into(), "unavailable"),
            (NativeArtifactError::Denied.into(), "denied"),
            (
                NativeArtifactError::Execution(NativeDataIoError::Cancelled).into(),
                "cancelled",
            ),
            (SavedOrchestrationStepError::Invalid.into(), "invalid"),
            (
                SavedOrchestrationStepError::Capacity.into(),
                "limit-exceeded",
            ),
        ] {
            assert_eq!(role.failure_code(live, &error), code);
        }
    }

    #[test]
    fn saved_orchestration_start_requires_a_bounded_length_and_no_other_operation() {
        let start = |descriptor: &str, extra: &str| {
            start_frame(&format!(r#", "saved_orchestration":{descriptor}{extra}"#))
        };
        for input_bytes in [1, SAVED_ORCHESTRATION_FRAME_LIMIT] {
            let frame = start(&format!(r#"{{"input_bytes":{input_bytes}}}"#), "");
            validate_start_size(START, frame.len()).unwrap();
            assert!(matches!(
                parse_start(START, frame.as_bytes()).unwrap().request_start(),
                Ok(RequestStart::SavedOrchestration { input_bytes: declared })
                    if declared == input_bytes
            ));
            assert!(parse_start(START_APPEND, frame.as_bytes()).is_err());
        }
        for descriptor in [
            "{}".to_owned(),
            "null".to_owned(),
            "[1]".to_owned(),
            r#"{"input_bytes":0}"#.to_owned(),
            r#"{"input_bytes":-1}"#.to_owned(),
            r#"{"input_bytes":true}"#.to_owned(),
            r#"{"input_bytes":"1"}"#.to_owned(),
            r#"{"input_bytes":1,"input_bytes":1}"#.to_owned(),
            r#"{"input_bytes":1,"input":{}}"#.to_owned(),
            format!(
                r#"{{"input_bytes":{}}}"#,
                SAVED_ORCHESTRATION_FRAME_LIMIT + 1
            ),
        ] {
            let frame = start(&descriptor, "");
            assert!(
                parse_start(START, frame.as_bytes())
                    .and_then(|start| start.request_start())
                    .is_err(),
                "{descriptor}"
            );
        }
        for extra in [
            PROVIDER_REQUEST,
            r#", "model_list":{}"#,
            r#", "apply":{}"#,
            r#", "saved_orchestration":{"input_bytes":1}"#,
        ] {
            let frame = start(r#"{"input_bytes":1}"#, extra);
            assert!(
                parse_start(START, frame.as_bytes())
                    .and_then(|start| start.request_start())
                    .is_err(),
                "{extra}"
            );
        }
    }

    #[test]
    fn saved_orchestration_receives_large_exact_bytes_after_body_ready() {
        let (mut client, mut server) = UnixStream::pair().unwrap();
        client.set_nonblocking(true).unwrap();
        server.set_nonblocking(true).unwrap();
        let padding_bytes = SAVED_ORCHESTRATION_FRAME_LIMIT - r#"{"input":""}"#.len();
        let padding = "猫".repeat(padding_bytes / "猫".len());
        let remainder = "a".repeat(padding_bytes % "猫".len());
        let input = format!(r#"{{"input":"{padding}{remainder}"}}"#).into_bytes();
        assert_eq!(input.len(), SAVED_ORCHESTRATION_FRAME_LIMIT);
        let input_bytes = input.len();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        let worker = thread::spawn(move || {
            let input = receive_orchestration_input(&mut server, input_bytes, deadline)
                .unwrap()
                .unwrap();
            let mut wire = Wire::new(server);
            wire.exchange_authority(AuthorityCheck::Live, deadline)
                .unwrap();
            assert!(wire.authority == WireAuthority::Live);
            input
        });
        assert_eq!(read_header(&mut client, deadline).unwrap(), (BODY_READY, 0));
        send_frame(&mut client, ORCHESTRATION_INPUT, &input, deadline).unwrap();
        assert_eq!(read_header(&mut client, deadline).unwrap(), (CHECK, 0));
        send_frame(&mut client, LIVE, &[], deadline).unwrap();
        assert_eq!(worker.join().unwrap(), input);
    }

    #[test]
    fn saved_orchestration_rejects_wrong_body_header_without_reading_a_body() {
        let input_bytes = 17;
        for (tag, size) in [
            (LIVE, input_bytes),
            (ORCHESTRATION_INPUT, 0),
            (ORCHESTRATION_INPUT, input_bytes - 1),
            (ORCHESTRATION_INPUT, input_bytes + 1),
            (ORCHESTRATION_INPUT, u32::MAX),
            (CANCEL, 1),
        ] {
            let (mut client, mut server) = UnixStream::pair().unwrap();
            let [a, b, c, d] = size.to_be_bytes();
            client.write_all(&[tag, a, b, c, d]).unwrap();
            let deadline = Instant::now() + FRAME_TIMEOUT;
            let error = receive_orchestration_input(&mut server, input_bytes as usize, deadline)
                .unwrap_err();
            assert_eq!(error.to_string(), "invalid Saved Orchestration input frame");
            assert_eq!(read_header(&mut client, deadline).unwrap(), (BODY_READY, 0));
        }
    }

    #[test]
    fn saved_orchestration_body_phase_honors_cancel_and_the_frame_deadline() {
        let (mut client, mut server) = UnixStream::pair().unwrap();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        send_frame(&mut client, CANCEL, &[], deadline).unwrap();
        assert!(receive_orchestration_input(&mut server, 2, deadline)
            .unwrap()
            .is_none());
        assert_eq!(read_header(&mut client, deadline).unwrap(), (BODY_READY, 0));

        client
            .write_all(&[ORCHESTRATION_INPUT, 0, 0, 0, 2, b'{'])
            .unwrap();
        let deadline = Instant::now() + Duration::from_millis(30);
        let error = receive_orchestration_input(&mut server, 2, deadline).unwrap_err();
        assert_eq!(
            error.downcast_ref::<io::Error>().unwrap().kind(),
            io::ErrorKind::TimedOut
        );
    }

    #[test]
    fn saved_orchestration_requires_live_object_results_and_forbids_publication_and_fallback() {
        let role = ReplyRole::SavedOrchestration;
        assert!(!role.permits_publication());
        for authority in [
            WireAuthority::AwaitingLive,
            WireAuthority::PublicationPermitted,
        ] {
            assert_eq!(role.validate_result(authority, b"{}"), Err("unavailable"));
        }
        let live = WireAuthority::Live;
        assert_eq!(
            role.validate_result(live, r#" {"猫":1} "#.as_bytes()),
            Ok(())
        );
        for invalid in [b"".as_slice(), b"null", b"[]", b"{}{}", b"{\"\xff\":1}"] {
            assert_eq!(role.validate_result(live, invalid), Err("internal"));
        }
        let at_limit = format!(
            "{{\"result\":\"{}\"}}",
            "a".repeat(SAVED_ORCHESTRATION_FRAME_LIMIT - 13)
        );
        assert_eq!(at_limit.len(), SAVED_ORCHESTRATION_FRAME_LIMIT);
        assert_eq!(role.validate_result(live, at_limit.as_bytes()), Ok(()));
        let too_large = at_limit + " ";
        assert_eq!(
            role.validate_result(live, too_large.as_bytes()),
            Err("limit-exceeded")
        );
        assert_eq!(
            role.failure_code(live, &ModelRegistryRequiresLegacyRead.into()),
            "unavailable"
        );
        assert_eq!(
            role.failure_code(live, &SavedOrchestrationStepError::Invalid.into()),
            "invalid"
        );
        assert_eq!(
            role.failure_code(live, &SavedOrchestrationStepError::Capacity.into()),
            "limit-exceeded"
        );
    }

    #[test]
    fn save_start_borrows_only_two_object_inputs() {
        let request = r#"{"model_id": "gemma-3-1b"}"#;
        let provider = r#"{"revision":1,"providers":[]}"#;
        let save = format!(r#", "model_save":{{"request":{request},"provider":{provider}}}"#);
        let frame = start_frame(&save);
        let start: Start<'_> = serde_json::from_str(&frame).unwrap();
        let NativeRegistryRequest::ModelSave {
            request: raw,
            provider: snapshot,
        } = start.operation().unwrap()
        else {
            panic!("expected Model Save");
        };
        assert_eq!(raw, request.as_bytes());
        assert_eq!(snapshot, provider.as_bytes());
        for extra in [
            PROVIDER_REQUEST,
            r#", "model_list": {}"#,
            r#", "apply": {}"#,
        ] {
            let frame = start_frame(&format!("{save}{extra}"));
            assert!(serde_json::from_str::<Start<'_>>(&frame)
                .unwrap()
                .operation()
                .is_err());
        }
        for body in [
            r#"null"#,
            r#"{}"#,
            r#"{"request":[],"provider":{}}"#,
            r#"{"request":{},"provider":{},"permit":true}"#,
        ] {
            let frame = start_frame(&format!(r#", "model_save":{body}"#));
            assert!(serde_json::from_str::<Start<'_>>(&frame).is_err());
        }
    }

    #[test]
    fn conversation_get_accepts_only_its_fixed_caller_and_conversation_id() {
        for (wire_caller, expected_caller) in [
            ("shell", ConversationReadCaller::Shell),
            ("saved-turn", ConversationReadCaller::SavedTurn),
            (
                "saved-orchestration",
                ConversationReadCaller::SavedOrchestration,
            ),
        ] {
            let frame = start_frame(&format!(
                r#", "conversation_get":{{"caller":"{wire_caller}","conversation_id":"会話"}}"#
            ));
            let start: Start<'_> = serde_json::from_str(&frame).unwrap();
            let NativeRegistryRequest::ConversationGet {
                caller,
                conversation_id,
            } = start.operation().unwrap()
            else {
                panic!("expected Conversation Get");
            };
            assert_eq!(caller, expected_caller);
            assert_eq!(conversation_id, "会話");
        }

        for body in [
            r#"null"#,
            r#"{}"#,
            r#"["shell","stored"]"#,
            r#"{"caller":"shell"}"#,
            r#"{"conversation_id":"stored"}"#,
            r#"{"caller":"unknown","conversation_id":"stored"}"#,
            r#"{"caller":"saved_turn","conversation_id":"stored"}"#,
            r#"{"caller":{"shell":null},"conversation_id":"stored"}"#,
            r#"{"caller":"shell","conversation_id":123}"#,
            r#"{"caller":"shell","conversation_id":"stored","profile_id":"other"}"#,
            r#"{"caller":"shell","caller":"saved-turn","conversation_id":"stored"}"#,
        ] {
            let frame = start_frame(&format!(r#", "conversation_get":{body}"#));
            assert!(serde_json::from_str::<Start<'_>>(&frame).is_err(), "{body}");
        }
    }

    #[test]
    fn linked_conversation_context_accepts_only_the_fixed_child_and_saved_caller() {
        let get = r#", "conversation_get":{"caller":"saved-orchestration","conversation_id":"Side.1:child","linked_context":true}"#;
        let frame = start_frame(get);
        let start = parse_start(START, frame.as_bytes()).unwrap();
        let NativeRegistryRequest::LinkedConversationContext { conversation_id } =
            start.operation().unwrap()
        else {
            panic!("expected private linked Conversation context");
        };
        assert_eq!(conversation_id, "Side.1:child");

        for rejected in [
            get.replace("saved-orchestration", "shell"),
            get.replace("saved-orchestration", "saved-turn"),
            get.replace("Side.1:child", "../child"),
            get.replace("Side.1:child", &"x".repeat(257)),
            get.replace("Side.1:child", ""),
            get.replace("true", "null"),
            get.replace("true", "true,\"parent_id\":\"chosen\""),
            format!("{get}{PROVIDER_REQUEST}"),
        ] {
            let frame = start_frame(&rejected);
            assert!(parse_start(START, frame.as_bytes())
                .and_then(|start| start.operation())
                .is_err());
        }

        let nonce = "0123456789abcdef0123456789abcdef";
        let linked_get = |parent: &str, context: &str| {
            start_frame(&format!(
                r#", "conversation_get":{{"caller":"saved-orchestration","conversation_id":"Side.1:child","linked_parent":{parent}{context}}}"#
            ))
        };
        for (mode, expected) in [
            ("retain", LinkedConversationReadMode::Retain),
            ("reuse", LinkedConversationReadMode::Reuse),
            (
                "retain-for-recheck",
                LinkedConversationReadMode::RetainForRecheck,
            ),
            ("recheck-child", LinkedConversationReadMode::RecheckChild),
            ("recheck-parent", LinkedConversationReadMode::RecheckParent),
        ] {
            let parent = format!(r#"{{"mode":"{mode}","nonce":"{nonce}"}}"#);
            for context in ["", r#","linked_context":false"#] {
                let frame = linked_get(&parent, context);
                let start = parse_start(START, frame.as_bytes()).unwrap();
                let NativeRegistryRequest::LinkedConversationRead {
                    conversation_id,
                    nonce: parsed_nonce,
                    mode: parsed_mode,
                } = start.operation().unwrap()
                else {
                    panic!("expected private linked Conversation read");
                };
                assert_eq!(conversation_id, "Side.1:child");
                assert_eq!(parsed_nonce.as_slice(), nonce.as_bytes());
                assert_eq!(parsed_mode, expected);
            }
        }

        let parent = format!(r#"{{"mode":"retain","nonce":"{nonce}"}}"#);
        let valid = linked_get(&parent, "");
        for rejected in [
            linked_get("null", ""),
            linked_get("[]", ""),
            linked_get("{}", ""),
            linked_get(&format!(r#"["retain","{nonce}"]"#), ""),
            linked_get(&format!(r#"{{"nonce":"{nonce}"}}"#), ""),
            linked_get(r#"{"mode":"retain"}"#, ""),
            linked_get(&parent.replace("retain", "fetch"), ""),
            linked_get(&parent.replace("retain", "Retain"), ""),
            linked_get(&parent.replace(nonce, &nonce.to_uppercase()), ""),
            linked_get(&parent.replace(nonce, &nonce[..31]), ""),
            linked_get(&parent.replace(nonce, &format!("{nonce}0")), ""),
            linked_get(&parent.replace("abcd", "abcz"), ""),
            linked_get(&parent.replace("abcd", "abc猫"), ""),
            linked_get(&parent.replace('}', r#","parent_id":"chosen"}"#), ""),
            linked_get(&parent.replace('}', r#","mode":"reuse"}"#), ""),
            linked_get(&parent, r#","linked_context":true"#),
            valid.replace("saved-orchestration", "shell"),
            valid.replace("saved-orchestration", "saved-turn"),
            valid.replace("Side.1:child", "../child"),
            valid.replace("Side.1:child", "子"),
            valid.replace("Side.1:child", &"x".repeat(257)),
            valid.replace("Side.1:child", ""),
        ] {
            assert!(
                parse_start(START, rejected.as_bytes())
                    .and_then(|start| start.operation())
                    .is_err(),
                "{rejected}"
            );
        }
    }

    #[test]
    fn conversation_get_cannot_share_a_start_with_another_operation() {
        let get = r#", "conversation_get":{"caller":"shell","conversation_id":"stored"}"#;
        for extra in [
            PROVIDER_REQUEST,
            r#", "apply": {}"#,
            r#", "model_list": {}"#,
            r#", "conversation_list": {}"#,
            r#", "model_save": {"request":{},"provider":{}}"#,
        ] {
            let frame = start_frame(&format!("{get}{extra}"));
            assert!(serde_json::from_str::<Start<'_>>(&frame)
                .unwrap()
                .operation()
                .is_err());
        }
        assert!(serde_json::from_str::<Start<'_>>(&start_frame(&format!("{get}{get}"))).is_err());
        assert_eq!(
            failure_code(&ConversationRegistryRequiresLegacyRead.into()),
            "legacy-read-required"
        );
    }

    #[test]
    fn conversation_list_start_has_no_caller_or_arguments() {
        let start = start_frame(r#", "conversation_list": {}"#);
        assert!(matches!(
            serde_json::from_str::<Start<'_>>(&start)
                .unwrap()
                .operation()
                .unwrap(),
            NativeRegistryRequest::ConversationList
        ));
        for value in [
            "null",
            "[]",
            r#"{"caller":"shell"}"#,
            r#"{"conversation_id":"id"}"#,
        ] {
            let start = start_frame(&format!(r#", "conversation_list": {value}"#));
            assert!(serde_json::from_str::<Start<'_>>(&start).is_err());
        }
    }

    #[test]
    fn saved_receipt_preserves_the_raw_bounded_turn_id_for_each_caller() {
        for (wire_caller, expected_caller, id) in [
            ("saved-turn", OriginalSavedReceiptRole::SavedTurn, ""),
            (
                "saved-reconcile",
                OriginalSavedReceiptRole::SavedReconcile,
                "\u{001c}  保存🧾-turn\t",
            ),
            (
                "side-chat-turn",
                OriginalSavedReceiptRole::SideChatTurn,
                "side-chat/保存",
            ),
            (
                "side-chat-reconcile",
                OriginalSavedReceiptRole::SideChatReconcile,
                &"🧾".repeat(SAVED_RECEIPT_TURN_ID_MAX_CHARS),
            ),
        ] {
            let encoded_id = serde_json::to_string(id).unwrap();
            let frame = start_frame(&format!(
                r#", "conversation_receipt":{{"caller":"{wire_caller}","turn_id":{encoded_id}}}"#
            ));
            assert!(frame.len() <= JSON_LIMIT);
            let start = parse_start(START, frame.as_bytes()).unwrap();
            assert!(parse_start(START_APPEND, frame.as_bytes()).is_err());
            let NativeRegistryRequest::ConversationReceipt { caller, turn_id } =
                start.operation().unwrap()
            else {
                panic!("expected saved receipt lookup");
            };
            assert_eq!(caller, expected_caller);
            assert_eq!(turn_id, id);
        }

        let oversized = "🧾".repeat(SAVED_RECEIPT_TURN_ID_MAX_CHARS + 1);
        for caller in ["saved-turn", "side-chat-reconcile"] {
            let frame = start_frame(&format!(
                r#", "conversation_receipt":{{"caller":"{caller}","turn_id":"{oversized}"}}"#
            ));
            assert!(frame.len() <= JSON_LIMIT);
            assert!(parse_start(START, frame.as_bytes()).is_err());
        }
    }

    #[test]
    fn saved_receipt_rejects_other_operations_and_unrecognized_fields() {
        for body in [
            r#"null"#,
            r#"["saved-turn","turn-id"]"#,
            r#"{"caller":"shell","turn_id":"stored"}"#,
            r#"{"caller":"saved-orchestration","turn_id":"stored"}"#,
            r#"{"caller":"side-chat","turn_id":"stored"}"#,
            r#"{"caller":"side-chat-reconcile ","turn_id":"stored"}"#,
            r#"{"caller":"side_chat_turn","turn_id":"stored"}"#,
            r#"{"caller":"saved-turn"}"#,
            r#"{"caller":"saved-turn","turn_id":42}"#,
            r#"{"caller":"saved-turn","turn_id":"stored","profile_id":"elsewhere"}"#,
            r#"{"caller":"saved-turn","turn_id":"stored","publish":true}"#,
            r#"{"caller":"saved-turn","caller":"saved-reconcile","turn_id":"stored"}"#,
            r#"{"caller":"saved-turn","turn_id":"stored","turn_id":"elsewhere"}"#,
        ] {
            let frame = start_frame(&format!(r#", "conversation_receipt":{body}"#));
            assert!(serde_json::from_str::<Start<'_>>(&frame).is_err(), "{body}");
        }

        let receipt = r#", "conversation_receipt":{"caller":"saved-turn","turn_id":"stored"}"#;
        for other in [
            PROVIDER_REQUEST,
            r#", "apply": {}"#,
            r#", "model_list": {}"#,
            r#", "model_save": {"request":{},"provider":{}}"#,
            r#", "conversation_get": {"caller":"saved-turn","conversation_id":"stored"}"#,
            r#", "model_resolve": {"role":"generate","identifier":"local"}"#,
        ] {
            let frame = start_frame(&format!("{receipt}{other}"));
            let start: Start<'_> = serde_json::from_str(&frame).unwrap();
            assert!(start.operation().is_err(), "{other}");
        }
        assert!(
            serde_json::from_str::<Start<'_>>(&start_frame(&format!("{receipt}{receipt}")))
                .is_err()
        );
    }

    #[test]
    fn model_resolve_accepts_only_fixed_roles_and_canonical_identifiers() {
        let identifier = format!("A._:/-{}", "9".repeat(250));
        for (wire_role, expected_role) in [
            ("quote-generate", OriginalModelResolveRole::QuoteGenerate),
            ("quote-stream", OriginalModelResolveRole::QuoteStream),
            ("generate", OriginalModelResolveRole::Generate),
            ("stream", OriginalModelResolveRole::Stream),
        ] {
            let frame = start_frame(&format!(
                r#", "model_resolve":{{"role":"{wire_role}","identifier":"{identifier}"}}"#
            ));
            let start: Start<'_> = serde_json::from_str(&frame).unwrap();
            let NativeRegistryRequest::ModelResolve {
                role,
                identifier: decoded_identifier,
            } = start.operation().unwrap()
            else {
                panic!("expected Model Resolve");
            };
            assert_eq!(role, expected_role);
            assert_eq!(decoded_identifier, identifier);
        }

        for invalid in ["", "-a", "a b", " a", "a ", "a\n", "a/猫", &"a".repeat(257)] {
            let invalid = serde_json::to_string(invalid).unwrap();
            let frame = start_frame(&format!(
                r#", "model_resolve":{{"role":"generate","identifier":{invalid}}}"#
            ));
            assert!(serde_json::from_str::<Start<'_>>(&frame).is_err());
        }

        for body in [
            r#"null"#,
            r#"["generate","local"]"#,
            r#"{"identifier":"local"}"#,
            r#"{"role":"generate"}"#,
            r#"{"role":"quote_generate","identifier":"local"}"#,
            r#"{"role":{"generate":null},"identifier":"local"}"#,
            r#"{"role":"generate","identifier":123}"#,
            r#"{"role":"generate","identifier":"local","profile_id":"elsewhere"}"#,
            r#"{"role":"generate","role":"stream","identifier":"local"}"#,
            r#"{"role":"generate","identifier":"local","identifier":"elsewhere"}"#,
        ] {
            let frame = start_frame(&format!(r#", "model_resolve":{body}"#));
            assert!(serde_json::from_str::<Start<'_>>(&frame).is_err(), "{body}");
        }
    }

    #[test]
    fn model_resolve_cannot_share_a_start_with_another_operation() {
        let resolve = r#", "model_resolve":{"role":"generate","identifier":"local"}"#;
        for extra in [
            PROVIDER_REQUEST,
            r#", "apply": {}"#,
            r#", "model_list": {}"#,
            r#", "model_save": {"request":{},"provider":{}}"#,
            r#", "conversation_get": {"caller":"shell","conversation_id":"stored"}"#,
        ] {
            let frame = start_frame(&format!("{resolve}{extra}"));
            assert!(serde_json::from_str::<Start<'_>>(&frame)
                .unwrap()
                .operation()
                .is_err());
        }
        let repeated = start_frame(&format!("{resolve}{resolve}"));
        assert!(serde_json::from_str::<Start<'_>>(&repeated).is_err());
        assert_eq!(
            failure_code(&ModelRegistryRequiresLegacyRead.into()),
            "legacy-read-required"
        );
        assert_eq!(failure_code(&NativeArtifactError::Denied.into()), "denied");
    }

    fn settlement_frame(settlement: &serde_json::Value) -> String {
        start_frame(&format!(r#", "turn_settlement":{settlement}"#))
    }

    fn settlement_body() -> serde_json::Value {
        serde_json::json!({
            "caller": "saved-turn",
            "turn_id": "turn:one-2",
            "input_digest": format!("sha256:{}", "a".repeat(64)),
            "expected_revision": null,
            "result_reference": r#"{"conversation_id":"会話 \"1\"","conversation_revision":7}"#,
        })
    }

    #[test]
    fn turn_settlement_preserves_reference_bytes_and_requires_explicit_revision() {
        let maximal_id = format!("a._:-{}", "0".repeat(251));
        let maximal_reference = format!(
            r#"{{"value":"{}"}}"#,
            "x".repeat(TURN_SETTLEMENT_REFERENCE_LIMIT - r#"{"value":""}"#.len()),
        );
        for (role, expected, revision, id, reference) in [
            (
                "saved-turn",
                SettlementCaller::SavedTurn,
                None,
                "turn:one-2",
                r#"{"conversation_id":"会話 \"1\"","conversation_revision":7}"#,
            ),
            (
                "saved-reconcile",
                SettlementCaller::SavedReconcile,
                Some(u64::MAX),
                maximal_id.as_str(),
                maximal_reference.as_str(),
            ),
            (
                "side-chat-turn",
                SettlementCaller::SideChatTurn,
                None,
                "turn:one-2",
                r#"{"conversation_id":"会話 \"1\"","conversation_revision":7}"#,
            ),
            (
                "side-chat-reconcile",
                SettlementCaller::SideChatReconcile,
                Some(7),
                maximal_id.as_str(),
                maximal_reference.as_str(),
            ),
        ] {
            let mut body = settlement_body();
            body["caller"] = role.into();
            body["expected_revision"] = serde_json::json!(revision);
            body["turn_id"] = id.into();
            body["result_reference"] = reference.into();
            let frame = settlement_frame(&body);
            let start: Start<'_> = serde_json::from_str(&frame).unwrap();
            let NativeRegistryRequest::TurnSettlement {
                caller,
                turn_id,
                input_digest,
                expected_revision,
                result_reference,
            } = start.operation().unwrap()
            else {
                panic!("expected Turn settlement");
            };
            assert!(caller == expected);
            assert_eq!(turn_id, id);
            assert_eq!(input_digest, body["input_digest"].as_str().unwrap());
            assert_eq!(expected_revision, revision);
            assert_eq!(result_reference, reference);
        }

        for role in ["saved-reconcile", "side-chat-reconcile"] {
            let mut body = settlement_body();
            body["caller"] = role.into();
            let frame = settlement_frame(&body);
            let start: Start<'_> = serde_json::from_str(&frame).unwrap();
            assert!(start.operation().is_err(), "{role}");
            body["expected_revision"] = 0.into();
            assert!(serde_json::from_str::<Start<'_>>(&settlement_frame(&body)).is_err());
            body.as_object_mut().unwrap().remove("expected_revision");
            assert!(serde_json::from_str::<Start<'_>>(&settlement_frame(&body)).is_err());
        }
    }

    #[test]
    fn turn_read_start_accepts_only_a_saved_caller_and_trim_stable_id() {
        let id = "猫".repeat(TURN_READ_ID_MAX_CHARS);
        let operation = |caller: &str, turn_id: &str| {
            let request = serde_json::json!({"caller": caller, "turn_id": turn_id});
            format!(r#", "turn_read":{request}"#)
        };
        for (caller, expected) in [
            ("saved-turn", OriginalSavedReceiptRole::SavedTurn),
            ("saved-reconcile", OriginalSavedReceiptRole::SavedReconcile),
            ("side-chat-turn", OriginalSavedReceiptRole::SideChatTurn),
            (
                "side-chat-reconcile",
                OriginalSavedReceiptRole::SideChatReconcile,
            ),
        ] {
            let frame = start_frame(&operation(caller, &id));
            let start = parse_start(START, frame.as_bytes()).unwrap();
            let NativeRegistryRequest::TurnRead { caller, turn_id } = start.operation().unwrap()
            else {
                panic!("expected Turn read operation");
            };
            assert_eq!(caller, expected);
            assert_eq!(turn_id, id);
            assert!(parse_start(START_APPEND, frame.as_bytes()).is_err());
        }
        for operation in [
            operation("saved-turn", ""),
            operation("saved-turn", "猫  "),
            operation("saved-turn", "\u{001c}猫"),
            operation("saved-turn", &format!("{id}猫")),
            operation("shell", "猫"),
            operation("side-chat", "猫"),
            operation("side-chat-turn ", "猫"),
            r#", "turn_read":null"#.to_owned(),
            r#", "turn_read":["saved-turn","猫"]"#.to_owned(),
            r#", "turn_read":{"caller":"saved-turn"}"#.to_owned(),
            r#", "turn_read":{"caller":"saved-turn","turn_id":1}"#.to_owned(),
            r#", "turn_read":{"caller":"saved-turn","turn_id":"猫","publish":false}"#.to_owned(),
            r#", "turn_read":{"caller":"saved-turn","turn_id":"猫","turn_id":"turn"}"#.to_owned(),
        ] {
            assert!(parse_start(START, start_frame(&operation).as_bytes()).is_err());
        }
        let read = operation("saved-turn", "turn");
        for other in [PROVIDER_REQUEST, r#", "apply":{}"#, r#", "model_list":{}"#] {
            let frame = start_frame(&format!("{read}{other}"));
            let start = parse_start(START, frame.as_bytes()).unwrap();
            assert!(start.operation().is_err());
        }
    }

    #[test]
    fn owner_read_replies_require_live_and_only_explicit_legacy_errors_can_fallback() {
        use LegacySavedTurnSettlementError as Error;
        let role = ReplyRole::TurnRead;
        assert!(!role.permits_publication());
        let codec = Error::RequiresLegacyCodec.into();
        for authority in [
            WireAuthority::AwaitingLive,
            WireAuthority::PublicationPermitted,
        ] {
            assert_eq!(role.failure_code(authority, &codec), "unavailable");
            assert_eq!(role.validate_result(authority, b"null"), Err("unavailable"));
        }
        let live = WireAuthority::Live;
        assert_eq!(role.failure_code(live, &codec), "legacy-read-required");
        assert_eq!(
            role.failure_code(live, &ModelRegistryRequiresLegacyRead.into()),
            "unavailable"
        );
        assert_eq!(role.result_limit(live), (1024 * 1024, "limit-exceeded"));
        for (error, code) in [
            (Error::InvalidRequest, "invalid"),
            (Error::InvalidRecord, "invalid"),
            (Error::Capacity, "limit-exceeded"),
            (Error::Revoked, "cancelled"),
            (Error::Storage, "internal"),
        ] {
            assert_eq!(role.failure_code(live, &error.into()), code);
        }
        assert!(role.validate_result(live, br#"{"id":"turn"}"#).is_ok());
        assert!(role.validate_result(live, b"null").is_ok());
        for output in ["[]", "false", "1", "\"null\"", "{", "{} null"] {
            assert_eq!(
                role.validate_result(live, output.as_bytes()),
                Err("internal")
            );
        }
        assert_eq!(
            role.validate_result(live, b"{\"id\":\"\xff\"}"),
            Err("internal")
        );
        let oversized = format!("{{\"padding\":\"{}\"}}", "a".repeat(TURN_READ_REPLY_LIMIT));
        assert_eq!(
            role.validate_result(live, oversized.as_bytes()),
            Err("limit-exceeded")
        );

        let provider_snapshot = ReplyRole::ProviderSnapshot;
        assert!(provider_snapshot.effect_check().is_none());
        let codec = ProviderSnapshotRequiresLegacyRead.into();
        assert_eq!(
            provider_snapshot.failure_code(WireAuthority::AwaitingLive, &codec),
            "unavailable"
        );
        assert_eq!(
            provider_snapshot.failure_code(WireAuthority::Live, &codec),
            "legacy-read-required"
        );
        assert_eq!(
            provider_snapshot.failure_code(WireAuthority::PublicationPermitted, &codec),
            "unavailable"
        );
        for (error, code) in [
            (NativeArtifactError::Integrity, "unavailable"),
            (NativeArtifactError::Denied, "denied"),
            (NativeDataIoError::Cancelled.into(), "cancelled"),
        ] {
            assert_eq!(
                provider_snapshot.failure_code(WireAuthority::Live, &error.into()),
                code
            );
        }
        let owner_result = br#"{"providers":[{"number": NaN, "precise": 9007199254740993}]}"#;
        assert_eq!(
            provider_snapshot.validate_result(WireAuthority::Live, owner_result),
            Ok(())
        );
        assert_eq!(
            provider_snapshot.validate_result(WireAuthority::AwaitingLive, owner_result),
            Err("unavailable")
        );
        assert_eq!(
            provider_snapshot.validate_result(WireAuthority::Live, b"[]"),
            Err("internal")
        );
        assert_eq!(
            provider_snapshot.result_limit(WireAuthority::Live),
            (PROVIDER_SNAPSHOT_REPLY_LIMIT, "legacy-read-required")
        );
    }

    fn turn_start_body() -> serde_json::Value {
        serde_json::json!({
            "action": "begin",
            "turn_id": "turn:one-2",
            "conversation_id": "conversation.1",
            "conversation_revision": MAX_SAFE_JSON_REVISION,
            "input_digest": format!("sha256:{}", "a".repeat(64)),
            "source_digest": format!("sha256:{}", "b".repeat(64)),
        })
    }

    #[test]
    fn turn_start_requires_one_named_inline_operation() {
        let mut body = turn_start_body();
        let max_id = format!("A._:-{}", "0".repeat(251));
        body["turn_id"] = max_id.as_str().into();
        for (action, expected, revision) in [
            ("begin", SavedTurnStartAction::Begin, 1),
            ("claim", SavedTurnStartAction::Claim, MAX_SAFE_JSON_REVISION),
        ] {
            body["action"] = action.into();
            body["conversation_revision"] = revision.into();
            let operation = format!(r#", "turn_start":{body}"#);
            let frame = start_frame(&operation);
            let start = parse_start(START, frame.as_bytes()).unwrap();
            let NativeRegistryRequest::TurnStart {
                caller,
                action,
                turn_id,
                conversation_id,
                conversation_revision,
                input_digest,
                source_digest,
            } = start.operation().unwrap()
            else {
                panic!("expected Turn start operation");
            };
            assert_eq!(caller, SavedTurnStartCaller::SavedTurn);
            assert_eq!(action, expected);
            assert_eq!(turn_id, max_id);
            assert_eq!(conversation_id, "conversation.1");
            assert_eq!(conversation_revision, revision);
            assert_eq!(input_digest, body["input_digest"].as_str().unwrap());
            assert_eq!(source_digest, body["source_digest"].as_str().unwrap());
            assert!(parse_start(START_APPEND, frame.as_bytes()).is_err());
            for other in [PROVIDER_REQUEST, r#", "apply":{}"#, r#", "model_list":{}"#] {
                let frame = start_frame(&format!("{operation}{other}"));
                assert!(parse_start(START, frame.as_bytes())
                    .unwrap()
                    .operation()
                    .is_err());
            }
            let duplicate = start_frame(&format!("{operation}{operation}"));
            assert!(parse_start(START, duplicate.as_bytes()).is_err());
        }
    }

    #[test]
    fn turn_start_accepts_exact_explicit_callers() {
        let body = turn_start_body().to_string();
        let message = |caller_field: &str| {
            start_frame(&format!(
                r#", "turn_start":{{{caller_field},{}"#,
                &body[1..]
            ))
        };
        for (field, expected) in [
            (r#""caller":"saved-turn""#, SavedTurnStartCaller::SavedTurn),
            (r#""caller":"side-chat""#, SavedTurnStartCaller::SideChat),
            (r#""caller":"calendar""#, SavedTurnStartCaller::Calendar),
            (
                r#""caller":"calendar-manual""#,
                SavedTurnStartCaller::CalendarManual,
            ),
        ] {
            let frame = message(field);
            let start = parse_start(START, frame.as_bytes()).unwrap();
            let NativeRegistryRequest::TurnStart { caller, .. } = start.operation().unwrap() else {
                panic!("expected Turn start operation");
            };
            assert_eq!(caller, expected);
        }
        for field in [
            r#""caller":"side_chat""#,
            r#""caller":"saved-reconcile""#,
            r#""caller":"calendar_manual""#,
            r#""caller":"calendar-status""#,
            r#""caller":"Calendar""#,
            r#""caller":null"#,
            r#""caller":"side-chat","caller":"saved-turn""#,
            r#""Caller":"side-chat""#,
        ] {
            assert!(
                parse_start(START, message(field).as_bytes()).is_err(),
                "{field}"
            );
        }
    }

    #[test]
    fn turn_start_rejects_invalid_fields_and_duplicate_names() {
        use serde_json::json;

        for (key, value) in [
            ("action", json!("saved-turn")),
            ("action", json!({"begin": null})),
            ("turn_id", json!("-turn")),
            ("turn_id", json!("turn/path")),
            ("conversation_id", json!("")),
            ("conversation_id", json!("猫")),
            ("conversation_id", json!("x".repeat(257))),
            ("conversation_revision", json!(0)),
            ("conversation_revision", json!(MAX_SAFE_JSON_REVISION + 1)),
            ("conversation_revision", json!(-1)),
            ("conversation_revision", json!(true)),
            ("conversation_revision", json!(1.0)),
            ("conversation_revision", json!("1")),
            ("input_digest", json!(format!("sha256:{}", "A".repeat(64)))),
            ("source_digest", json!("sha256:abcd")),
            ("source_digest", json!(null)),
            ("publish", json!(true)),
        ] {
            let mut body = turn_start_body();
            body[key] = value;
            let frame = start_frame(&format!(r#", "turn_start":{body}"#));
            assert!(parse_start(START, frame.as_bytes()).is_err(), "{key}");
        }

        let original = turn_start_body();
        for field in original.as_object().unwrap().keys() {
            let mut missing = original.clone();
            missing.as_object_mut().unwrap().remove(field);
            let frame = start_frame(&format!(r#", "turn_start":{missing}"#));
            assert!(parse_start(START, frame.as_bytes()).is_err(), "{field}");
        }
        let compact = original.to_string();
        let duplicate = format!(r#"{{"action":"begin",{}"#, &compact[1..]);
        for body in ["null", "[]", &duplicate] {
            let frame = start_frame(&format!(r#", "turn_start":{body}"#));
            assert!(parse_start(START, frame.as_bytes()).is_err());
        }
    }

    #[test]
    fn turn_start_validates_the_raw_reply_and_claim_phase() {
        let begin = ReplyRole::TurnStart(SavedTurnStartAction::Begin);
        let claim = ReplyRole::TurnStart(SavedTurnStartAction::Claim);
        let live = WireAuthority::Live;
        let permitted = WireAuthority::PublicationPermitted;
        let unchanged = br#"{"claimed":false,"turn": { "revision":9007199254740993 }}"#;
        let claimed = br#"{"claimed":true,"turn":{"status":"running"}}"#;
        assert!(begin.permits_publication());
        assert!(claim.permits_publication());
        assert!(begin.validate_result(live, unchanged).is_ok());
        assert!(begin.validate_result(permitted, unchanged).is_ok());
        assert!(claim.validate_result(live, unchanged).is_ok());
        assert!(claim.validate_result(permitted, claimed).is_ok());
        assert_eq!(
            begin.validate_result(WireAuthority::AwaitingLive, unchanged),
            Err("unavailable")
        );
        assert_eq!(claim.validate_result(live, claimed), Err("internal"));
        assert_eq!(begin.validate_result(live, claimed), Err("internal"));
        assert_eq!(
            begin.validate_result(permitted, claimed),
            Err("unavailable")
        );
        assert_eq!(
            claim.validate_result(permitted, unchanged),
            Err("unavailable")
        );
        for malformed in [
            r#"[false,{}]"#,
            r#"{"claimed":false,"turn":null}"#,
            r#"{"claimed":false,"turn":[]}"#,
            r#"{"claimed":0,"turn":{}}"#,
            r#"{"claimed":false}"#,
            r#"{"claimed":false,"claimed":false,"turn":{}}"#,
            r#"{"claimed":false,"turn":{},"turn":{}}"#,
            r#"{"claimed":false,"turn":{},"publish":false}"#,
            r#"{"claimed":false,"turn":{}} null"#,
        ] {
            assert_eq!(
                claim.validate_result(live, malformed.as_bytes()),
                Err("internal")
            );
        }
        let parsed: TurnStartReply<'_> = serde_json::from_slice(unchanged).unwrap();
        assert_eq!(parsed.turn.get(), r#"{ "revision":9007199254740993 }"#);
        let row = format!(r#"{{"id":"{}"}}"#, "a".repeat(TURN_READ_REPLY_LIMIT - 8));
        assert_eq!(row.len(), TURN_READ_REPLY_LIMIT + 1);
        let wrapped = format!(r#"{{"claimed":false,"turn":{row}}}"#);
        assert!(wrapped.len() <= TURN_START_REPLY_LIMIT);
        assert_eq!(
            begin.validate_result(live, wrapped.as_bytes()),
            Err("limit-exceeded")
        );
        assert_eq!(
            begin.validate_result(permitted, wrapped.as_bytes()),
            Err("unavailable")
        );
    }

    #[test]
    fn turn_start_fallback_requires_live_and_the_specific_codec_error() {
        use LegacySavedTurnSettlementError as Error;

        for action in [SavedTurnStartAction::Begin, SavedTurnStartAction::Claim] {
            let role = ReplyRole::TurnStart(action);
            let codec = TurnStartRequiresLegacyCodec.into();
            let live = WireAuthority::Live;
            assert_eq!(role.failure_code(live, &codec), "legacy-read-required");
            for authority in [
                WireAuthority::AwaitingLive,
                WireAuthority::PublicationPermitted,
            ] {
                assert_eq!(role.failure_code(authority, &codec), "unavailable");
            }
            for other_legacy in [
                TurnSettlementRequiresLegacyCodec.into(),
                Error::RequiresLegacyCodec.into(),
            ] {
                assert_eq!(role.failure_code(live, &other_legacy), "unavailable");
            }
            for (error, code) in [
                (Error::InvalidRequest, "invalid"),
                (Error::InvalidRecord, "invalid"),
                (Error::NotFound, "not-found"),
                (Error::Conflict, "conflict"),
                (Error::Capacity, "limit-exceeded"),
                (Error::Revoked, "cancelled"),
                (Error::Storage, "internal"),
            ] {
                let error = error.into();
                assert_eq!(role.failure_code(live, &error), code);
                assert_eq!(
                    role.failure_code(WireAuthority::PublicationPermitted, &error),
                    "unavailable"
                );
            }
        }
    }

    #[test]
    fn turn_settlement_rejects_invalid_envelopes_and_other_operations() {
        let invalid_fields = [
            ("caller", serde_json::json!("shell")),
            ("caller", serde_json::json!("side-chat")),
            ("caller", serde_json::json!("side_chat_turn")),
            ("caller", serde_json::json!("side-chat-reconcile ")),
            ("caller", serde_json::Value::Null),
            ("turn_id", serde_json::json!("")),
            ("turn_id", serde_json::json!("-turn")),
            ("turn_id", serde_json::json!("turn/child")),
            ("turn_id", serde_json::json!("turn猫")),
            ("turn_id", serde_json::json!("a".repeat(257))),
            ("input_digest", serde_json::json!("sha256:abcd")),
            (
                "input_digest",
                serde_json::json!(format!("sha256:{}", "A".repeat(64))),
            ),
            ("expected_revision", serde_json::json!(0)),
            ("expected_revision", serde_json::json!(-1)),
            ("expected_revision", serde_json::json!(1.0)),
            ("expected_revision", serde_json::json!(true)),
            ("expected_revision", serde_json::json!("1")),
            ("result_reference", serde_json::json!({"value": 1})),
            ("result_reference", serde_json::json!("[]")),
            ("result_reference", serde_json::json!(r#"{"value": }"#)),
            ("result_reference", serde_json::json!(r#"{"value": 1}"#)),
            ("result_reference", serde_json::json!("{}\n")),
            (
                "result_reference",
                serde_json::json!(format!(r#"{{"value":"{}"}}"#, "猫".repeat(700))),
            ),
            ("publish", serde_json::json!(true)),
        ];
        for (field, value) in invalid_fields {
            let mut body = settlement_body();
            body[field] = value;
            assert!(
                serde_json::from_str::<Start<'_>>(&settlement_frame(&body)).is_err(),
                "{field}"
            );
        }
        for body in [serde_json::Value::Null, serde_json::json!([])] {
            assert!(serde_json::from_str::<Start<'_>>(&settlement_frame(&body)).is_err());
        }
        let mut body = settlement_body();
        body.as_object_mut().unwrap().remove("caller");
        assert!(serde_json::from_str::<Start<'_>>(&settlement_frame(&body)).is_err());
        let body = settlement_body().to_string();
        let settlement = format!(r#", "turn_settlement":{body}"#);
        for other in [
            PROVIDER_REQUEST,
            r#", "apply": {}"#,
            r#", "model_list": {}"#,
            r#", "model_save": {"request":{},"provider":{}}"#,
            r#", "conversation_get": {"caller":"shell","conversation_id":"stored"}"#,
            r#", "conversation_receipt": {"caller":"saved-turn","turn_id":"turn"}"#,
            r#", "model_resolve": {"role":"generate","identifier":"local"}"#,
        ] {
            let frame = start_frame(&format!("{settlement}{other}"));
            let start: Start<'_> = serde_json::from_str(&frame).unwrap();
            assert!(start.operation().is_err(), "{other}");
        }
        let frame = start_frame(&format!("{settlement}{settlement}"));
        assert!(serde_json::from_str::<Start<'_>>(&frame).is_err());
        let duplicate_caller = format!(r#"{{"caller":"saved-turn",{}"#, &body[1..]);
        let frame = start_frame(&format!(r#", "turn_settlement":{duplicate_caller}"#));
        assert!(serde_json::from_str::<Start<'_>>(&frame).is_err());
    }

    #[test]
    fn settlement_fallback_requires_live_and_cannot_follow_publication() {
        let role = ReplyRole::TurnSettlement;
        assert!(role.permits_publication());
        assert!(ReplyRole::ModelSave.permits_publication());
        for readonly in [
            ReplyRole::Provider,
            ReplyRole::ModelList,
            ReplyRole::ConversationRead,
            ReplyRole::ModelResolve,
        ] {
            assert!(!readonly.permits_publication());
        }
        let codec = TurnSettlementRequiresLegacyCodec.into();
        assert_eq!(
            role.failure_code(WireAuthority::AwaitingLive, &codec),
            "unavailable"
        );
        assert_eq!(
            role.failure_code(WireAuthority::Live, &codec),
            "legacy-read-required"
        );
        assert_eq!(
            role.failure_code(WireAuthority::PublicationPermitted, &codec),
            "unavailable"
        );
        assert_eq!(
            role.failure_code(WireAuthority::Live, &ModelRegistryRequiresLegacyRead.into()),
            "unavailable",
        );
        let conflict = LegacySavedTurnSettlementError::Conflict.into();
        assert_eq!(
            role.failure_code(WireAuthority::Live, &conflict),
            "conflict"
        );
        assert_eq!(
            role.failure_code(WireAuthority::PublicationPermitted, &conflict),
            "unavailable"
        );
        assert_eq!(
            role.result_limit(WireAuthority::Live),
            (1024 * 1024, "limit-exceeded")
        );
        assert_eq!(
            role.result_limit(WireAuthority::PublicationPermitted),
            (1024 * 1024, "unavailable")
        );
    }

    #[test]
    fn publication_requires_live_and_uses_one_permit() {
        let (mut client, server) = UnixStream::pair().unwrap();
        client.set_nonblocking(true).unwrap();
        server.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        let mut wire = Wire::new(server);
        assert!(wire
            .exchange_authority(AuthorityCheck::Publication, deadline)
            .is_err());
        let peer = thread::spawn(move || {
            assert_eq!(read_header(&mut client, deadline).unwrap(), (CHECK, 0));
            send_frame(&mut client, LIVE, &[], deadline).unwrap();
            assert_eq!(
                read_header(&mut client, deadline).unwrap(),
                (PUBLISH_CHECK, 0)
            );
            send_frame(&mut client, PUBLISH_PERMIT, &[], deadline).unwrap();
            assert_eq!(read_header(&mut client, deadline).unwrap(), (CHECK, 0));
            send_frame(&mut client, LIVE, &[], deadline).unwrap();
            client
        });
        wire.exchange_authority(AuthorityCheck::Live, deadline)
            .unwrap();
        wire.exchange_authority(AuthorityCheck::Publication, deadline)
            .unwrap();
        wire.exchange_authority(AuthorityCheck::Live, deadline)
            .unwrap();
        let _peer = peer.join().unwrap();
        assert!(wire
            .exchange_authority(AuthorityCheck::Publication, deadline)
            .is_err());
    }

    #[test]
    fn publication_cannot_be_authorized_by_a_live_response() {
        let (mut client, server) = UnixStream::pair().unwrap();
        client.set_nonblocking(true).unwrap();
        server.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        let mut wire = Wire::new(server);
        let peer = thread::spawn(move || {
            assert_eq!(read_header(&mut client, deadline).unwrap(), (CHECK, 0));
            send_frame(&mut client, LIVE, &[], deadline).unwrap();
            assert_eq!(
                read_header(&mut client, deadline).unwrap(),
                (PUBLISH_CHECK, 0),
            );
            send_frame(&mut client, LIVE, &[], deadline).unwrap();
            client
        });
        wire.exchange_authority(AuthorityCheck::Live, deadline)
            .unwrap();
        assert!(wire
            .exchange_authority(AuthorityCheck::Publication, deadline)
            .is_err());
        assert!(wire.authority == WireAuthority::Live);
        peer.join().unwrap();
    }

    #[test]
    fn idle_connection_distinguishes_explicit_cancel_and_disconnect() {
        let (mut sender, receiver) = UnixStream::pair().unwrap();
        sender.set_nonblocking(true).unwrap();
        receiver.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        let mut wire = Wire::new(receiver);
        wire.check_idle(deadline).unwrap();
        send_frame(&mut sender, CANCEL, &[], deadline).unwrap();
        assert!(wire.check_idle(deadline).is_err());
        assert!(matches!(wire.stop, Some(Stop::Cancelled)));
        let (sender, stream) = UnixStream::pair().unwrap();
        stream.set_nonblocking(true).unwrap();
        let mut wire = Wire::new(stream);
        drop(sender);
        assert!(wire.check_idle(deadline).is_err());
        assert!(matches!(wire.stop, Some(Stop::Broken)));
    }

    #[test]
    fn incomplete_frame_cannot_extend_its_absolute_deadline() {
        let (mut sender, mut receiver) = UnixStream::pair().unwrap();
        receiver.set_nonblocking(true).unwrap();
        sender.write_all(&[CHECK, 0, 0]).unwrap();
        let error =
            read_header(&mut receiver, Instant::now() + Duration::from_millis(10)).unwrap_err();
        assert_eq!(
            error.downcast_ref::<io::Error>().unwrap().kind(),
            io::ErrorKind::TimedOut
        );
    }

    #[test]
    fn host_deadline_must_be_future_and_bounded() {
        let uptime = uptime_ns().unwrap();
        assert!(host_deadline(uptime).is_err());
        assert!(host_deadline(u64::MAX).is_err());
        let anchor = Instant::now();
        let accepted = host_deadline(uptime + 30_000_000_000).unwrap();
        assert!(accepted > anchor);
        assert!(accepted <= Instant::now() + Duration::from_secs(30));
    }
}
