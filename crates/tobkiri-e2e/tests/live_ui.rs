//! Opt-in live-provider regression for the actual received-UI dynamic Flow entry.
//! This exercises terminal-input.submit -> guest run-flow, never NativeInput.
//! WebView rendering and native IPC acceptance remain separate proofs.
mod support;

use anyhow::Result;
use std::{cell::Cell, future::Future, path::PathBuf, sync::Arc, time::Duration};
use support::*;
use tobkiri_canon::{
    CanonValue, Digest, Variant,
    ids::{InputSourceId, OpName, ProfileId, ShapeRef},
};
use tobkiri_core::{Core, CoreConfig, CoreError, Session, UiAttachment};
use tobkiri_launcher_core_host::{
    HostCaller, HostError as LauncherHostError, LauncherCore, TransportTickets,
    UiOperationRegistration, UiRoute, UiTransportResponse,
};
use tobkiri_profile::Profile;

const INPUT: &str = "Write one sentence of at least twelve words about a quiet forest.";
const CONVERSATION: &str = "e2e";
const INPUT_SHAPE: &str = "local.harness-chat.user-input@1.0";
const STORE_SHAPE: &str = "local.harness-chat.conversation-store@1.0";
const OUTPUT_SHAPE: &str = "local.harness-chat.assistant-output@1.0";
const OPERATION_TIMEOUT: Duration = Duration::from_secs(30);

// Every failure reported by this test contains only a static stage/step and a
// closed code. In particular, never attach an underlying error with Context:
// guest messages, arguments, SQL, source paths and credentials are not receipts.
fn failure(step: &'static str, code: &'static str) -> anyhow::Error {
    anyhow::anyhow!("live-ui step={step} code={code}")
}

fn require(condition: bool, step: &'static str, code: &'static str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(failure(step, code))
    }
}

fn core_code(error: &CoreError) -> &'static str {
    match error {
        CoreError::Cleanup { original, .. } => core_code(original),
        CoreError::Gate(error) => error.code(),
        CoreError::Step(error) => error.audit_code(),
        CoreError::Invalid(_) | CoreError::Json(_) | CoreError::Canon(_) => "invalid",
        CoreError::NotFound(_) => "not-found",
        CoreError::Conflicts(_) => "conflict",
        _ => "unavailable",
    }
}

fn core_failure(step: &'static str, error: CoreError) -> anyhow::Error {
    if let CoreError::Cleanup { original, cleanup } = error {
        anyhow::anyhow!(
            "live-ui step={step} code={}; live-ui step=cleanup code={}",
            core_code(&original),
            core_code(&cleanup)
        )
    } else {
        failure(step, core_code(&error))
    }
}

fn flow_step(value: Option<&str>) -> &'static str {
    match value {
        Some("load") => "load",
        Some("prompt") => "prompt",
        Some("generate") => "generate",
        Some("save") => "save",
        _ => "flow",
    }
}

fn flow_code(value: Option<&str>) -> &'static str {
    match value {
        Some("denied") => "denied",
        Some("approval-required") => "approval-required",
        Some("cancelled") => "cancelled",
        Some("deadline") => "deadline",
        Some("not-found") => "not-found",
        Some("invalid") => "invalid",
        Some("limit-exceeded") => "limit-exceeded",
        Some("conflict") => "conflict",
        Some("unavailable") => "unavailable",
        Some("trap") => "trap",
        Some("blocked") => "blocked",
        _ => "internal",
    }
}

