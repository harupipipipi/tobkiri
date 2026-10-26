//! Optional in-process Core host. Legacy startup remains the default until M5.

use async_trait::async_trait;
use serde::Serialize;
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU8, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tauri::{AppHandle, Emitter, Manager, WebviewWindow};
use tobkiri_launcher_core_host::{
    copy_final_host_payload,
    core::{
        ids::{ProfileId, UiSessionId},
        AllocationBudget, ApprovalAnswer, ApprovalPrompt, CanonValue, CoreBuffer, HostIo,
        TrustedPrompt, UiEmbed,
    },
    observe_shutdown, preserve_primary_close, restore_primary, startup_ui_embedding,
    trusted_host_origin, trusted_review, DebugScope, HostCaller, HostError, LauncherCore,
    ShutdownCompletion, ShutdownObservation, TransportTickets, UiFrame, UiOperationRegistration,
    UiRoute, UiTransportResponse, UiTransportTicket, PACK_UI_CSP, TRUSTED_HOST_RECEIVER,
};

#[derive(Clone, Serialize)]
struct UiError {
    code: &'static str,
    message: String,
}

impl From<HostError> for UiError {
    fn from(error: HostError) -> Self {
        let code = match &error {
            HostError::Caller | HostError::Core => "denied",
            HostError::NoSession => "closed",
            HostError::AssetPath | HostError::Invalid => "invalid",
            HostError::Limit => "limit-exceeded",
            HostError::RegistrationFull => "registration-full",
            HostError::Duplicate => "duplicate",
            HostError::Cancelled => "cancelled",
            HostError::Rejected(code) => *code,
        };
        Self {
            code,
            message: error.to_string(),
        }
    }
}

