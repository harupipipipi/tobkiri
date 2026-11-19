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
use tobkiri_harness_native::LegacySavedTurnSettlementError;
use tobkiri_wasm::NativeDataIoError;

use crate::{
    defaultspack_manager::{ApplicationProcessManager, ProviderPrepareListener},
    host_contract::ExecutionProfileIdentity,
    native_harness_bootstrap::{
        start_provider_prepare, ConversationConfirmationRequiresLegacyRead, ConversationReadCaller,
        ConversationRegistryRequiresLegacyRead, ModelRegistryRequiresLegacyRead,
        NativeRegistryRequest, OriginalModelResolveRole, OriginalSavedReceiptRole,
        PendingProviderPrepare, ProviderPrepareDeadline, SettlementCaller,
        TurnSettlementRequiresLegacyCodec, CONVERSATION_CONFIRMATION_LIMIT,
        CONVERSATION_READ_REPLY_LIMIT, MODEL_RESOLVE_REPLY_LIMIT,
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
const JSON_LIMIT: usize = 16 * 1024;
const MODEL_RESULT_LIMIT: usize = 1024 * 1024;
const SAVED_RECEIPT_TURN_ID_MAX_CHARS: usize = 2048;
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
    conversation_receipt: Option<ConversationReceipt>,
    #[serde(borrow, default, deserialize_with = "deserialize_present")]
    conversation_confirmation: Option<ConversationConfirmation<'a>>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    model_resolve: Option<ModelResolve>,
    #[serde(default, deserialize_with = "deserialize_present_object")]
    turn_settlement: Option<TurnSettlement>,
}

impl Start<'_> {
    fn operation(&self) -> Result<NativeRegistryRequest> {
        match (
            &self.request,
            &self.model_list,
            &self.model_save,
            &self.conversation_get,
            &self.conversation_receipt,
            &self.conversation_confirmation,
            &self.model_resolve,
            &self.turn_settlement,
            self.apply,
        ) {
            (Some(request), None, None, None, None, None, None, None, apply) => {
                let request = serde_json::to_vec(request)?;
                ensure!(
                    request.len() <= JSON_LIMIT,
                    "Provider request exceeds its envelope"
                );
                // Preserve the frozen plan's exact JSON for the Core parser.
                let apply = apply.map(|plan| plan.get().as_bytes().to_vec());
                Ok(NativeRegistryRequest::Provider { request, apply })
            }
            (None, Some(_), None, None, None, None, None, None, None) => {
                Ok(NativeRegistryRequest::ModelList)
            }
            (None, None, Some(save), None, None, None, None, None, None) => {
                Ok(NativeRegistryRequest::ModelSave {
                    request: save.request.get().as_bytes().to_vec(),
                    provider: save.provider.get().as_bytes().to_vec(),
                })
            }
            (None, None, None, Some(get), None, None, None, None, None) => {
                Ok(NativeRegistryRequest::ConversationGet {
                    caller: get.caller,
                    conversation_id: get.conversation_id.clone(),
                })
            }
            (None, None, None, None, Some(receipt), None, None, None, None) => {
                Ok(NativeRegistryRequest::ConversationReceipt {
                    caller: receipt.caller,
                    turn_id: receipt.turn_id.clone(),
                })
            }
            (None, None, None, None, None, Some(confirmation), None, None, None) => {
                confirmation.operation(&self.identity.profile_id)
            }
            (None, None, None, None, None, None, Some(resolve), None, None) => {
                Ok(NativeRegistryRequest::ModelResolve {
                    role: resolve.role,
                    identifier: resolve.identifier.clone(),
                })
            }
            (None, None, None, None, None, None, None, Some(settlement), None) => {
                ensure!(
                    !matches!(settlement.caller, SettlementCaller::SavedReconcile)
                        || settlement.expected_revision.is_some(),
                    "Turn settlement reconciliation requires an expected revision"
                );
                Ok(NativeRegistryRequest::TurnSettlement {
                    caller: settlement.caller,
                    turn_id: settlement.turn_id.clone(),
                    input_digest: settlement.input_digest.clone(),
                    expected_revision: settlement.expected_revision,
                    result_reference: settlement.result_reference.clone(),
                })
            }
            _ => bail!("native Registry start must name exactly one operation"),
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelList {}

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
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConversationReceipt {
    #[serde(deserialize_with = "deserialize_saved_receipt_caller")]
    caller: OriginalSavedReceiptRole,
    #[serde(deserialize_with = "deserialize_saved_receipt_turn_id")]
    turn_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConversationConfirmation<'a> {
    #[serde(deserialize_with = "deserialize_saved_receipt_caller")]
    caller: OriginalSavedReceiptRole,
    #[serde(borrow, deserialize_with = "deserialize_object")]
    terminal: &'a RawValue,
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
    #[serde(deserialize_with = "deserialize_settlement_digest")]
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

fn deserialize_settlement_digest<'de, D>(deserializer: D) -> std::result::Result<String, D::Error>
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

fn deserialize_model_resolve_role<'de, D>(
    deserializer: D,
) -> std::result::Result<OriginalModelResolveRole, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "quote-generate" => Ok(OriginalModelResolveRole::QuoteGenerate),
        "quote-stream" => Ok(OriginalModelResolveRole::QuoteStream),
        "generate" => Ok(OriginalModelResolveRole::Generate),
        "stream" => Ok(OriginalModelResolveRole::Stream),
        _ => Err(de::Error::custom("unknown Model Resolve role")),
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

fn deserialize_saved_receipt_caller<'de, D>(
    deserializer: D,
) -> std::result::Result<OriginalSavedReceiptRole, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "saved-turn" => Ok(OriginalSavedReceiptRole::SavedTurn),
        "saved-reconcile" => Ok(OriginalSavedReceiptRole::SavedReconcile),
        _ => Err(de::Error::custom("unknown saved receipt caller")),
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
    Unavailable,
    Broken,
}

impl Stop {
    fn as_error(self) -> anyhow::Error {
        let error = match self {
            Self::Cancelled => NativeDataIoError::Cancelled,
            Self::Deadline => NativeDataIoError::Deadline,
            Self::Unavailable => NativeDataIoError::Unavailable,
            Self::Broken => NativeDataIoError::Io,
        };
        NativeArtifactError::from(error).into()
    }

    fn reply(self) -> Option<&'static str> {
        match self {
            Self::Cancelled | Self::Deadline => Some("cancelled"),
            Self::Unavailable => Some("unavailable"),
            Self::Broken => None,
        }
    }
}