fn live_fixture() -> Result<Fixture> {
    require(
        std::env::var("TOBKIRI_TEST_LLAMA").as_deref() == Ok("1"),
        "fixture",
        "invalid",
    )?;
    // Root supplies the immutable native live+UI structural definition. Read
    // this document and its verified Pack sources, never its existing data root.
    let path = std::env::var_os("TOBKIRI_TEST_LIVE_UI_PROFILE")
        .map(PathBuf::from)
        .ok_or_else(|| failure("fixture", "invalid"))?;
    let bytes = std::fs::read(path).map_err(|_| failure("fixture", "unavailable"))?;
    let mut profile: Profile =
        serde_json::from_slice(&bytes).map_err(|_| failure("fixture", "invalid"))?;
    let expected = [
        "local.harness-chat",
        "local.terminal-input",
        "local.harness-conversation-store",
        "local.harness-prompt",
        "local.openai-compat-provider",
        "local.terminal-output",
        "local.harness-ui",
    ];
    require(
        profile.packs.len() == expected.len()
            && expected.iter().all(|id| {
                profile
                    .packs
                    .iter()
                    .filter(|pin| pin.id.as_str() == *id)
                    .count()
                    == 1
            })
            && profile.startup.ui.as_ref().map(|id| id.as_str()) == Some("local.harness-ui")
            && profile
                .bindings
                .get("local.harness-chat.llm-provider@1")
                .map(|id| id.as_str())
                == Some("local.openai-compat-provider"),
        "fixture",
        "invalid",
    )?;
    let [entry] = profile.startup.inputs.as_slice() else {
        return Err(failure("fixture", "invalid"));
    };
    require(
        entry.source_pack.as_str() == "local.terminal-input"
            && entry.exact_input_view.to_string() == INPUT_SHAPE
            && entry.operation.as_str() == "read",
        "fixture",
        "invalid",
    )?;
    let input = InputSourceId::parse(entry.source_pack.as_str())
        .map_err(|_| failure("fixture", "invalid"))?;
    let shape = entry.exact_input_view.clone();
    let input_operation = entry.operation.clone();
    let temp = physical_temp().map_err(|_| failure("fixture", "unavailable"))?;
    // Same isolation as the existing real-1B test: change only the instance ID
    // for the real OS-backed Vault ACL. Pins, sources, bindings and every limit
    // and permission remain exactly those of the supplied structural definition.
    let identity = Digest::hash(temp.path().to_string_lossy().as_bytes());
    profile.id = ProfileId::new(format!("prof_live_ui_{}", &identity.hex()[..16]))
        .map_err(|_| failure("fixture", "invalid"))?;
    let host = Arc::new(Host::default());
    *host
        .ui_enabled
        .lock()
        .map_err(|_| failure("fixture", "unavailable"))? = true;
    Ok(Fixture {
        temp,
        profile,
        host,
        shape,
        input,
        input_operation,
    })
}

fn key_file() -> Result<Vec<u8>> {
    // The same explicit Root-runtime credential path and 0600/trim contract as
    // llama.rs. The bytes enter only through the ordinary Core.set_secret API.
    let path = std::env::var_os("TOBKIRI_TEST_LLAMA_KEY_FILE")
        .map(PathBuf::from)
        .ok_or_else(|| failure("credential", "invalid"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(&path)
            .map_err(|_| failure("credential", "unavailable"))?
            .permissions()
            .mode();
        require(mode & 0o777 == 0o600, "credential", "invalid")?;
    }
    let mut bytes = std::fs::read(path).map_err(|_| failure("credential", "unavailable"))?;
    while bytes.last().is_some_and(|byte| byte.is_ascii_whitespace()) {
        bytes.pop();
    }
    require(!bytes.is_empty(), "credential", "invalid")?;
    Ok(bytes)
}

/// On timeout keep the SAME operation future pinned while requesting close,
/// as in support::within_session. No retry or replacement operation is created.
async fn session_operation<T>(
    session: &Session,
    step: &'static str,
    deadline: tokio::time::Instant,
    future: impl Future<Output = std::result::Result<T, CoreError>>,
) -> Result<T> {
    let mut future = std::pin::pin!(future);
    match tokio::time::timeout_at(deadline, &mut future).await {
        Ok(result) => result.map_err(|error| core_failure(step, error)),
        Err(_) => {
            // A pending close is a failing test, never clean-stop evidence.
            if let Ok(Err(error)) =
                tokio::time::timeout(Duration::from_secs(5), session.close()).await
            {
                eprintln!("live-ui step=cleanup code={}", core_code(&error));
            }
            Err(failure(step, "deadline"))
        }
    }
}

/// Trusted headless host routing claims. Actual Core source identity, host
/// session and execution context still come from the production attachment
/// lookup inside LauncherCore; this is never a WebView/iframe origin proof.
fn host_caller() -> HostCaller<'static> {
    HostCaller {
        label: "core-host",
        url: "tauri://localhost/",
    }
}

