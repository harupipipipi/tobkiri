//! Own the private Launcher socket for the fixed Provider preparation call.
use std::{
    fs::{self, DirBuilder, Permissions},
    io::ErrorKind,
    net::Shutdown,
    os::unix::{
        fs::{DirBuilderExt, PermissionsExt},
        net::{UnixListener, UnixStream},
    },
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

use anyhow::{ensure, Context, Result};
use tauri::AppHandle;

use crate::native_provider_prepare_ipc::{admit_connection, AdmittedConnection};

pub(crate) const SOCKET_ENV: &str = "TOBKIRI_NATIVE_PROVIDER_PREPARE_SOCKET";
pub(crate) const OWNER_ENV: &str = "TOBKIRI_NATIVE_PROVIDER_PREPARE_OWNER";
pub(crate) const APPEND_ENV: &str = "TOBKIRI_NATIVE_CONVERSATION_APPEND";
pub(crate) const TURN_READ_ENV: &str = "TOBKIRI_NATIVE_SAVED_TURN_READ";
pub(crate) const TURN_START_ENV: &str = "TOBKIRI_NATIVE_SAVED_TURN_START";
pub(crate) const SAVED_ORCHESTRATION_ENV: &str = "TOBKIRI_NATIVE_SAVED_ORCHESTRATION";
pub(crate) const SAVED_LOCAL_CHAT_ENV: &str = "TOBKIRI_NATIVE_SAVED_LOCAL_CHAT";
pub(crate) const AI_ROUTING_ENV: &str = "TOBKIRI_NATIVE_AI_ROUTING";
pub(crate) const AI_STREAM_RESULT_ENV: &str = "TOBKIRI_NATIVE_AI_STREAM_RESULT";
pub(crate) const AI_USAGE_COST_ENV: &str = "TOBKIRI_NATIVE_AI_USAGE_COST";
pub(crate) const AI_TOOL_INTENTS_ENV: &str = "TOBKIRI_NATIVE_AI_TOOL_INTENTS";
pub(crate) const AI_PIPELINE_ENV: &str = "TOBKIRI_NATIVE_AI_PIPELINE";
pub(crate) const AI_GATEWAY_RESULT_ENV: &str = "TOBKIRI_NATIVE_AI_GATEWAY_RESULT";
pub(crate) const AI_GATEWAY_POLICY_ENV: &str = "TOBKIRI_NATIVE_AI_GATEWAY_POLICY";

// The original production Broker can execute at most sixteen requests.
// Keep their socket handlers bounded while the native Jobs own actual admission.
const MAX_LOCAL_CHAT_HANDLERS: usize = 16;

#[derive(Debug, Clone)]
pub(crate) struct ProviderPrepareEndpoint {
    pub(crate) socket: PathBuf,
    pub(crate) owner: String,
}

struct PrivateSocketDirectory(PathBuf);

impl Drop for PrivateSocketDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_file(self.0.join("s"));
        let _ = fs::remove_dir(&self.0);
    }
}

#[derive(Default)]
struct ServiceState {
    stopping: AtomicBool,
    active: Mutex<ActiveConnections>,
}

#[derive(Default)]
struct ActiveConnections {
    admitting_or_inline: Option<UnixStream>,
    local_chat: Vec<LocalChatHandler>,
}

struct LocalChatHandler {
    monitored: UnixStream,
    thread: JoinHandle<()>,
}

impl ActiveConnections {
    fn close(&self) {
        for stream in self
            .admitting_or_inline
            .iter()
            .chain(self.local_chat.iter().map(|handler| &handler.monitored))
        {
            let _ = stream.shutdown(Shutdown::Both);
        }
    }

    fn reap_local_chat(&mut self) {
        let mut index = 0;
        while index < self.local_chat.len() {
            if self.local_chat[index].thread.is_finished() {
                let handler = self.local_chat.swap_remove(index);
                join_local_chat(handler);
            } else {
                index += 1;
            }
        }
    }
}

impl ServiceState {
    fn active(&self) -> std::sync::MutexGuard<'_, ActiveConnections> {
        self.active
            .lock()
            .unwrap_or_else(|error| error.into_inner())
    }

    fn stop(&self) {
        self.stopping.store(true, Ordering::Release);
        self.active().close();
    }

    fn start_local_chat(&self, app: &AppHandle, admitted: AdmittedConnection) -> Result<()> {
        let mut active = self.active();
        if self.stopping.load(Ordering::Acquire)
            || active.local_chat.len() >= MAX_LOCAL_CHAT_HANDLERS
        {
            drop(active);
            return admitted.reject_unavailable();
        }
        active.local_chat.try_reserve(1)?;
        let monitored = active
            .admitting_or_inline
            .take()
            .context("missing admitted local Provider socket")?;
        let app = app.clone();
        let thread = thread::Builder::new()
            .name("tobkiri-saved-local-chat".into())
            .spawn(move || report_unverified(admitted.run(&app)))?;
        active
            .local_chat
            .push(LocalChatHandler { monitored, thread });
        Ok(())
    }

    fn finish(&self) {
        self.stop();
        let handlers = std::mem::take(&mut self.active().local_chat);
        for handler in handlers {
            join_local_chat(handler);
        }
    }
}