/// Handle one accepted macOS connection for the currently managed Host process.
pub(crate) fn handle_connection(app: &AppHandle, mut stream: UnixStream) -> Result<()> {
    let peer = authenticate_peer(&stream)?;
    suppress_sigpipe(&stream)?;
    stream.set_nonblocking(true)?;
    let start_deadline = Instant::now() + FRAME_TIMEOUT;
    let (tag, size) = read_header(&mut stream, start_deadline)?;
    ensure!(
        tag == START && (1..=JSON_LIMIT).contains(&size),
        "invalid Provider prepare start frame"
    );
    let mut bytes = vec![0; size];
    read_exact(&mut stream, &mut bytes, start_deadline)?;
    let start: Start<'_> =
        serde_json::from_slice(&bytes).map_err(|_| anyhow!("invalid Provider prepare start"))?;
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
    let operation = start.operation()?;
    let needs_active_host = matches!(
        &operation,
        NativeRegistryRequest::TurnSettlement { .. }
            | NativeRegistryRequest::ConversationConfirmation { .. }
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
        // An absent or stale admission cannot authorize a completion fallback.
        let (tag, payload): (u8, &[u8]) = if needs_active_host {
            (FAILURE, b"unavailable")
        } else {
            (INAPPLICABLE, &[])
        };
        return send_frame(
            &mut stream,
            tag,
            payload,
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        );
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
        return send_frame(
            &mut stream,
            FAILURE,
            b"unavailable",
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        );
    }
    manager.require_provider_prepare_admission(&candidate)?;
    if !matches_runtime || !operation.has_launch_proof(candidate.verified_source()) {
        return send_frame(
            &mut stream,
            INAPPLICABLE,
            &[],
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        );
    }

    let reply = match &operation {
        NativeRegistryRequest::Provider { .. } => ReplyRole::Provider,
        NativeRegistryRequest::ModelList => ReplyRole::ModelList,
        NativeRegistryRequest::ModelSave { .. } => ReplyRole::ModelSave,
        NativeRegistryRequest::ConversationGet { .. }
        | NativeRegistryRequest::ConversationReceipt { .. } => ReplyRole::ConversationRead,
        NativeRegistryRequest::ConversationConfirmation { .. } => {
            ReplyRole::ConversationConfirmation
        }
        NativeRegistryRequest::ModelResolve { .. } => ReplyRole::ModelResolve,
        NativeRegistryRequest::TurnSettlement { .. } => ReplyRole::TurnSettlement,
    };
    drop(bytes);
    let deadline = Arc::new(ProviderPrepareDeadline::new(deadline));
    let connection = Arc::new(Connection {
        manager,
        candidate: Arc::clone(&candidate),
        wire: Mutex::new(Wire::new(stream)),
        deadline: Arc::clone(&deadline),
        reply,
    });
    let check_connection = Arc::clone(&connection);
    let publish_permit = reply.permits_publication().then(|| {
        let connection = Arc::clone(&connection);
        Arc::new(move || connection.authorize_publication())
            as Arc<dyn Fn() -> Result<()> + Send + Sync>
    });
    let pending = match start_provider_prepare(
        app,
        candidate,
        operation,
        deadline,
        Arc::new(move || check_connection.check_current()),
        publish_permit,
    ) {
        Ok(pending) => pending,
        Err(error) => {
            return connection.send_failure(
                reply.failure_code(WireAuthority::AwaitingLive, &error),
                connection.deadline.current(),
            )
        }
    };
    connection.finish(pending)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ReplyRole {
    Provider,
    ModelList,
    ModelSave,
    ConversationRead,
    ConversationConfirmation,
    ModelResolve,
    TurnSettlement,
}

impl ReplyRole {
    fn permits_publication(self) -> bool {
        matches!(
            self,
            Self::ModelSave | Self::ConversationConfirmation | Self::TurnSettlement
        )
    }

    fn result_limit(self, authority: WireAuthority) -> (usize, &'static str) {
        match self {
            Self::Provider => (JSON_LIMIT, "limit-exceeded"),
            Self::ModelList => (MODEL_RESULT_LIMIT, "legacy-read-required"),
            Self::ModelSave => (MODEL_RESULT_LIMIT, "limit-exceeded"),
            Self::ConversationRead => (CONVERSATION_READ_REPLY_LIMIT, "legacy-read-required"),
            Self::ConversationConfirmation => (
                CONVERSATION_CONFIRMATION_LIMIT,
                conversation_confirmation_fallback(authority),
            ),
            Self::ModelResolve => (MODEL_RESOLVE_REPLY_LIMIT, "legacy-read-required"),
            Self::TurnSettlement if authority == WireAuthority::PublicationPermitted => {
                (TURN_SETTLEMENT_RESULT_LIMIT, "unavailable")
            }
            Self::TurnSettlement => (TURN_SETTLEMENT_RESULT_LIMIT, "limit-exceeded"),
        }
    }

    fn failure_code(self, authority: WireAuthority, error: &anyhow::Error) -> &'static str {
        if self == Self::ConversationConfirmation {
            if authority == WireAuthority::PublicationPermitted {
                return "publication-unconfirmed";
            }
            if error.is::<ConversationConfirmationRequiresLegacyRead>()
                || matches!(
                    error.downcast_ref::<NativeArtifactError>(),
                    Some(NativeArtifactError::ConversationLimitBeforePublish)
                )
            {
                return conversation_confirmation_fallback(authority);
            }
            return match error.downcast_ref::<NativeArtifactError>() {
                Some(NativeArtifactError::ConversationPublicationUnconfirmed(_)) => {
                    "publication-unconfirmed"
                }
                _ => failure_code(error),
            };
        }
        if self != Self::TurnSettlement {
            return failure_code(error);
        }
        if authority == WireAuthority::PublicationPermitted {
            return "unavailable";
        }
        if authority == WireAuthority::Live && error.is::<TurnSettlementRequiresLegacyCodec>() {
            return "legacy-read-required";
        }
        use LegacySavedTurnSettlementError as SettlementError;
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
}

fn conversation_confirmation_fallback(authority: WireAuthority) -> &'static str {
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
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum WireAuthority {
    AwaitingLive,
    Live,
    PublicationPermitted,
}

impl Connection {
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
        self.authorize(false)
    }

    fn authorize_publication(&self) -> Result<()> {
        ensure!(
            self.reply.permits_publication(),
            "Registry exchange does not admit publication"
        );
        self.authorize(true)
    }

    fn authorize(&self, publish: bool) -> Result<()> {
        let mut wire = self.lock_wire()?;
        self.checkpoint(&mut wire)?;
        let deadline = self.deadline.current().min(Instant::now() + FRAME_TIMEOUT);
        let result = wire.exchange_authority(publish, deadline);
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
        let (limit, overflow_code) = self.reply.result_limit(wire.authority);
        match result {
            Ok(output) if output.len() <= limit => {
                send_frame(&mut wire.stream, PLAN, output.as_bytes(), deadline)
            }
            Ok(_) => send_frame(
                &mut wire.stream,
                FAILURE,
                overflow_code.as_bytes(),
                deadline,
            ),
            Err(error) => {
                let code = self.reply.failure_code(wire.authority, &error);
                send_frame(&mut wire.stream, FAILURE, code.as_bytes(), deadline)
            }
        }
    }

    fn send_failure(&self, code: &str, deadline: Instant) -> Result<()> {
        let mut wire = self.lock_wire()?;
        let code = if self.reply == ReplyRole::ConversationConfirmation
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
        }
    }

    fn exchange_authority(&mut self, publish: bool, deadline: Instant) -> Result<()> {
        self.check_idle(deadline)?;
        let (challenge, expected) = if publish {
            ensure!(
                self.authority == WireAuthority::Live,
                "Registry publication is outside its live phase"
            );
            (PUBLISH_CHECK, PUBLISH_PERMIT)
        } else {
            (CHECK, LIVE)
        };
        send_frame(&mut self.stream, challenge, &[], deadline)?;
        let header = read_header(&mut self.stream, deadline)?;
        if header == (CANCEL, 0) {
            self.stop = Some(Stop::Cancelled);
            return Err(Stop::Cancelled.as_error());
        }
        ensure!(
            header == (expected, 0),
            "invalid Registry authorization frame"
        );
        if publish {
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

    #[test]
    fn confirmation_start_preserves_the_exact_terminal_json_for_both_callers() {
        let terminal = serde_json::to_string_pretty(&confirmation_terminal()).unwrap();
        for (wire_role, expected) in [
            ("saved-turn", OriginalSavedReceiptRole::SavedTurn),
            ("saved-reconcile", OriginalSavedReceiptRole::SavedReconcile),
        ] {
            let operation = confirmation_operation(wire_role, &terminal);
            let message = start_frame(&operation);
            let start: Start<'_> = serde_json::from_str(&message).unwrap();
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
            confirmation_operation("saved-turn", "null"),
            r#", "conversation_confirmation":null"#.to_owned(),
        ] {
            assert!(serde_json::from_str::<Start<'_>>(&start_frame(&operation)).is_err());
        }
    }

    #[test]
    fn confirmation_fallback_requires_live_and_cannot_follow_publication() {
        let role = ReplyRole::ConversationConfirmation;
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
                ConversationConfirmationRequiresLegacyRead.into(),
                NativeArtifactError::ConversationLimitBeforePublish.into(),
            ] {
                assert_eq!(role.failure_code(authority, &error), code);
            }
            assert_eq!(role.result_limit(authority), (16 * 1024, code));
        }
        let denied = NativeArtifactError::Denied.into();
        assert_eq!(role.failure_code(WireAuthority::Live, &denied), "denied");
        assert_eq!(
            role.failure_code(WireAuthority::PublicationPermitted, &denied),
            "publication-unconfirmed"
        );
    }

    #[test]
    fn start_distinguishes_prepare_and_preserves_frozen_apply_bytes() {
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
    fn conversation_get_cannot_share_a_start_with_another_operation() {
        let get = r#", "conversation_get":{"caller":"shell","conversation_id":"stored"}"#;
        for extra in [
            PROVIDER_REQUEST,
            r#", "apply": {}"#,
            r#", "model_list": {}"#,
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
    fn saved_receipt_preserves_the_raw_bounded_turn_id_for_either_caller() {
        for (wire_caller, expected_caller, id) in [
            ("saved-turn", OriginalSavedReceiptRole::SavedTurn, ""),
            (
                "saved-reconcile",
                OriginalSavedReceiptRole::SavedReconcile,
                "\u{001c}  保存🧾-turn\t",
            ),
            (
                "saved-turn",
                OriginalSavedReceiptRole::SavedTurn,
                &"🧾".repeat(SAVED_RECEIPT_TURN_ID_MAX_CHARS),
            ),
        ] {
            let encoded_id = serde_json::to_string(id).unwrap();
            let frame = start_frame(&format!(
                r#", "conversation_receipt":{{"caller":"{wire_caller}","turn_id":{encoded_id}}}"#
            ));
            assert!(frame.len() <= JSON_LIMIT);
            let start: Start<'_> = serde_json::from_str(&frame).unwrap();
            let NativeRegistryRequest::ConversationReceipt { caller, turn_id } =
                start.operation().unwrap()
            else {
                panic!("expected saved receipt lookup");
            };
            assert_eq!(caller, expected_caller);
            assert_eq!(turn_id, id);
        }

        let oversized = "🧾".repeat(SAVED_RECEIPT_TURN_ID_MAX_CHARS + 1);
        let frame = start_frame(&format!(
            r#", "conversation_receipt":{{"caller":"saved-turn","turn_id":"{oversized}"}}"#
        ));
        assert!(frame.len() <= JSON_LIMIT);
        assert!(serde_json::from_str::<Start<'_>>(&frame).is_err());
    }

    #[test]
    fn saved_receipt_rejects_other_operations_and_unrecognized_fields() {
        for body in [
            r#"null"#,
            r#"["saved-turn","turn-id"]"#,
            r#"{"caller":"shell","turn_id":"stored"}"#,
            r#"{"caller":"saved-orchestration","turn_id":"stored"}"#,
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

        let mut body = settlement_body();
        body["caller"] = "saved-reconcile".into();
        let frame = settlement_frame(&body);
        let start: Start<'_> = serde_json::from_str(&frame).unwrap();
        assert!(start.operation().is_err());
        body.as_object_mut().unwrap().remove("expected_revision");
        assert!(serde_json::from_str::<Start<'_>>(&settlement_frame(&body)).is_err());
    }

    #[test]
    fn turn_settlement_rejects_invalid_envelopes_and_other_operations() {
        let invalid_fields = [
            ("caller", serde_json::json!("shell")),
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
        assert!(wire.exchange_authority(true, deadline).is_err());
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
        wire.exchange_authority(false, deadline).unwrap();
        wire.exchange_authority(true, deadline).unwrap();
        wire.exchange_authority(false, deadline).unwrap();
        let _peer = peer.join().unwrap();
        assert!(wire.exchange_authority(true, deadline).is_err());
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
        wire.exchange_authority(false, deadline).unwrap();
        assert!(wire.exchange_authority(true, deadline).is_err());
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