// Tauri exposes the top-level webview URL here. This is a top-window guard,
// not proof of the invoking iframe origin; native frame IPC proof stays open.
fn caller(window: &WebviewWindow) -> Result<(String, String), UiError> {
    let label = window.label().to_owned();
    let url = window
        .url()
        .map_err(|_| UiError::from(HostError::Caller))?
        .to_string();
    HostCaller {
        label: &label,
        url: &url,
    }
    .check()?;
    crate::native_ipc_probe::admitted(window);
    Ok((label, url))
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CoreStartResult {
    namespace: String,
    generation: u64,
    attachments: Vec<UiFrame>,
}

struct NativeHost {
    app: AppHandle,
    terminal: Mutex<BTreeMap<String, PendingTerminal>>,
    startup_warning_sent: AtomicBool,
    output_generation: std::sync::RwLock<Option<u64>>,
    generation_transition: tokio::sync::Mutex<()>,
    host_page_loaded: AtomicBool,
    tickets: TransportTickets,
    debug_scope: Option<DebugScope>,
}

struct PendingTerminal {
    sender: tokio::sync::oneshot::Sender<String>,
    max_bytes: usize,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct HostOutputEvent<'a> {
    namespace: &'a str,
    generation: u64,
    receiver: &'static str,
    ticket_id: u64,
    // The optional Core surface is presentation text only. It never selects
    // the receiver, host window, session, or any authority.
    surface: Option<&'a str>,
    payload: String,
}

impl NativeHost {
    fn retire_output_generation(&self) -> Result<(), HostError> {
        let mut active = self
            .output_generation
            .write()
            .map_err(|_| HostError::Core)?;
        if let Some(generation) = active.take() {
            self.tickets.retire_generation(generation)?;
        }
        Ok(())
    }

    fn activate_output_generation(&self, generation: u64) -> Result<(), HostError> {
        if generation == 0 || generation > 9_007_199_254_740_991 {
            return Err(HostError::Limit);
        }
        let mut active = self
            .output_generation
            .write()
            .map_err(|_| HostError::Core)?;
        if let Some(previous) = active.replace(generation) {
            self.tickets.retire_generation(previous)?;
        }
        Ok(())
    }

    fn emit_core_output(
        &self,
        buffer: &CoreBuffer,
        surface: Option<&str>,
    ) -> Result<(), tobkiri_launcher_core_host::core::HostError> {
        let active = self.output_generation.try_read().map_err(|_| {
            tobkiri_launcher_core_host::core::HostError("trusted output is busy".into())
        })?;
        let generation = active.ok_or_else(|| {
            tobkiri_launcher_core_host::core::HostError("trusted output is unavailable".into())
        })?;
        let reservation = self.tickets.reserve_host_output(generation).map_err(|_| {
            tobkiri_launcher_core_host::core::HostError("trusted output queue is busy".into())
        })?;
        let ticket_id = reservation.ticket_id();
        let payload = copy_final_host_payload(buffer, surface).map_err(|_| {
            tobkiri_launcher_core_host::core::HostError("trusted output exceeds its limit".into())
        })?;
        self.app
            .emit_to(
                "core-host",
                "tobkiri-core-output",
                &HostOutputEvent {
                    namespace: self.tickets.namespace(),
                    generation,
                    receiver: TRUSTED_HOST_RECEIVER,
                    ticket_id,
                    surface,
                    payload,
                },
            )
            .map_err(|_| {
                tobkiri_launcher_core_host::core::HostError("trusted output is unavailable".into())
            })?;
        reservation.keep_until_ack();
        Ok(())
    }

    fn acknowledge_core_output(
        &self,
        namespace: &str,
        generation: u64,
        receiver: &str,
        ticket_id: u64,
    ) -> Result<(), HostError> {
        if generation == 0
            || generation > 9_007_199_254_740_991
            || ticket_id == 0
            || ticket_id > 9_007_199_254_740_991
        {
            return Err(HostError::Invalid);
        }
        let active = self.output_generation.read().map_err(|_| HostError::Core)?;
        if *active != Some(generation) {
            return Ok(());
        }
        let _ = self
            .tickets
            .acknowledge_host_output(namespace, generation, receiver, ticket_id)?;
        Ok(())
    }

    async fn read_terminal_line_with_limit(
        &self,
        prompt: &str,
        max_bytes: usize,
    ) -> Result<String, tobkiri_launcher_core_host::core::HostError> {
        #[derive(Clone, Serialize)]
        #[serde(rename_all = "camelCase")]
        struct Read<'a> {
            id: &'a str,
            prompt: &'a str,
            max_bytes: usize,
        }
        const MAX_TERMINAL_REPLY_BYTES: usize = 65_536;
        if prompt.len() > 4096 || max_bytes > MAX_TERMINAL_REPLY_BYTES {
            return Err(tobkiri_launcher_core_host::core::HostError(
                "terminal request exceeds the host limit".into(),
            ));
        }
        let id = format!("terminal_{}", ulid::Ulid::new());
        let (sender, receiver) = tokio::sync::oneshot::channel();
        {
            let mut pending = self.terminal.lock().map_err(|_| {
                tobkiri_launcher_core_host::core::HostError("terminal closed".into())
            })?;
            if pending.len() >= 1 {
                return Err(tobkiri_launcher_core_host::core::HostError(
                    "finish the existing terminal prompt".into(),
                ));
            }
            pending.insert(id.clone(), PendingTerminal { sender, max_bytes });
        }
        let _lease = TerminalLease {
            host: self,
            id: id.clone(),
        };
        if self
            .app
            .emit_to(
                "core-host",
                "tobkiri-core-terminal-read",
                Read {
                    id: &id,
                    prompt,
                    max_bytes,
                },
            )
            .is_err()
        {
            if let Ok(mut pending) = self.terminal.lock() {
                pending.remove(&id);
            }
            return Err(tobkiri_launcher_core_host::core::HostError(
                "terminal unavailable".into(),
            ));
        }
        let result = tokio::time::timeout(Duration::from_secs(240), receiver).await;
        if let Ok(mut pending) = self.terminal.lock() {
            pending.remove(&id);
        }
        match result {
            Ok(Ok(text)) => Ok(text),
            _ => Err(tobkiri_launcher_core_host::core::HostError(
                "terminal request expired".into(),
            )),
        }
    }
}

struct NativeCancellation(Arc<AtomicBool>);
impl Drop for NativeCancellation {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}

