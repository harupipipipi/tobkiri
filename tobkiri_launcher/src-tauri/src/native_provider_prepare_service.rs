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

pub(crate) const SOCKET_ENV: &str = "TOBKIRI_NATIVE_PROVIDER_PREPARE_SOCKET";
pub(crate) const OWNER_ENV: &str = "TOBKIRI_NATIVE_PROVIDER_PREPARE_OWNER";
pub(crate) const APPEND_ENV: &str = "TOBKIRI_NATIVE_CONVERSATION_APPEND";
pub(crate) const TURN_READ_ENV: &str = "TOBKIRI_NATIVE_SAVED_TURN_READ";

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
    active: Mutex<Option<UnixStream>>,
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
        self.state.stopping.store(true, Ordering::Release);
        let active = self
            .state
            .active
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if let Some(stream) = active.as_ref() {
            let _ = stream.shutdown(Shutdown::Both);
        }
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
        match listener.accept() {
            Ok((stream, _)) => {
                let monitored = match stream.try_clone().context("track Provider connection") {
                    Ok(stream) => stream,
                    Err(_) => continue,
                };
                {
                    let mut active = state
                        .active
                        .lock()
                        .unwrap_or_else(|error| error.into_inner());
                    if state.stopping.load(Ordering::Acquire) {
                        break;
                    }
                    *active = Some(monitored);
                }
                // Serve one connection at a time. If cleanup is unconfirmed,
                // the native job tracker retains the original worker and owner.
                if crate::native_provider_prepare_ipc::handle_connection(&app, stream).is_err() {
                    log::debug!("native Provider connection ended without a verified reply");
                }
                state
                    .active
                    .lock()
                    .unwrap_or_else(|error| error.into_inner())
                    .take();
            }
            Err(error) if error.kind() == ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(20))
            }
            Err(error) if error.kind() == ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
}