/// Closed LauncherCore diagnostics mirroring the core_host.rs UiError mapping.
/// Rejected codes pass through the same closed whitelist as Flow failures.
fn host_code(error: &LauncherHostError) -> &'static str {
    match error {
        LauncherHostError::Caller | LauncherHostError::Core => "denied",
        LauncherHostError::NoSession => "closed",
        LauncherHostError::AssetPath | LauncherHostError::Invalid => "invalid",
        LauncherHostError::Limit => "limit-exceeded",
        LauncherHostError::RegistrationFull => "registration-full",
        LauncherHostError::Duplicate => "duplicate",
        LauncherHostError::Cancelled => "cancelled",
        LauncherHostError::Rejected(code) => flow_code(Some(*code)),
    }
}

/// On timeout keep the SAME facade operation future pinned and poll it
/// together with an ordinary stop: LauncherCore::start holds the startup mutex
/// that stop awaits after next_lifecycle, so retaining an unpolled start while
/// awaiting stop would self-block. Pending cleanup is never a clean stop.
async fn facade_cleanup<T>(
    launcher: &LauncherCore,
    future: impl Future<Output = std::result::Result<T, LauncherHostError>>,
) {
    let bounded = async move {
        let _ = tokio::join!(future, launcher.stop(host_caller()));
    };
    if tokio::time::timeout(Duration::from_secs(5), bounded)
        .await
        .is_err()
    {
        eprintln!("live-ui step=cleanup code=deadline");
    }
}

/// Bound one facade operation by its absolute deadline; on timeout the pinned
/// future and an ordinary stop are both polled for five more seconds.
async fn facade_operation<T>(
    launcher: &LauncherCore,
    step: &'static str,
    deadline: tokio::time::Instant,
    future: impl Future<Output = std::result::Result<T, LauncherHostError>>,
) -> Result<T> {
    let mut future = std::pin::pin!(future);
    match tokio::time::timeout_at(deadline, &mut future).await {
        Ok(result) => result.map_err(|error| failure(step, host_code(&error))),
        Err(_) => {
            facade_cleanup(launcher, future).await;
            Err(failure(step, "deadline"))
        }
    }
}

struct ReceivedUi<'a> {
    session: &'a Session,
    attachment: &'a UiAttachment,
    generation: u64,
}

impl ReceivedUi<'_> {
    async fn call(&self, shape: &str, op: &str, args: &CanonValue) -> Result<CanonValue> {
        let shape = ShapeRef::parse(shape).map_err(|_| failure("invoke", "invalid"))?;
        let op = OpName::parse(op).map_err(|_| failure("invoke", "invalid"))?;
        let buffer = session_operation(
            self.session,
            "invoke",
            tokio::time::Instant::now() + OPERATION_TIMEOUT,
            self.session.ui_invoke_received(
                &self.attachment.session,
                &self.attachment.host_session,
                self.generation,
                &self.attachment.receiver,
                shape,
                op,
                args,
            ),
        )
        .await?;
        parse_output(&buffer).map_err(|_| failure("invoke", "invalid"))
    }

    async fn pull(
        &self,
        step: &'static str,
        deadline: tokio::time::Instant,
        handle: u64,
        max: u32,
    ) -> Result<CanonValue> {
        let buffer = session_operation(
            self.session,
            step,
            deadline,
            self.session
                .ui_stream_pull_buffer(&self.attachment.session, handle, max),
        )
        .await?;
        parse_output(&buffer).map_err(|_| failure(step, "invalid"))
    }

    /// EOF consumed the actual UI source; a subsequent handle reuse through
    /// the real pull path must fail.
    async fn require_retired(&self, handle: u64) -> Result<()> {
        require(
            session_operation(
                self.session,
                "retired",
                tokio::time::Instant::now() + OPERATION_TIMEOUT,
                self.session
                    .ui_stream_pull_buffer(&self.attachment.session, handle, 1),
            )
            .await
            .is_err(),
            "retired",
            "invalid",
        )
    }
}