struct TerminalLease<'a> {
    host: &'a NativeHost,
    id: String,
}
impl Drop for TerminalLease<'_> {
    fn drop(&mut self) {
        if let Ok(mut pending) = self.host.terminal.lock() {
            pending.remove(&self.id);
        }
        let _ = self
            .host
            .app
            .emit_to("core-host", "tobkiri-core-terminal-close", &self.id);
    }
}

#[async_trait]
impl TrustedPrompt for NativeHost {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        #[cfg(target_os = "macos")]
        {
            let Some(window) = self.app.get_webview_window("core-host") else {
                return ApprovalAnswer::Unavailable;
            };
            let Ok(review) = trusted_review(&prompt) else {
                return ApprovalAnswer::Unavailable;
            };
            let cancelled = Arc::new(AtomicBool::new(false));
            let _lease = NativeCancellation(cancelled.clone());
            return match tauri::async_runtime::spawn_blocking(move || {
                crate::native_plan_review::show_detail_cancellable_with_warning(
                    &window,
                    review.title,
                    review.explanation,
                    review.accept,
                    &review.detail,
                    cancelled,
                    review.dangerous,
                )
            })
            .await
            {
                Ok(Ok(true)) => ApprovalAnswer::Approve,
                Ok(Ok(false)) => ApprovalAnswer::Deny,
                _ => ApprovalAnswer::Unavailable,
            };
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = prompt;
            // A complete, scrollable native review renderer is a prerequisite;
            // a truncated plugin confirmation must not silently replace it.
            ApprovalAnswer::Unavailable
        }
    }
}

#[async_trait]
impl HostIo for NativeHost {
    fn display(&self, _surface: &str, _content: &CanonValue) {
        // An unleased CanonValue is not a trusted transport buffer. The
        // budgeted CoreBuffer callback is the only path that may cross IPC.
    }

    async fn terminal_read_line(
        &self,
        _prompt: &str,
    ) -> Result<String, tobkiri_launcher_core_host::core::HostError> {
        Err(tobkiri_launcher_core_host::core::HostError(
            "budgeted terminal input is required".into(),
        ))
    }

    async fn terminal_read_line_budgeted(
        &self,
        prompt: &str,
        max_bytes: usize,
        budget: Arc<dyn AllocationBudget>,
    ) -> Result<CoreBuffer, tobkiri_launcher_core_host::core::HostError> {
        const MAX_TERMINAL_REPLY_BYTES: usize = 65_536;
        if max_bytes > MAX_TERMINAL_REPLY_BYTES || prompt.len() > 4096 {
            return Err(tobkiri_launcher_core_host::core::HostError(
                "terminal input exceeds the host limit".into(),
            ));
        }
        let mut builder = CoreBuffer::builder(&budget, max_bytes);
        // Reserve the full producer-funded Core buffer before asking the host
        // UI to read. The bounded UI string remains host input; only its
        // checked copy enters this already-reserved Core-owned buffer.
        builder.write_chunk(max_bytes, |_| Ok(0)).map_err(|_| {
            tobkiri_launcher_core_host::core::HostError(
                "terminal input budget is unavailable".into(),
            )
        })?;
        let input = self
            .read_terminal_line_with_limit(prompt, max_bytes)
            .await?;
        if input.len() > max_bytes {
            return Err(tobkiri_launcher_core_host::core::HostError(
                "terminal input exceeds the requested limit".into(),
            ));
        }
        builder.append(input.as_bytes()).map_err(|_| {
            tobkiri_launcher_core_host::core::HostError(
                "terminal input budget is unavailable".into(),
            )
        })?;
        builder.finish().map_err(|_| {
            tobkiri_launcher_core_host::core::HostError(
                "terminal input budget is unavailable".into(),
            )
        })
    }

    fn display_budgeted(
        &self,
        surface: &str,
        content: &CoreBuffer,
    ) -> Result<(), tobkiri_launcher_core_host::core::HostError> {
        self.emit_core_output(content, Some(surface))
    }