pub(crate) struct ProviderPrepareService {
    state: Arc<ServiceState>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl ProviderPrepareService {
    pub(crate) fn start(app: AppHandle) -> Result<(Self, ProviderPrepareEndpoint)> {
        let identity = crate::shell_handoff::process_identity(std::process::id())?;
        // A short private path fits Darwin's sockaddr_un and avoids deriving
        // a socket name from a Profile or a renderer-provided value.
        let root = PathBuf::from(format!(
            "/private/tmp/tobkiri-provider-{:032x}",
            rand::random::<u128>()
        ));
        DirBuilder::new().mode(0o700).create(&root)?;
        let directory = PrivateSocketDirectory(root);
        let socket = directory.0.join("s");
        let listener = UnixListener::bind(&socket)?;
        fs::set_permissions(&socket, Permissions::from_mode(0o600))?;
        listener.set_nonblocking(true)?;
        let state = Arc::new(ServiceState::default());
        let worker_state = Arc::clone(&state);
        let worker = thread::Builder::new()
            .name("tobkiri-provider-prepare".into())
            .spawn(move || {
                let _directory = directory;
                serve(app, listener, worker_state);
            })?;
        Ok((
            Self {
                state,
                worker: Mutex::new(Some(worker)),
            },
            ProviderPrepareEndpoint {
                socket,
                owner: format!(
                    "{}:{}:{}",
                    identity.pid, identity.start_tvsec, identity.start_tvusec
                ),
            },
        ))
    }

    pub(crate) fn request_stop(&self) {
        self.state.stop();
    }

    pub(crate) fn join_until(&self, deadline: Instant) -> Result<()> {
        self.request_stop();
        let mut worker = self
            .worker
            .lock()
            .map_err(|_| anyhow::anyhow!("Provider socket owner is poisoned"))?;
        while worker.as_ref().is_some_and(|thread| !thread.is_finished()) {
            let remaining = deadline.saturating_duration_since(Instant::now());
            ensure!(
                !remaining.is_zero(),
                "Provider socket shutdown is still pending"
            );
            thread::sleep(remaining.min(Duration::from_millis(10)));
        }
        if let Some(thread) = worker.take() {
            thread
                .join()
                .map_err(|_| anyhow::anyhow!("Provider socket worker failed"))?;
        }
        Ok(())
    }
}

impl Drop for ProviderPrepareService {
    fn drop(&mut self) {
        self.request_stop();
    }
}

fn serve(app: AppHandle, listener: UnixListener, state: Arc<ServiceState>) {
    while !state.stopping.load(Ordering::Acquire) {
        state.active().reap_local_chat();
        match listener.accept() {
            Ok((stream, _)) => {
                let monitored = match stream.try_clone().context("track Provider connection") {
                    Ok(stream) => stream,
                    Err(_) => continue,
                };
                {
                    let mut active = state.active();
                    if state.stopping.load(Ordering::Acquire) {
                        break;
                    }
                    active.admitting_or_inline = Some(monitored);
                }
                let result = admit_connection(&app, stream).and_then(|admitted| match admitted {
                    Some(connection) if connection.is_saved_local_chat() => {
                        state.start_local_chat(&app, connection)
                    }
                    Some(connection) => connection.run(&app),
                    None => Ok(()),
                });
                report_unverified(result);
                state.active().admitting_or_inline.take();
            }
            Err(error) if error.kind() == ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(20))
            }
            Err(error) if error.kind() == ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
    state.finish();
}

fn report_unverified(result: Result<()>) {
    if result.is_err() {
        log::debug!("native Provider connection ended without a verified reply");
    }
}

fn join_local_chat(handler: LocalChatHandler) {
    if handler.thread.join().is_err() {
        log::debug!("native local Provider socket handler terminated unexpectedly");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;

    #[test]
    fn stops_the_inline_and_local_sockets_and_joins_the_local_handler() {
        let state = ServiceState::default();
        let (inline, mut caller) = UnixStream::pair().unwrap();
        let (mut worker, mut provider) = UnixStream::pair().unwrap();
        let monitored = worker.try_clone().unwrap();
        let joined = Arc::new(AtomicBool::new(false));
        let completed = Arc::clone(&joined);
        let timeout = Some(Duration::from_secs(2));
        caller.set_read_timeout(timeout).unwrap();
        provider.set_read_timeout(timeout).unwrap();
        worker.set_read_timeout(timeout).unwrap();
        let thread = thread::spawn(move || {
            assert_eq!(worker.read(&mut [0]).unwrap(), 0);
            completed.store(true, Ordering::Release);
        });
        {
            let mut active = state.active();
            active.admitting_or_inline = Some(inline);
            active
                .local_chat
                .push(LocalChatHandler { monitored, thread });
        }

        state.finish();

        assert_eq!(caller.read(&mut [0]).unwrap(), 0);
        assert_eq!(provider.read(&mut [0]).unwrap(), 0);
        assert!(state.active().local_chat.is_empty());
        assert!(joined.load(Ordering::Acquire));
    }
}