/// Production LauncherCore facade backend. Every operation performs the real
/// registration handshake before dispatch, uses the internally spawned native
/// task through invoke/stream_pull, validates each minted response against the
/// bound route and a strictly increasing safe ticket ID, then performs the
/// real acknowledge. The request counter starts at one for the actual frame.
struct FacadeUi<'a> {
    launcher: &'a LauncherCore,
    namespace: &'a str,
    generation: u64,
    receiver: &'a str,
    next_id: Cell<u64>,
    last_ticket: Cell<u64>,
    acks: Cell<usize>,
}

impl FacadeUi<'_> {
    fn route(&self) -> UiRoute<'_> {
        UiRoute {
            namespace: self.namespace,
            generation: self.generation,
            receiver: self.receiver,
        }
    }

    fn take_id(&self) -> Result<u64> {
        let id = self.next_id.get();
        require(id <= 9_007_199_254_740_991, "register", "invalid")?;
        self.next_id.set(id + 1);
        Ok(id)
    }

    /// The real registration handshake precedes every dispatch. Its private
    /// fields are checked through the serialized six-field ACK object with a
    /// boolean compare and a static failure, never a value-dumping assert.
    fn register(
        &self,
        stage: &'static str,
        id: u64,
        kind: &'static str,
        handle: Option<u64>,
    ) -> Result<()> {
        let ack = self
            .launcher
            .register_ui_operation(host_caller(), self.route(), id, kind, handle)
            .map_err(|error| failure(stage, host_code(&error)))?;
        self.check_registration_ack(stage, id, kind, handle, &ack)
    }

    fn check_registration_ack(
        &self,
        stage: &'static str,
        id: u64,
        kind: &'static str,
        handle: Option<u64>,
        ack: &UiOperationRegistration,
    ) -> Result<()> {
        let actual = serde_json::to_value(ack).map_err(|_| failure(stage, "invalid"))?;
        let expected = serde_json::json!({
            "namespace": self.namespace,
            "generation": self.generation,
            "receiver": self.receiver,
            "id": id,
            "kind": kind,
            "handle": handle,
        });
        require(actual == expected, stage, "invalid")
    }

    /// Validate a minted response against the bound route and monotone ticket,
    /// parse its StrictV1 payload and always attempt the real acknowledge so
    /// a parse failure cannot strand its ticket silently.
    fn response(
        &self,
        step: &'static str,
        result: Result<UiTransportResponse>,
    ) -> Result<CanonValue> {
        let response = result?;
        require(
            response.namespace == self.namespace
                && response.generation == self.generation
                && response.receiver.as_str() == self.receiver
                && response.ticket_id > self.last_ticket.get()
                && response.ticket_id <= 9_007_199_254_740_991,
            step,
            "invalid",
        )?;
        self.last_ticket.set(response.ticket_id);
        let parsed = tobkiri_canon::parse(response.payload.as_bytes(), Variant::StrictV1)
            .map_err(|_| failure(step, "invalid"));
        let ack = self
            .launcher
            .acknowledge(
                host_caller(),
                response.namespace.as_str(),
                response.generation,
                response.receiver.as_str(),
                response.ticket_id,
            )
            .map_err(|error| failure("ack", host_code(&error)));
        match (parsed, ack) {
            (Ok(value), Ok(())) => {
                self.acks.set(self.acks.get() + 1);
                Ok(value)
            }
            (Err(error), Ok(())) => Err(error),
            (Ok(_), Err(error)) => Err(error),
            (Err(error), Err(ack)) => Err(anyhow::anyhow!("{error}; {ack}")),
        }
    }

    async fn call(&self, shape: &str, op: &str, args: &CanonValue) -> Result<CanonValue> {
        let id = self.take_id()?;
        self.register("register-invoke", id, "invoke", None)?;
        // Ordinary request JSON: exactly id/shape/op/args, never a type field,
        // nested envelope or authority claim.
        let request = serde_json::json!({
            "id": id,
            "shape": shape,
            "op": op,
            "args": args,
        });
        let json = serde_json::to_string(&request).map_err(|_| failure("invoke", "invalid"))?;
        let response = facade_operation(
            self.launcher,
            "invoke",
            tokio::time::Instant::now() + OPERATION_TIMEOUT,
            self.launcher.invoke(
                host_caller(),
                self.namespace,
                self.generation,
                self.receiver,
                id,
                &json,
            ),
        )
        .await;
        self.response("invoke", response)
    }

    async fn pull(
        &self,
        step: &'static str,
        deadline: tokio::time::Instant,
        handle: u64,
        max: u32,
    ) -> Result<CanonValue> {
        let id = self.take_id()?;
        self.register("register-pull", id, "stream-pull", Some(handle))?;
        let response = facade_operation(
            self.launcher,
            step,
            deadline,
            self.launcher
                .stream_pull(host_caller(), self.route(), id, handle, max),
        )
        .await;
        self.response(step, response)
    }

    /// Called only after successful EOF: Core removed this exact source entry
    /// and completed its observed cleanup before returning the empty batch.
    /// Prepared Hosts refuse the missing entry before ACK; legacy Hosts may
    /// ACK registration first, but their actual dispatch must still reject.
    async fn require_retired(&self, handle: u64) -> Result<()> {
        let id = self.take_id()?;
        require(
            handle > 0
                && handle <= 9_007_199_254_740_991
                && self.launcher.current_generation() == Some(self.generation),
            "retired",
            "invalid",
        )?;
        match self.launcher.register_ui_operation(
            host_caller(),
            self.route(),
            id,
            "stream-pull",
            Some(handle),
        ) {
            Err(LauncherHostError::Rejected("invalid")) => return Ok(()),
            Err(error) => return Err(failure("register-retired", host_code(&error))),
            Ok(ack) => {
                self.check_registration_ack(
                    "register-retired",
                    id,
                    "stream-pull",
                    Some(handle),
                    &ack,
                )?;
            }
        }
        let mut pull =
            std::pin::pin!(
                self.launcher
                    .stream_pull(host_caller(), self.route(), id, handle, 1,)
            );
        match tokio::time::timeout_at(tokio::time::Instant::now() + OPERATION_TIMEOUT, &mut pull)
            .await
        {
            Ok(Ok(response)) => {
                // An unexpected success still performs its real acknowledge.
                let _ = self.launcher.acknowledge(
                    host_caller(),
                    response.namespace.as_str(),
                    response.generation,
                    response.receiver.as_str(),
                    response.ticket_id,
                );
                Err(failure("retired", "invalid"))
            }
            Ok(Err(_)) => Ok(()),
            Err(_) => {
                facade_cleanup(self.launcher, pull).await;
                Err(failure("retired", "deadline"))
            }
        }
    }
}