    fn terminal_write_budgeted(
        &self,
        text: &CoreBuffer,
    ) -> Result<(), tobkiri_launcher_core_host::core::HostError> {
        self.emit_core_output(text, None)
    }

    fn terminal_write(&self, text: &str) {
        #[derive(Clone, Serialize)]
        struct Text<'a> {
            text: &'a str,
        }
        // Preserve Core's fixed startup notice at most once. Guest output
        // requires the budgeted CoreBuffer callback and cannot use this legacy
        // borrowed-string event path; repeated guest strings must not create
        // an unbounded Tauri event queue.
        const UI_WARNING: &str =
            "Tobkiri: this host cannot embed the selected UI; input Packs are running.\n";
        if text == UI_WARNING
            && self
                .startup_warning_sent
                .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
        {
            let _ = self
                .app
                .emit_to("core-host", "tobkiri-core-terminal", Text { text });
        }
    }

    fn embed_ui(
        &self,
        request: UiEmbed,
    ) -> Result<UiSessionId, tobkiri_launcher_core_host::core::HostError> {
        // No frame is created here. The frontend renders only attachments that
        // the Session later pairs with a separate Core-minted invocation token.
        startup_ui_embedding(&request.slot, || {
            UiSessionId::parse(&format!("frame_{}", ulid::Ulid::new()))
                .map_err(|_| tobkiri_launcher_core_host::core::HostError("UI unavailable".into()))
        })?
    }

    async fn pick_directory(&self, purpose: &str) -> Option<PathBuf> {
        #[cfg(not(target_os = "macos"))]
        {
            // The legacy blocking picker remains in the default Launcher.
            // This path requires an owned native cancellation handle first.
            let _ = purpose;
            None
        }
        #[cfg(target_os = "macos")]
        {
            if purpose.len() > 4096 {
                return None;
            }
            let app = self.app.clone();
            let title = purpose.to_owned();
            let cancelled = Arc::new(AtomicBool::new(false));
            let _lease = NativeCancellation(cancelled.clone());
            tauri::async_runtime::spawn_blocking(move || {
                crate::project_directory_picker::pick_named(&app, cancelled, &title, false)
            })
            .await
            .ok()?
            .ok()?
            .and_then(|mut paths| if paths.len() == 1 { paths.pop() } else { None })
        }
    }
}

#[tauri::command]
async fn core_start_profile(
    window: WebviewWindow,
    core: tauri::State<'_, Arc<LauncherCore>>,
    host: tauri::State<'_, Arc<NativeHost>>,
    profile: String,
) -> Result<CoreStartResult, UiError> {
    let (label, url) = caller(&window)?;
    if let Some(scope) = &host.debug_scope {
        let requested = ProfileId::parse(&profile).map_err(|_| HostError::Invalid)?;
        if !scope.allows_profile(&requested) {
            return Err(HostError::Invalid.into());
        }
    }
    let _transition = host.generation_transition.lock().await;
    let attachments = core
        .start(
            HostCaller {
                label: &label,
                url: &url,
            },
            &profile,
        )
        .await
        .map_err(UiError::from)?;
    let publication = (|| {
        let generation = core.current_generation().ok_or(HostError::Cancelled)?;
        host.activate_output_generation(generation)?;
        let namespace = host.tickets.copy_namespace()?;
        if core.current_generation() != Some(generation) {
            return Err(HostError::Cancelled);
        }
        Ok(CoreStartResult {
            namespace,
            generation,
            attachments,
        })
    })();
    match publication {
        Ok(result) => Ok(result),
        Err(error) => {
            // Commands share generation_transition, so a failed publication
            // can close its session without closing a replacement start.
            let output_cleanup = host.retire_output_generation();
            let session_cleanup = core
                .stop(HostCaller {
                    label: &label,
                    url: &url,
                })
                .await;
            output_cleanup?;
            session_cleanup.map_err(UiError::from)?;
            Err(UiError::from(error))
        }
    }
}
#[tauri::command]
async fn core_stop_session(
    window: WebviewWindow,
    core: tauri::State<'_, Arc<LauncherCore>>,
    host: tauri::State<'_, Arc<NativeHost>>,
) -> Result<(), UiError> {
    let (label, url) = caller(&window)?;
    let _transition = host.generation_transition.lock().await;
    host.retire_output_generation()?;
    host.terminal
        .lock()
        .map_err(|_| UiError::from(HostError::Core))?
        .clear();
    core.stop(HostCaller {
        label: &label,
        url: &url,
    })
    .await
    .map_err(Into::into)
}