/// Shared turn backend over the two real transports: the direct received-UI
/// Session methods and the headless LauncherCore native facade.
enum Ui<'a> {
    Direct(ReceivedUi<'a>),
    Facade(FacadeUi<'a>),
}

impl Ui<'_> {
    async fn call(&self, shape: &str, op: &str, args: &CanonValue) -> Result<CanonValue> {
        match self {
            Ui::Direct(ui) => ui.call(shape, op, args).await,
            Ui::Facade(ui) => ui.call(shape, op, args).await,
        }
    }

    async fn pull(
        &self,
        step: &'static str,
        deadline: tokio::time::Instant,
        handle: u64,
        max: u32,
    ) -> Result<CanonValue> {
        match self {
            Ui::Direct(ui) => ui.pull(step, deadline, handle, max).await,
            Ui::Facade(ui) => ui.pull(step, deadline, handle, max).await,
        }
    }

    async fn require_retired(&self, handle: u64) -> Result<()> {
        match self {
            Ui::Direct(ui) => ui.require_retired(handle).await,
            Ui::Facade(ui) => ui.require_retired(handle).await,
        }
    }
}

async fn received_turn(session: &Session) -> Result<()> {
    let generation = 7;
    session
        .bind_ui_generation(generation)
        .map_err(|error| core_failure("attach", error))?;
    let attachments = session
        .ui_attachments()
        .map_err(|error| core_failure("attach", error))?;
    let [attachment] = attachments.as_slice() else {
        return Err(failure("attach", "invalid"));
    };
    require(
        attachment.generation == Some(generation) && attachment.pack.as_str() == "local.harness-ui",
        "attach",
        "invalid",
    )?;
    let ui = Ui::Direct(ReceivedUi {
        session,
        attachment,
        generation,
    });
    ui_turn(&ui).await
}

/// The shared real turn: initial load -> actual UI guest terminal-input.submit
/// -> bounded pulls -> emitted/save/completed -> EOF/retired -> persisted tail.
async fn ui_turn(ui: &Ui<'_>) -> Result<()> {
    // Match the real Harness composer: first load, then the named WIT input
    // argument to actual terminal-input.submit (and therefore dynamic run-flow).
    let load_args = canon(&serde_json::json!({"conversation":CONVERSATION}))
        .map_err(|_| failure("load", "invalid"))?;
    let previous = ui.call(STORE_SHAPE, "load", &load_args).await?;
    let previous_messages = value_messages(&previous)?;
    require(previous_messages.is_empty(), "load", "invalid")?;
    let args = canon(&serde_json::json!({
        "input":{"conversation":CONVERSATION,"text":INPUT}
    }))
    .map_err(|_| failure("submit", "invalid"))?;
    let opened = ui.call(INPUT_SHAPE, "submit", &args).await?;
    require(
        opened.get("kind").and_then(CanonValue::as_str) == Some("stream"),
        "submit",
        "invalid",
    )?;
    let handle = opened
        .get("handle")
        .and_then(CanonValue::as_i64)
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| failure("submit", "invalid"))?;
    let deadline = tokio::time::Instant::now() + OPERATION_TIMEOUT;
    let mut assistant = String::new();
    let mut deltas = 0usize;
    let mut saved = false;
    let mut completed = false;
    let mut step = "flow";
    let mut identity: Option<(String, String)> = None;
    let mut previous_sequence: Option<i64> = None;
    loop {
        let batch = ui.pull(step, deadline, handle, 8).await?;
        let frames = batch.as_array().ok_or_else(|| failure(step, "invalid"))?;
        require(frames.len() <= 8, step, "invalid")?;
        if frames.is_empty() {
            break;
        }
        for frame in frames {
            let request = string(frame, "request", step)?;
            let plan = string(frame, "plan_digest", step)?;
            Digest::parse(plan).map_err(|_| failure(step, "invalid"))?;
            match &identity {
                Some((expected_request, expected_plan)) => require(
                    request == expected_request.as_str() && plan == expected_plan.as_str(),
                    step,
                    "invalid",
                )?,
                None => identity = Some((request.to_owned(), plan.to_owned())),
            }
            let sequence = frame
                .get("sequence")
                .and_then(CanonValue::as_i64)
                .ok_or_else(|| failure(step, "invalid"))?;
            if let Some(previous) = previous_sequence {
                require(Some(sequence) == previous.checked_add(1), step, "invalid")?;
            }
            previous_sequence = Some(sequence);
            let kind = frame.get("kind").ok_or_else(|| failure(step, "invalid"))?;
            match string(kind, "kind", step)? {
                "started" => {}
                "step_started" => step = flow_step(kind.get("step").and_then(CanonValue::as_str)),
                "step_value" => {
                    saved |= kind.get("step").and_then(CanonValue::as_str) == Some("save");
                }
                "emitted" => {
                    require(
                        string(kind, "shape", step)? == OUTPUT_SHAPE,
                        step,
                        "invalid",
                    )?;
                    let value = kind.get("value").ok_or_else(|| failure(step, "invalid"))?;
                    if value.get("kind").and_then(CanonValue::as_str) == Some("delta") {
                        assistant.push_str(string(value, "text", step)?);
                        deltas += 1;
                    }
                }
                "step_failed" => {
                    let failed_step = flow_step(kind.get("step").and_then(CanonValue::as_str));
                    return Err(failure(failed_step, event_error_code(kind)));
                }
                "failed" => return Err(failure(step, event_error_code(kind))),
                "cancelled" => return Err(failure(step, "cancelled")),
                "completed" => completed = true,
                _ => return Err(failure(step, "invalid")),
            }
        }
    }
    require(
        deltas > 0 && !assistant.trim().is_empty(),
        "generate",
        "invalid",
    )?;
    require(saved, "save", "invalid")?;
    require(completed, "flow", "invalid")?;
    // EOF consumed the actual UI source; a subsequent handle reuse must fail.
    ui.require_retired(handle).await?;
    let persisted = ui.call(STORE_SHAPE, "load", &load_args).await?;
    let messages = value_messages(&persisted)?;
    require(
        messages.len() == previous_messages.len() + 2,
        "save",
        "invalid",
    )?;
    let [user, reply] = &messages[messages.len() - 2..] else {
        return Err(failure("save", "invalid"));
    };
    require(
        user.get("role").and_then(CanonValue::as_str) == Some("user")
            && user.get("content").and_then(CanonValue::as_str) == Some(INPUT)
            && reply.get("role").and_then(CanonValue::as_str) == Some("assistant")
            && reply.get("content").and_then(CanonValue::as_str) == Some(assistant.as_str()),
        "save",
        "invalid",
    )?;
    Ok(())
}