#[tauri::command]
fn core_host_output_ack(
    window: WebviewWindow,
    host: tauri::State<'_, Arc<NativeHost>>,
    namespace: String,
    generation: u64,
    receiver: String,
    ticket_id: u64,
) -> Result<(), UiError> {
    caller(&window)?;
    host.acknowledge_core_output(&namespace, generation, &receiver, ticket_id)
        .map_err(Into::into)
}
#[tauri::command]
async fn pack_ui_invoke(
    window: WebviewWindow,
    core: tauri::State<'_, Arc<LauncherCore>>,
    namespace: String,
    ui_session_id: String,
    generation: u64,
    id: u64,
    request_json: String,
) -> Result<UiTransportResponse, UiError> {
    let (label, url) = caller(&window)?;
    core.invoke(
        HostCaller {
            label: &label,
            url: &url,
        },
        &namespace,
        generation,
        &ui_session_id,
        id,
        &request_json,
    )
    .await
    .map_err(Into::into)
}

#[tauri::command]
fn pack_ui_register(
    window: WebviewWindow,
    core: tauri::State<'_, Arc<LauncherCore>>,
    namespace: String,
    ui_session_id: String,
    generation: u64,
    id: u64,
    kind: String,
    handle: Option<u64>,
) -> Result<UiOperationRegistration, UiError> {
    let (label, url) = caller(&window)?;
    core.register_ui_operation(
        HostCaller {
            label: &label,
            url: &url,
        },
        UiRoute {
            namespace: &namespace,
            generation,
            receiver: &ui_session_id,
        },
        id,
        &kind,
        handle,
    )
    .map_err(Into::into)
}

#[tauri::command]
fn pack_ui_cancel(
    window: WebviewWindow,
    core: tauri::State<'_, Arc<LauncherCore>>,
    namespace: String,
    ui_session_id: String,
    generation: u64,
    id: u64,
    kind: String,
    handle: Option<u64>,
) -> Result<(), UiError> {
    let (label, url) = caller(&window)?;
    core.cancel(
        HostCaller {
            label: &label,
            url: &url,
        },
        UiRoute {
            namespace: &namespace,
            generation,
            receiver: &ui_session_id,
        },
        id,
        &kind,
        handle,
    )
    .map_err(Into::into)
}
#[tauri::command]
fn pack_ui_ack(
    window: WebviewWindow,
    core: tauri::State<'_, Arc<LauncherCore>>,
    namespace: String,
    ui_session_id: String,
    generation: u64,
    ticket_id: u64,
) -> Result<(), UiError> {
    let (label, url) = caller(&window)?;
    core.acknowledge(
        HostCaller {
            label: &label,
            url: &url,
        },
        &namespace,
        generation,
        &ui_session_id,
        ticket_id,
    )
    .map_err(Into::into)
}
#[tauri::command]
async fn pack_ui_detach(
    window: WebviewWindow,
    core: tauri::State<'_, Arc<LauncherCore>>,
    namespace: String,
    ui_session_id: String,
    generation: u64,
) -> Result<(), UiError> {
    let (label, url) = caller(&window)?;
    core.detach(
        HostCaller {
            label: &label,
            url: &url,
        },
        &namespace,
        generation,
        &ui_session_id,
    )
    .await
    .map_err(Into::into)
}
#[tauri::command]
async fn pack_ui_stream_pull(
    window: WebviewWindow,
    core: tauri::State<'_, Arc<LauncherCore>>,
    namespace: String,
    ui_session_id: String,
    generation: u64,
    id: u64,
    handle: u64,
    max: u32,
) -> Result<UiTransportResponse, UiError> {
    let (label, url) = caller(&window)?;
    core.stream_pull(
        HostCaller {
            label: &label,
            url: &url,
        },
        UiRoute {
            namespace: &namespace,
            generation,
            receiver: &ui_session_id,
        },
        id,
        handle,
        max,
    )
    .await
    .map_err(Into::into)
}
#[tauri::command]
async fn pack_ui_stream_cancel(
    window: WebviewWindow,
    core: tauri::State<'_, Arc<LauncherCore>>,
    namespace: String,
    ui_session_id: String,
    generation: u64,
    id: u64,
    handle: u64,
    abort_pull: bool,
) -> Result<Option<UiTransportTicket>, UiError> {
    let (label, url) = caller(&window)?;
    core.stream_cancel(
        HostCaller {
            label: &label,
            url: &url,
        },
        UiRoute {
            namespace: &namespace,
            generation,
            receiver: &ui_session_id,
        },
        id,
        handle,
        abort_pull,
    )
    .await
    .map_err(Into::into)
}
#[tauri::command]
fn core_terminal_reply(
    window: WebviewWindow,
    host: tauri::State<'_, Arc<NativeHost>>,
    id: String,
    text: String,
) -> Result<(), UiError> {
    caller(&window)?;
    if text.len() > 65_536 {
        return Err(HostError::Limit.into());
    }
    let mut pending = host
        .terminal
        .lock()
        .map_err(|_| UiError::from(HostError::Core))?;
    let max_bytes = pending
        .get(&id)
        .map(|request| request.max_bytes)
        .ok_or_else(|| UiError::from(HostError::Invalid))?;
    if text.len() > max_bytes {
        return Err(HostError::Limit.into());
    }
    let request = pending
        .remove(&id)
        .ok_or_else(|| UiError::from(HostError::Invalid))?;
    drop(pending);
    request
        .sender
        .send(text)
        .map_err(|_| HostError::Cancelled.into())
}