fn value_messages(value: &CanonValue) -> Result<&[CanonValue]> {
    require(
        value.get("kind").and_then(CanonValue::as_str) == Some("value"),
        "load",
        "invalid",
    )?;
    value
        .get("value")
        .and_then(|value| value.get("messages"))
        .and_then(CanonValue::as_array)
        .ok_or_else(|| failure("load", "invalid"))
}

fn string<'a>(value: &'a CanonValue, key: &str, step: &'static str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(CanonValue::as_str)
        .ok_or_else(|| failure(step, "invalid"))
}

fn event_error_code(kind: &CanonValue) -> &'static str {
    flow_code(
        kind.get("error")
            .and_then(|error| error.get("code"))
            .and_then(CanonValue::as_str),
    )
}

/// Ordinary review, save and Vault credential entry shared by all three
/// entries. The direct helper also starts its Session; the facade test drops
/// this Core before opening LauncherCore on the same data root.
async fn prepare_core(fixture: &Fixture, secret: &[u8]) -> Result<Core> {
    let core = fixture.core().map_err(|_| failure("fixture", "invalid"))?;
    tokio::time::timeout(
        OPERATION_TIMEOUT,
        core.save_profile(fixture.profile.clone(), None),
    )
    .await
    .map_err(|_| failure("review", "deadline"))?
    .map_err(|error| core_failure("review", error))?;
    core.set_secret(&fixture.profile.id, "api-key", secret)
        .await
        .map_err(|error| core_failure("credential", error))?;
    Ok(core)
}