fn show_primary(app: &AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("core-host")
        .ok_or("Core host window unavailable")?;
    restore_primary(
        || window.unminimize(),
        || window.show(),
        || window.set_focus(),
    )
    .map_err(|e| e.to_string())
}

pub(crate) fn run() {
    // A malformed scope, or any debug variable in a release build, fails
    // closed before plugins, windows, or Core can touch normal user data.
    let debug_scope = DebugScope::from_env().expect("invalid Tobkiri Core debug scope");
    let mut context = tauri::generate_context!("tauri.core-host.conf.json");
    if let Some(scope) = &debug_scope {
        let config = context.config_mut();
        config.identifier = scope.identifier().to_owned();
        let window = config
            .app
            .windows
            .iter_mut()
            .find(|window| window.label == "core-host")
            .expect("core-host window configuration required for a debug scope");
        window.title = scope.window_title().to_owned();
        window.incognito = true;
    }
    let ipc_probe_setup =
        crate::native_ipc_probe::prepare(context.config_mut(), debug_scope.as_ref())
            .expect("invalid native IPC probe scope");
    let builder = tauri::Builder::default();
    #[cfg(any(target_os = "macos", windows, target_os = "linux"))]
    let builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
        if let Err(error) = show_primary(app) {
            log::warn!("Cannot restore Core host window: {error}");
        }
    }));
    let app = builder
        .plugin(tauri_plugin_dialog::init())
        .plugin(
            tauri::plugin::Builder::<tauri::Wry, ()>::new("core-host-navigation")
                .on_navigation(|_, url| {
                    trusted_host_origin(url.as_str())
                        || tobkiri_launcher_core_host::PackAssetRequest::parse(url.as_str()).is_ok()
                })
                .on_page_load(|webview, payload| {
                    if webview.label() != "core-host" {
                        return;
                    }
                    if payload.event() != tauri::webview::PageLoadEvent::Started {
                        return;
                    }
                    let Ok(main_url) = webview.url() else {
                        return;
                    };
                    // Ignore child-frame loads. Only a main-document reload or
                    // navigation retires the native attachment generation.
                    if main_url.as_str() != payload.url().as_str() {
                        return;
                    }
                    let Some(host) = webview.app_handle().try_state::<Arc<NativeHost>>() else {
                        return;
                    };
                    let already_loaded = host.host_page_loaded.swap(true, Ordering::AcqRel);
                    if !already_loaded {
                        return;
                    }
                    if let Some(core) = webview.app_handle().try_state::<Arc<LauncherCore>>() {
                        if let Err(error) = core.retire_active_generation() {
                            log::warn!("Cannot retire Core UI after host page navigation: {error}");
                        }
                    }
                    if let Err(error) = host.retire_output_generation() {
                        log::warn!("Cannot retire Core output after host page navigation: {error}");
                    }
                })
                .build(),
        )
        .register_asynchronous_uri_scheme_protocol("tobkiri-pack", |context, request, responder| {
            let app = context.app_handle().clone();
            let label = context.webview_label().to_owned();
            tauri::async_runtime::spawn(async move {
                let value = if label == "core-host" && request.method() == tauri::http::Method::GET
                {
                    if let Some(core) = app.try_state::<Arc<LauncherCore>>() {
                        core.asset_with_policy(&request.uri().to_string())
                            .await
                            .ok()
                    } else {
                        None
                    }
                } else {
                    None
                };
                let (status, body, mime, csp) = match value {
                    // The legacy Tauri responder still requires an owned Vec. Its
                    // transport-copy custody must be completed before native
                    // staged-source activation is considered production-ready.
                    Some(asset) => (200, asset.bytes.to_vec(), asset.mime, asset.csp),
                    // Denials keep the closed static policy; a failed lookup
                    // never carries a fabricated per-Pack source.
                    None => (403, Vec::new(), "text/plain", PACK_UI_CSP.to_string()),
                };
                let response = tauri::http::Response::builder()
                    .status(status)
                    .header("Content-Type", mime)
                    .header("Content-Security-Policy", csp)
                    // Opaque-origin frames use CORS for module scripts. These
                    // bytes are public immutable Pack files, never host data;
                    // credentials and IPC remain unavailable to the frame.
                    .header("Access-Control-Allow-Origin", "*")
                    .header("Cross-Origin-Resource-Policy", "cross-origin")
                    .header("X-Content-Type-Options", "nosniff")
                    .header("Referrer-Policy", "no-referrer")
                    .header("Cache-Control", "no-store")
                    .body(body)
                    .expect("fixed response headers");
                responder.respond(response);
            });
        })
        .setup(move |app| {
            let tickets = TransportTickets::new()?;
            let host = Arc::new(NativeHost {
                app: app.handle().clone(),
                terminal: Mutex::new(BTreeMap::new()),
                startup_warning_sent: AtomicBool::new(false),
                output_generation: std::sync::RwLock::new(None),
                generation_transition: tokio::sync::Mutex::new(()),
                host_page_loaded: AtomicBool::new(false),
                tickets: tickets.clone(),
                debug_scope: debug_scope.clone(),
            });
            #[cfg(debug_assertions)]
            let core_config = {
                use tobkiri_launcher_core_host::core::CoreConfig;
                match &debug_scope {
                    Some(scope) => CoreConfig::for_test_data_root(scope.data_root().to_owned()),
                    None => CoreConfig::default(),
                }
            };
            #[cfg(not(debug_assertions))]
            let core_config = tobkiri_launcher_core_host::core::CoreConfig::default();
            // The real native composition opens ONE Core with its actual Host.
            // Native Kit and the frontend wrapper can retain this exact owner;
            // neither needs another Core/root/control account to obtain it.
            let shared_core = Arc::new(
                tobkiri_launcher_core_host::core::Core::open(core_config, host.clone())
                    .map_err(|_| HostError::Core)?,
            );
            let core = Arc::new(LauncherCore::from_shared_core_with_tickets(
                shared_core.clone(),
                tickets,
            ));
            app.manage(shared_core);
            app.manage(host);
            app.manage(core);
            crate::native_ipc_probe::install(app, ipc_probe_setup)?;
            setup_tray(app)?;
            Ok(())
        })
        .on_window_event(|window, event| match event {
            tauri::WindowEvent::CloseRequested { api, .. } => {
                if let Some(Err(error)) =
                    preserve_primary_close(window.label(), || api.prevent_close(), || window.hide())
                {
                    log::warn!("Cannot hide Core host window: {error}");
                }
            }
            tauri::WindowEvent::Destroyed if window.label() == "core-host" => {
                if let Some(core) = window.app_handle().try_state::<Arc<LauncherCore>>() {
                    if let Err(error) = core.retire_active_generation() {
                        log::warn!("Cannot retire Core UI after native host destruction: {error}");
                    }
                }
                if let Some(host) = window.app_handle().try_state::<Arc<NativeHost>>() {
                    if let Err(error) = host.retire_output_generation() {
                        log::warn!(
                            "Cannot retire Core output after native host destruction: {error}"
                        );
                    }
                }
            }
            _ => {}
        })
        .invoke_handler(tauri::generate_handler![
            core_start_profile,
            core_stop_session,
            core_host_output_ack,
            pack_ui_invoke,
            pack_ui_register,
            pack_ui_cancel,
            pack_ui_ack,
            pack_ui_detach,
            pack_ui_stream_pull,
            pack_ui_stream_cancel,
            core_terminal_reply
        ])
        .build(context)
        .expect("failed to create generic Core host");
    let shutdown = Arc::new(AtomicU8::new(0));
    app.run(move |app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if shutdown.load(Ordering::Acquire) == 2 {
                return;
            }
            api.prevent_exit();
            if shutdown
                .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
            {
                let core = app.state::<Arc<LauncherCore>>().inner().clone();
                let host = app.state::<Arc<NativeHost>>().inner().clone();
                let app = app.clone();
                let shutdown = shutdown.clone();
                let (finished, receiver) = std::sync::mpsc::channel();
                let watchdog_app = app.clone();
                std::thread::spawn(move || {
                    // This independent thread still reaches the budget if a
                    // cleanup poll blocks a runtime worker on filesystem/SQL.
                    let result = observe_shutdown(receiver, crate::shutdown_budget::RUNTIME_WATCHDOG);
                    let shutdown_closed = matches!(&result, ShutdownObservation::Closed);
                    match result {
                        ShutdownObservation::Closed => {},
                        ShutdownObservation::CleanupFailed => log::error!("Core shutdown failed; final cleanup/audit completion is unconfirmed"),
                        ShutdownObservation::Deadline | ShutdownObservation::WorkerLost => log::error!("Core shutdown budget reached or worker lost; forcing exit with final audit completion unconfirmed"),
                    }
                    crate::native_ipc_probe::shutdown(&watchdog_app, shutdown_closed);
                    shutdown.store(2, Ordering::Release);
                    watchdog_app.exit(0);
                });
                tauri::async_runtime::spawn(async move {
                    // Cancellation and stream close run off the UI thread.
                    if let Ok(mut pending) = host.terminal.lock() { pending.clear(); }
                    let retired = host.retire_output_generation();
                    let stopped = core.stop(HostCaller { label: "core-host", url: "tauri://localhost/" }).await;
                    let result = retired.and(stopped);
                    let _ = finished.send(if result.is_ok() { ShutdownCompletion::Closed } else { ShutdownCompletion::CleanupFailed });
                });
            }
        }
    });
}

fn setup_tray(app: &tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    use tauri::{
        menu::{Menu, MenuItem},
        tray::TrayIconBuilder,
    };
    let open = MenuItem::with_id(app, "open", "Open", true, None::<&str>)?;
    let update = MenuItem::with_id(app, "update", "Check for Updates", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &update, &quit])?;
    let mut tray = TrayIconBuilder::with_id("core-host-tray")
        .tooltip("Tobkiri Launcher")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => {
                if let Err(error) = show_primary(app) {
                    log::warn!("Cannot restore Core host window: {error}");
                }
            }
            "update" => {
                std::thread::spawn(|| {
                    if let Ok(Some(update)) = crate::updater::check_for_update() {
                        let _ = crate::updater::open_release_page(&update);
                    }
                });
            }
            "quit" => app.exit(0),
            _ => {}
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}