/// The existing direct turn: ordinary preparation -> actual Core::start ->
/// shared received-UI turn -> real Session close -> plaintext scan.
async fn direct_fixture_turn(fixture: &Fixture, secret: &[u8]) -> Result<()> {
    // This existing fixture entry verifies all seven canonical pins and scripts
    // only their ordinary permission review; no runtime authority is fabricated.
    let core = prepare_core(fixture, secret).await?;
    let session = tokio::time::timeout(OPERATION_TIMEOUT, core.start(&fixture.profile.id))
        .await
        .map_err(|_| failure("start", "deadline"))?
        .map_err(|error| core_failure("start", error))?;
    let result = received_turn(&session).await;
    // Always request the ordinary Session close, including a failed real turn.
    // A timeout/error cannot be mistaken for native clean-stop acceptance.
    let closed = match tokio::time::timeout(OPERATION_TIMEOUT, session.close()).await {
        Ok(result) => result.map_err(|error| core_failure("cleanup", error)),
        Err(_) => Err(failure("cleanup", "deadline")),
    };
    if let Err(error) = result {
        if let Err(cleanup) = closed {
            return Err(anyhow::anyhow!("{error}; {cleanup}"));
        }
        return Err(error);
    }
    closed?;
    assert_no_plaintext(&fixture.data_root(), secret)
        .map_err(|_| failure("credential", "invalid"))?;
    println!("live-ui step=complete code=ok");
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires TOBKIRI_TEST_LLAMA=1, explicit live+UI Profile and local Gemma 3 1B server"]
async fn real_live_received_ui_submit_stream_saves_persisted_tail() -> Result<()> {
    let fixture = live_fixture()?;
    let secret = key_file()?;
    direct_fixture_turn(&fixture, &secret).await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "requires TOBKIRI_TEST_LLAMA=1, explicit live+UI Profile and local Gemma 3 1B server"]
async fn real_live_received_ui_submit_stream_saves_persisted_tail_multi_thread() -> Result<()> {
    let fixture = live_fixture()?;
    let secret = key_file()?;
    direct_fixture_turn(&fixture, &secret).await
}

/// Headless multi_thread LauncherCore facade regression. The real start mints
/// the generation; every load/submit/stream-pull/retired dispatch/persisted
/// load performs the production registration -> invoke/stream_pull -> ticket
/// validation -> acknowledge sequence on the internally spawned native task,
/// then ordinary detach/stop close the Session. This is not Tauri IPC
/// macro/ACL integration, native platform GUI acceptance, WebKit iframe
/// lifecycle proof or normal native Stop-button acceptance.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "requires TOBKIRI_TEST_LLAMA=1, explicit live+UI Profile and local Gemma 3 1B server"]
async fn real_live_launcher_ui_submit_stream_saves_persisted_tail_multi_thread() -> Result<()> {
    let fixture = live_fixture()?;
    let secret = key_file()?;
    // Ordinary review, save and Vault credential entry through the same
    // preparation the direct tests use; this Core never starts a Session
    // and is dropped before the facade opens the same private data root.
    prepare_core(&fixture, &secret).await?;
    let tickets = TransportTickets::new().map_err(|error| failure("facade", host_code(&error)))?;
    let namespace = tickets
        .copy_namespace()
        .map_err(|error| failure("facade", host_code(&error)))?;
    let launcher = LauncherCore::open_with_config_and_tickets(
        fixture.host.clone(),
        CoreConfig::for_test_data_root(fixture.data_root()),
        tickets,
    )
    .map_err(|error| failure("facade", host_code(&error)))?;
    let frames = facade_operation(
        &launcher,
        "start",
        tokio::time::Instant::now() + OPERATION_TIMEOUT,
        launcher.start(host_caller(), fixture.profile.id.as_str()),
    )
    .await?;
    let [frame] = frames.as_slice() else {
        return Err(failure("start", "invalid"));
    };
    require(
        frame.generation > 0 && frame.generation <= 9_007_199_254_740_991,
        "start",
        "invalid",
    )?;
    require(
        launcher.current_generation() == Some(frame.generation),
        "start",
        "invalid",
    )?;
    let ui = Ui::Facade(FacadeUi {
        launcher: &launcher,
        namespace: &namespace,
        generation: frame.generation,
        receiver: frame.ui_session_id.as_str(),
        next_id: Cell::new(1),
        last_ticket: Cell::new(0),
        acks: Cell::new(0),
    });
    let result = ui_turn(&ui).await.and_then(|()| match &ui {
        // At least load + submit + one nonempty pull + EOF pull + persisted
        // load exceed the four-ticket transport capacity, so completion
        // proves the real acknowledge path freed its slots.
        Ui::Facade(facade) => require(facade.acks.get() >= 5, "ack", "invalid"),
        Ui::Direct(_) => Ok(()),
    });
    // A successful turn requests the actual facade detach for its route;
    // ordinary stop runs even when the turn or detach failed.
    let detached = if result.is_ok() {
        facade_operation(
            &launcher,
            "detach",
            tokio::time::Instant::now() + OPERATION_TIMEOUT,
            launcher.detach(
                host_caller(),
                &namespace,
                frame.generation,
                frame.ui_session_id.as_str(),
            ),
        )
        .await
    } else {
        Ok(())
    };
    let stopped = facade_operation(
        &launcher,
        "cleanup",
        tokio::time::Instant::now() + OPERATION_TIMEOUT,
        launcher.stop(host_caller()),
    )
    .await
    .and_then(|()| {
        require(
            launcher.current_generation().is_none(),
            "cleanup",
            "invalid",
        )
    });
    let cleanup = match (detached, stopped) {
        (Ok(()), stopped) => stopped,
        (Err(detach), Ok(())) => Err(detach),
        (Err(detach), Err(stop)) => Err(anyhow::anyhow!("{detach}; {stop}")),
    };
    if let Err(error) = result {
        if let Err(cleanup) = cleanup {
            return Err(anyhow::anyhow!("{error}; {cleanup}"));
        }
        return Err(error);
    }
    cleanup?;
    assert_no_plaintext(&fixture.data_root(), &secret)
        .map_err(|_| failure("credential", "invalid"))?;
    println!("live-ui step=complete code=ok");
    Ok(())
}
