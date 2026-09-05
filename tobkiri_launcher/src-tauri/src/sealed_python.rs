//! Verified packaged Python execution environment contract.
//!
//! Integrity (the manifest and every byte named by it), environment source
//! provenance, and the outer platform package signature are separate checks.
//!
//! Threat model: packaged macOS launch protects against a corrupt or
//! non-cooperating updater, cross-UID writes, and path/symlink substitution by
//! copying through no-follow directory handles and executing from the held
//! snapshot root. The snapshot is user-owned, so arbitrary malicious code
//! already executing as the same UID is explicitly out of scope; this module
//! does not describe that snapshot as OS-immutable or as an authenticity
//! boundary. Windows and Linux release packaging stays disabled until those
//! platforms have a real package-provenance boundary.

use std::collections::{BTreeMap, HashSet};
use std::ffi::{OsStr, OsString};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::ops::{Deref, DerefMut};
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::config::AppConfig;
use crate::process_utils;
use crate::sealed_python_protocol as protocol;

pub const MANIFEST_SCHEMA: &str = "io.tobkiri.sealed-python-environment.v1";
pub const ATTESTATION_SCHEMA: &str = protocol::ATTESTATION_SCHEMA;
pub const RESOURCE_DIRECTORY: &str =
    crate::runtime_resource_paths::SEALED_PYTHON_RESOURCE_DIRECTORY;
pub const MANIFEST_FILENAME: &str = "sealed-environment.v1.json";
const DIRECTORY_MODES_FILENAME: &str = "sealed-directory-modes.v1.json";
const DIRECTORY_MODES_SCHEMA: &str = "io.tobkiri.sealed-python-directory-modes.v1";
const LIFETIME_LEASE: &str = "lease.v1";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(15);
const TERMINATION_CONFIRM_TIMEOUT: Duration = Duration::from_millis(250);
const TERMINATION_POLL_INTERVAL: Duration = Duration::from_millis(10);
const BACKGROUND_REAPER_TIMEOUT: Duration = Duration::from_secs(30);
const CHILD_WAIT_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_ATTESTATION_BYTES: u64 = 64 * 1024;
const SNAPSHOT_RUNTIME_MANIFEST: &str = "app/runtime-resource-manifest.v1.json";
const RUNTIME_OVERLAY_SCHEMA: &str = "io.tobkiri.sealed-runtime-overlay.v1";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PythonRole {
    Kernel,
    Defaultspack,
    HostHelper,
}

impl PythonRole {
    pub fn name(self) -> &'static str {
        match self {
            Self::Kernel => protocol::ROLE_TYPED,
            Self::Defaultspack => protocol::ROLE_DEFAULTSPACK,
            Self::HostHelper => protocol::ROLE_HOST_HELPER,
        }
    }
}

#[derive(Debug, Default)]
pub struct RoleArguments(Vec<OsString>);

impl RoleArguments {
    pub fn defaultspack(values: impl IntoIterator<Item = OsString>) -> Result<Self> {
        let values = values.into_iter().collect::<Vec<_>>();
        for value in &values {
            let text = value.to_string_lossy();
            let lower = text.to_ascii_lowercase();
            if text == "-c"
                || text == "-m"
                || text.contains('\0')
                || ["token", "secret", "password", "api-key", "api_key"]
                    .iter()
                    .any(|marker| lower.contains(marker))
            {
                bail!("Defaultspack role arguments may not select an entrypoint or carry secrets");
            }
        }
        Ok(Self(values))
    }

    pub(crate) fn into_values(self) -> Vec<OsString> {
        self.0
    }
}

pub struct RoleCommand<'a> {
    command: &'a mut Command,
    packaged_role: Option<PythonRole>,
    environment: BTreeMap<OsString, OsString>,
    rejected_environment: Vec<OsString>,
}

impl<'a> RoleCommand<'a> {
    pub(crate) fn new(command: &'a mut Command) -> Self {
        Self {
            command,
            packaged_role: None,
            environment: BTreeMap::new(),
            rejected_environment: Vec::new(),
        }
    }

    fn packaged(command: &'a mut Command, role: PythonRole) -> Self {
        Self {
            command,
            packaged_role: Some(role),
            environment: BTreeMap::new(),
            rejected_environment: Vec::new(),
        }
    }

    fn finish(mut self) -> Result<()> {
        let Some(_) = self.packaged_role else {
            return Ok(());
        };
        if !self.rejected_environment.is_empty() {
            let keys = self
                .rejected_environment
                .iter()
                .map(|value| value.to_string_lossy())
                .collect::<Vec<_>>()
                .join(", ");
            bail!("packaged Python environment key is not allowed: {keys}");
        }
        self.command.env_clear();
        self.command.envs(std::mem::take(&mut self.environment));
        Ok(())
    }
}

impl RoleCommand<'_> {
    pub fn env(&mut self, key: impl AsRef<OsStr>, value: impl AsRef<OsStr>) -> &mut Self {
        if let Some(role) = self.packaged_role {
            let key = key.as_ref().to_os_string();
            if packaged_environment_key_allowed(role, &key) {
                self.environment.insert(key, value.as_ref().to_os_string());
            } else if !self.rejected_environment.contains(&key) {
                self.rejected_environment.push(key);
            }
        } else {
            self.command.env(key, value);
        }
        self
    }

    pub fn env_remove(&mut self, key: impl AsRef<OsStr>) -> &mut Self {
        if self.packaged_role.is_some() {
            self.environment.remove(key.as_ref());
        } else {
            self.command.env_remove(key);
        }
        self
    }

    pub fn envs<I, K, V>(&mut self, vars: I) -> &mut Self
    where
        I: IntoIterator<Item = (K, V)>,
        K: AsRef<OsStr>,
        V: AsRef<OsStr>,
    {
        for (key, value) in vars {
            self.env(key, value);
        }
        self
    }

    pub fn current_dir(&mut self, path: impl AsRef<Path>) -> &mut Self {
        self.command.current_dir(path);
        self
    }

    pub fn stdin(&mut self, value: Stdio) -> &mut Self {
        self.command.stdin(value);
        self
    }

    pub fn stdout(&mut self, value: Stdio) -> &mut Self {
        self.command.stdout(value);
        self
    }

    pub fn stderr(&mut self, value: Stdio) -> &mut Self {
        self.command.stderr(value);
        self
    }

    #[cfg(unix)]
    pub fn new_process_group(&mut self) -> &mut Self {
        use std::os::unix::process::CommandExt;
        self.command.process_group(0);
        self
    }
}

fn packaged_environment_key_allowed(role: PythonRole, key: &OsStr) -> bool {
    let Some(key) = key.to_str() else {
        return false;
    };
    let common = [
        "RUMI_HOME",
        "RUMI_USER_DATA",
        "RUMI_LOG_DIR",
        "PYTHONDONTWRITEBYTECODE",
    ];
    if common.contains(&key) {
        return true;
    }
    match role {
        PythonRole::Kernel => [
            "RUMI_DEFAULTSPACK_SECRETS_DIR",
            "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
            "RUMI_PORT",
            "TOBKIRI_HOST_CONTRACT_PATH",
            "RUMI_VIEWER_HOST_BROKER_CONNECTION",
            "RUMI_MACOS_PERMISSION_HOST",
            "PYTHONUTF8",
            "PYTHONIOENCODING",
            "PYTHONUNBUFFERED",
            "RUMI_ENVIRONMENT",
        ]
        .contains(&key),
        PythonRole::Defaultspack => [
            "RUMI_DEFAULTSPACK_SECRETS_DIR",
            "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
            "RUMI_VIEWER_HOST_BROKER_CONNECTION",
            "RUMI_VIEWER_BROKER_ATTESTATION_PUBLIC_KEY",
            "RUMI_VIEWER_BROKER_INSTANCE_NONCE",
            "RUMI_DEFAULTSPACK_GUARDIAN_RUN_ID",
            "TOBKIRI_HOST_CONTRACT_PATH",
            "DEFAULTS_HTTP_HOST",
            "DEFAULTS_HTTP_PORT",
            "RUMI_DEFAULTSPACK_PORT",
            "RUMI_PORT",
            "RUMI_DEFAULTSPACK_SURFACE",
            "RUMI_DEFAULTSPACK_DEBUG_ISOLATION",
            "RUMI_DEFAULTSPACK_REQUIRE_OWN_BIND",
            "RUMI_DEFAULTSPACK_OPEN_BROWSER",
        ]
        .contains(&key),
        PythonRole::HostHelper => ["RUMI_DEFAULTSPACK_CHAT_STORE_PATH"].contains(&key),
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SealedEnvironmentManifest {
    pub schema: String,
    pub environment_digest: String,
    pub platform: String,
    pub architecture: String,
    pub python_version: String,
    pub package_provenance: PackageProvenance,
    pub sentinels: SentinelContract,
    pub files: Vec<SealedFile>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PackageProvenance {
    pub kind: String,
    pub package_id: String,
    pub release_digest: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SentinelContract {
    pub stdlib_sha256: String,
    pub site_packages_sha256: String,
    pub native_sha256: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SealedFile {
    pub path: String,
    pub size: u64,
    pub sha256: String,
    pub executable: bool,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct SealedDirectoryModes {
    schema: String,
    directories: Vec<SealedDirectoryMode>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct SealedDirectoryMode {
    path: String,
    mode: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct StartupAttestation {
    schema: String,
    nonce: String,
    role: String,
    environment_digest: String,
    executable: String,
    prefix: String,
    base_prefix: String,
    sys_path: Vec<String>,
    stdlib_sha256: String,
    site_packages_sha256: String,
    native_sha256: String,
    runtime_overlay_sha256: String,
    outer_runtime_manifest_sha256: String,
    lifetime_lease: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
struct RuntimeOverlayAuthority {
    schema: &'static str,
    outer_manifest_sha256: String,
    sealed_manifest_sha256: String,
}

#[derive(Serialize)]
struct RuntimeOverlayDocument {
    schema: &'static str,
    overlay: RuntimeOverlayAuthority,
    entries: Vec<crate::runtime_resource_integrity::ResourceEntry>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct VerifiedRuntimeOverlay {
    bytes: Vec<u8>,
    sha256: String,
    authority: RuntimeOverlayAuthority,
}

#[cfg(target_os = "macos")]
#[derive(Clone, Debug, Eq, PartialEq)]
struct SnapshotFileIdentity {
    path: String,
    device: u64,
    inode: u64,
    size: u64,
    mode: u32,
    links: u64,
    modified_seconds: i64,
    modified_nanoseconds: i64,
    changed_seconds: i64,
    changed_nanoseconds: i64,
}

#[cfg(target_os = "macos")]
#[derive(Clone, Debug)]
struct SnapshotVerification {
    files: Vec<SnapshotFileIdentity>,
}

struct VerifiedEnvironment {
    root: PathBuf,
    manifest_path: PathBuf,
    manifest: SealedEnvironmentManifest,
    _root_lease: File,
    _interpreter_lease: File,
    environment_lease: Option<File>,
    snapshot_path: Option<PathBuf>,
    #[cfg(target_os = "macos")]
    snapshot_verification: Option<SnapshotVerification>,
    runtime_overlay: VerifiedRuntimeOverlay,
    cleanup_authority: CleanupAuthority,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum CleanupAuthority {
    BeforeChildSpawn,
    ConfirmedChildExitRequired,
}

/// A Python child together with the verified environment lease that must
/// remain alive until the process exits.
pub struct PythonChild {
    pid: u32,
    state: PythonChildState,
    operation_failures: ChildOperationFailures,
}

enum PythonChildState {
    Running {
        child: Child,
        environment: Option<Box<VerifiedEnvironment>>,
    },
    Exited {
        child: Option<Child>,
        status: std::process::ExitStatus,
    },
    ReaperOwned {
        completion: Receiver<ReaperCompletion>,
    },
    ReaperFailed {
        failure: ReaperFailure,
    },
    Transitioning,
}

enum ReaperCompletion {
    Exited(std::process::ExitStatus),
    Preserved(ReaperFailure),
}

#[derive(Clone, Copy)]
enum ReaperFailure {
    HandoffFailed,
    DeadlineExceeded,
    PollFailed,
    CompletionDisconnected,
}

impl ReaperFailure {
    fn message(self) -> &'static str {
        match self {
            Self::HandoffFailed => "Python child reaper handoff failed; snapshot was preserved",
            Self::DeadlineExceeded => {
                "Python child reaper deadline expired; snapshot was preserved"
            }
            Self::PollFailed => {
                "Python child reaper could not inspect process state; snapshot was preserved"
            }
            Self::CompletionDisconnected => {
                "Python child reaper completion channel disconnected; snapshot was preserved"
            }
        }
    }
}

#[derive(Default)]
struct ChildOperationFailures {
    #[cfg(test)]
    try_wait_once: bool,
    #[cfg(test)]
    kill_once: bool,
    #[cfg(test)]
    wait_once: bool,
    #[cfg(test)]
    reaper_handoff_once: bool,
    #[cfg(test)]
    reaper_timeout_once: bool,
    #[cfg(test)]
    reaper_poll_error_once: bool,
}

impl ChildOperationFailures {
    fn take_try_wait(&mut self) -> bool {
        #[cfg(test)]
        {
            std::mem::take(&mut self.try_wait_once)
        }
        #[cfg(not(test))]
        {
            false
        }
    }

    fn take_kill(&mut self) -> bool {
        #[cfg(test)]
        {
            std::mem::take(&mut self.kill_once)
        }
        #[cfg(not(test))]
        {
            false
        }
    }

    fn take_wait(&mut self) -> bool {
        #[cfg(test)]
        {
            std::mem::take(&mut self.wait_once)
        }
        #[cfg(not(test))]
        {
            false
        }
    }

    fn take_reaper_handoff(&mut self) -> bool {
        #[cfg(test)]
        {
            std::mem::take(&mut self.reaper_handoff_once)
        }
        #[cfg(not(test))]
        {
            false
        }
    }

    fn take_reaper_timeout(&mut self) -> bool {
        #[cfg(test)]
        {
            std::mem::take(&mut self.reaper_timeout_once)
        }
        #[cfg(not(test))]
        {
            false
        }
    }

    fn take_reaper_poll_error(&mut self) -> bool {
        #[cfg(test)]
        {
            std::mem::take(&mut self.reaper_poll_error_once)
        }
        #[cfg(not(test))]
        {
            false
        }
    }
}

impl PythonChild {
    pub(crate) fn development(child: Child) -> Self {
        let pid = child.id();
        Self {
            pid,
            state: PythonChildState::Running {
                child,
                environment: None,
            },
            operation_failures: ChildOperationFailures::default(),
        }
    }

    fn packaged(child: Child, mut environment: VerifiedEnvironment) -> Self {
        environment.require_confirmed_child_exit();
        let pid = child.id();
        Self {
            pid,
            state: PythonChildState::Running {
                child,
                environment: Some(Box::new(environment)),
            },
            operation_failures: ChildOperationFailures::default(),
        }
    }

    pub fn id(&self) -> u32 {
        self.pid
    }

    fn child_ref(&self) -> &Child {
        match &self.state {
            PythonChildState::Running { child, .. }
            | PythonChildState::Exited {
                child: Some(child), ..
            } => child,
            _ => panic!("Python child handle is owned by its bounded reaper"),
        }
    }

    fn child_mut(&mut self) -> &mut Child {
        match &mut self.state {
            PythonChildState::Running { child, .. }
            | PythonChildState::Exited {
                child: Some(child), ..
            } => child,
            _ => panic!("Python child handle is owned by its bounded reaper"),
        }
    }

    fn take_state(&mut self) -> PythonChildState {
        std::mem::replace(&mut self.state, PythonChildState::Transitioning)
    }

    fn finish_foreground_exit(
        child: Child,
        mut environment: Option<Box<VerifiedEnvironment>>,
        status: std::process::ExitStatus,
    ) -> PythonChildState {
        if let Some(mut environment) = environment.take() {
            environment.cleanup_after_confirmed_child_exit();
        }
        PythonChildState::Exited {
            child: Some(child),
            status,
        }
    }

    fn terminate_and_confirm_or_reap(&mut self, diagnostic: &'static str, attempt_kill: bool) {
        let state = self.take_state();
        self.state = match state {
            PythonChildState::Running { child, environment } => terminate_and_confirm_or_reap(
                child,
                environment,
                diagnostic,
                attempt_kill,
                &mut self.operation_failures,
            ),
            state => state,
        };
    }

    pub fn try_wait(&mut self) -> std::io::Result<Option<std::process::ExitStatus>> {
        if self.operation_failures.take_try_wait() {
            return Err(std::io::Error::other("injected child try_wait failure"));
        }
        let state = self.take_state();
        match state {
            PythonChildState::Running {
                mut child,
                environment,
            } => match child.try_wait() {
                Ok(Some(status)) => {
                    self.state = Self::finish_foreground_exit(child, environment, status);
                    Ok(Some(status))
                }
                Ok(None) => {
                    self.state = PythonChildState::Running { child, environment };
                    Ok(None)
                }
                Err(error) => {
                    self.state = PythonChildState::Running { child, environment };
                    Err(error)
                }
            },
            PythonChildState::Exited { child, status } => {
                self.state = PythonChildState::Exited { child, status };
                Ok(Some(status))
            }
            PythonChildState::ReaperOwned { completion } => match completion.try_recv() {
                Ok(ReaperCompletion::Exited(status)) => {
                    self.state = PythonChildState::Exited {
                        child: None,
                        status,
                    };
                    Ok(Some(status))
                }
                Ok(ReaperCompletion::Preserved(failure)) => {
                    self.state = PythonChildState::ReaperFailed { failure };
                    Err(std::io::Error::other(failure.message()))
                }
                Err(TryRecvError::Empty) => {
                    self.state = PythonChildState::ReaperOwned { completion };
                    Ok(None)
                }
                Err(TryRecvError::Disconnected) => {
                    let failure = ReaperFailure::CompletionDisconnected;
                    self.state = PythonChildState::ReaperFailed { failure };
                    Err(std::io::Error::other(failure.message()))
                }
            },
            PythonChildState::ReaperFailed { failure } => {
                self.state = PythonChildState::ReaperFailed { failure };
                Err(std::io::Error::other(failure.message()))
            }
            PythonChildState::Transitioning => unreachable!("Python child transition is atomic"),
        }
    }

    pub fn wait(&mut self) -> std::io::Result<std::process::ExitStatus> {
        if self.operation_failures.take_wait() {
            return Err(std::io::Error::other("injected child wait failure"));
        }
        let deadline = Instant::now() + CHILD_WAIT_TIMEOUT;
        loop {
            match self.try_wait()? {
                Some(status) => return Ok(status),
                None if Instant::now() < deadline => thread::sleep(TERMINATION_POLL_INTERVAL),
                None => {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        "Python child wait reached its bounded deadline",
                    ));
                }
            }
        }
    }

    pub fn kill(&mut self) -> std::io::Result<()> {
        let state = self.take_state();
        let PythonChildState::Running {
            mut child,
            environment,
        } = state
        else {
            self.state = state;
            return Ok(());
        };
        let result = if self.operation_failures.take_kill() {
            Err(std::io::Error::other("injected child kill failure"))
        } else {
            child.kill()
        };
        self.state = terminate_and_confirm_or_reap(
            child,
            environment,
            "explicit Python child termination",
            false,
            &mut self.operation_failures,
        );
        result
    }
}

impl Deref for PythonChild {
    type Target = Child;

    fn deref(&self) -> &Self::Target {
        self.child_ref()
    }
}

impl DerefMut for PythonChild {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.child_mut()
    }
}

impl Drop for PythonChild {
    fn drop(&mut self) {
        let state = self.take_state();
        if let PythonChildState::Running { child, environment } = state {
            let _ = terminate_and_confirm_or_reap(
                child,
                environment,
                "dropped Python child",
                true,
                &mut self.operation_failures,
            );
        }
    }
}

fn terminate_and_confirm_or_reap(
    mut child: Child,
    environment: Option<Box<VerifiedEnvironment>>,
    diagnostic: &'static str,
    attempt_kill: bool,
    failures: &mut ChildOperationFailures,
) -> PythonChildState {
    let already_exited = if failures.take_try_wait() {
        None
    } else {
        child.try_wait().ok().flatten()
    };
    if let Some(status) = already_exited {
        return PythonChild::finish_foreground_exit(child, environment, status);
    }

    if attempt_kill && !failures.take_kill() {
        let _ = child.kill();
    }
    let deadline = Instant::now() + TERMINATION_CONFIRM_TIMEOUT;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(status)) => {
                return PythonChild::finish_foreground_exit(child, environment, status);
            }
            Ok(None) | Err(_) => thread::sleep(TERMINATION_POLL_INTERVAL),
        }
    }

    let handoff_failure = failures.take_reaper_handoff();
    let force_timeout = failures.take_reaper_timeout();
    let force_poll_error = failures.take_reaper_poll_error();
    let (completion_sender, completion) = mpsc::sync_channel(1);
    let spawn_result = if handoff_failure {
        Err(std::io::Error::other("injected reaper handoff failure"))
    } else {
        thread::Builder::new()
            .name("tobkiri-python-reaper".to_owned())
            .spawn(move || {
                if force_timeout || force_poll_error {
                    let failure = if force_timeout {
                        ReaperFailure::DeadlineExceeded
                    } else {
                        ReaperFailure::PollFailed
                    };
                    let _ = completion_sender.send(ReaperCompletion::Preserved(failure));
                    return;
                }
                let deadline = Instant::now() + BACKGROUND_REAPER_TIMEOUT;
                let mut poll_failed = false;
                loop {
                    match child.try_wait() {
                        Ok(Some(status)) => {
                            if let Some(mut environment) = environment {
                                environment.cleanup_after_confirmed_child_exit();
                            }
                            let _ = completion_sender.send(ReaperCompletion::Exited(status));
                            return;
                        }
                        Ok(None) if Instant::now() < deadline => {
                            thread::sleep(TERMINATION_POLL_INTERVAL);
                        }
                        Err(_) if Instant::now() < deadline => {
                            poll_failed = true;
                            thread::sleep(TERMINATION_POLL_INTERVAL);
                        }
                        Ok(None) | Err(_) => {
                            let failure = if poll_failed {
                                ReaperFailure::PollFailed
                            } else {
                                ReaperFailure::DeadlineExceeded
                            };
                            eprintln!("Tobkiri sealed Python reaper: {}", failure.message());
                            let _ = completion_sender.send(ReaperCompletion::Preserved(failure));
                            return;
                        }
                    }
                }
            })
            .map(|_| ())
    };
    if let Err(error) = spawn_result {
        // The environment was disarmed before the child was spawned. If the
        // handoff cannot be established, dropping the payload deliberately
        // preserves the snapshot instead of risking deletion beneath a live
        // process. Do not include paths, arguments, or environment values in
        // this diagnostic.
        eprintln!(
            "Tobkiri sealed Python reaper handoff failed during {diagnostic}; preserving snapshot: {error}"
        );
        return PythonChildState::ReaperFailed {
            failure: ReaperFailure::HandoffFailed,
        };
    }
    PythonChildState::ReaperOwned { completion }
}

pub fn spawn_packaged_role<F>(
    config: &AppConfig,
    role: PythonRole,
    role_arguments: RoleArguments,
    configure: F,
) -> Result<PythonChild>
where
    F: FnOnce(&mut RoleCommand<'_>) -> Result<()>,
{
    let verified = VerifiedEnvironment::load(config)?;
    let nonce = random_nonce();
    let attestation_path = prepare_attestation_path(config, &nonce)?;
    let interpreter = fixed_interpreter(&verified.root);
    let mut command = process_utils::isolated_python(&interpreter);
    let packvm_bundle = packaged_packvm_bundle_binding(config)?;
    append_launch_wire(
        &mut command,
        role,
        &nonce,
        &attestation_path,
        &verified.manifest_path,
        &verified.root,
        &verified.runtime_overlay.sha256,
        &verified.runtime_overlay.authority.outer_manifest_sha256,
        packvm_bundle.as_ref(),
        role_arguments,
    )?;
    {
        let mut role_command = RoleCommand::packaged(&mut command, role);
        configure(&mut role_command)?;
        role_command.finish()?;
    }
    let path = if cfg!(windows) {
        format!(
            "{};{}",
            verified.root.join("venv/Scripts").display(),
            verified.root.join("runtime").display()
        )
    } else {
        format!(
            "{}:{}",
            verified.root.join("venv/bin").display(),
            verified.root.join("runtime/bin").display()
        )
    };
    command.env("PATH", path);
    command.env("RUMI_APP_DIR", verified.root.join("app"));
    command.env("PYTHONDONTWRITEBYTECODE", "1");
    command.env("PYTHONNOUSERSITE", "1");
    command.env("PYTHONSAFEPATH", "1");
    command.current_dir(&verified.root);

    verified.revalidate()?;
    let child = command
        .spawn()
        .with_context(|| format!("failed to spawn sealed Python {} role", role.name()))?;
    let mut child = PythonChild::packaged(child, verified);
    let attestation_result = {
        let PythonChildState::Running {
            child: process,
            environment: Some(environment),
        } = &mut child.state
        else {
            unreachable!("new packaged child must retain process and environment ownership");
        };
        wait_for_attestation(process, &attestation_path, &nonce, role, environment)
    };
    if let Err(error) = attestation_result {
        child.terminate_and_confirm_or_reap("startup attestation failure", true);
        let _ = fs::remove_file(&attestation_path);
        return Err(error);
    }
    let lease_result = match &mut child.state {
        PythonChildState::Running {
            environment: Some(environment),
            ..
        } => environment.prove_child_and_reacquire_parent_lease(),
        _ => unreachable!("attested child must retain environment ownership"),
    };
    if let Err(error) = lease_result {
        child.terminate_and_confirm_or_reap("child lifetime lease proof failure", true);
        let _ = fs::remove_file(&attestation_path);
        return Err(error);
    }
    let _ = fs::remove_file(&attestation_path);
    // Both parent and child retain the same shared lifetime lease. The parent
    // also retains the snapshot root descriptor until the child has exited.
    Ok(child)
}

fn append_launch_wire(
    command: &mut Command,
    role: PythonRole,
    nonce: &str,
    attestation_path: &Path,
    manifest_path: &Path,
    environment_root: &Path,
    runtime_overlay_sha256: &str,
    outer_runtime_manifest_sha256: &str,
    packvm_bundle: Option<&PackVMBundleBinding>,
    role_arguments: RoleArguments,
) -> Result<()> {
    if role != PythonRole::Defaultspack && !role_arguments.0.is_empty() {
        bail!("only the fixed Defaultspack role accepts application arguments");
    }
    command
        .args(protocol::launch_arguments(
            role.name(),
            nonce,
            attestation_path.as_os_str(),
            manifest_path.as_os_str(),
            environment_root.as_os_str(),
            runtime_overlay_sha256,
            outer_runtime_manifest_sha256,
            packvm_bundle
                .map(|binding| binding.root.as_os_str())
                .unwrap_or_else(|| OsStr::new("")),
            packvm_bundle
                .map(|binding| binding.provisioning_sha256.as_str())
                .unwrap_or(""),
            packvm_bundle
                .map(|binding| binding.helper_manifest_sha256.as_str())
                .unwrap_or(""),
            packvm_bundle
                .map(|binding| binding.helper_team_id.as_str())
                .unwrap_or(""),
        ))
        .arg(protocol::ARG_SEPARATOR)
        .args(role_arguments.0);
    Ok(())
}

#[derive(Debug)]
struct PackVMBundleBinding {
    root: PathBuf,
    provisioning_sha256: String,
    helper_manifest_sha256: String,
    helper_team_id: String,
}

fn packaged_packvm_bundle_binding(config: &AppConfig) -> Result<Option<PackVMBundleBinding>> {
    let binding = packaged_packvm_bundle_binding_from_app_dir(&config.app_dir)?;
    #[cfg(target_os = "macos")]
    if let Some(binding) = binding.as_ref() {
        verify_macos_static_code(&binding.root)
            .context("packaged application binding failed final static-code validation")?;
    }
    Ok(binding)
}

fn packaged_packvm_bundle_binding_from_app_dir(
    configured_app_dir: &Path,
) -> Result<Option<PackVMBundleBinding>> {
    let app_dir = configured_app_dir
        .canonicalize()
        .context("packaged application resource root is unavailable")?;
    let Some(resources) = app_dir.parent() else {
        return Ok(None);
    };
    let Some(contents) = resources.parent() else {
        return Ok(None);
    };
    let Some(bundle) = contents.parent() else {
        return Ok(None);
    };
    if app_dir.file_name() != Some(OsStr::new("app"))
        || resources.file_name() != Some(OsStr::new("Resources"))
        || contents.file_name() != Some(OsStr::new("Contents"))
        || bundle.extension() != Some(OsStr::new("app"))
    {
        return Ok(None);
    }
    let resources = bundle.join("Contents/Resources");
    let provisioning = read_bounded_regular(
        &resources.join("packvm-vz-provisioning.v1.json"),
        2 * 1024 * 1024,
    )
    .context("packaged PackVM provisioning manifest is unavailable")?;
    let helper_manifest = read_bounded_regular(
        &resources.join("packvm-vz-helper.manifest.v1.json"),
        256 * 1024,
    )
    .context("packaged PackVM helper manifest is unavailable")?;
    let policy = option_env!("TOBKIRI_MACOS_ARTIFACT_POLICY").unwrap_or("production-v1");
    let helper_team_id = if policy == "production-v1" {
        option_env!("TOBKIRI_MACOS_ARTIFACT_IDENTITY")
            .unwrap_or_default()
            .to_owned()
    } else {
        String::new()
    };
    Ok(Some(PackVMBundleBinding {
        root: bundle.to_path_buf(),
        provisioning_sha256: sha256_bytes(&provisioning),
        helper_manifest_sha256: sha256_bytes(&helper_manifest),
        helper_team_id,
    }))
}

impl VerifiedEnvironment {
    fn load(config: &AppConfig) -> Result<Self> {
        if config.is_dev_workspace() {
            bail!("sealed packaged Python authority is unavailable in a development workspace");
        }
        let source_root = config.app_dir.join(RESOURCE_DIRECTORY);
        let manifest_path = source_root.join(MANIFEST_FILENAME);
        let _source_environment_lease =
            acquire_environment_lease(&source_root.join(LIFETIME_LEASE))?;
        let manifest_bytes = read_bounded_regular(&manifest_path, 4 * 1024 * 1024)?;
        let expected_manifest =
            option_env!("TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256").unwrap_or_default();
        if expected_manifest.is_empty() {
            bail!("[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] Launcher build has no sealed Python manifest binding");
        }
        if sha256_bytes(&manifest_bytes) != expected_manifest {
            bail!("[PYTHON_SEALED_INVALID] sealed Python manifest differs from the build-provided identity");
        }
        let manifest: SealedEnvironmentManifest = serde_json::from_slice(&manifest_bytes)
            .context("[PYTHON_SEALED_INVALID] malformed sealed Python manifest")?;
        validate_manifest_contract(&manifest)?;
        verify_environment_tree(&source_root, &manifest)?;
        verify_package_provenance(config, &manifest.package_provenance)?;
        let runtime_resource_manifest = crate::runtime_resource_integrity::verify(&config.app_dir)
            .context("[PYTHON_SEALED_PROVENANCE_INVALID] packaged resource manifest rejected")?;
        let runtime_overlay = build_runtime_overlay(
            &manifest,
            &sha256_bytes(&manifest_bytes),
            &runtime_resource_manifest,
        )?;
        #[cfg(target_os = "macos")]
        {
            let (root, root_lease, snapshot_path, snapshot_verification) = create_macos_snapshot(
                config,
                &source_root,
                &manifest,
                &manifest_bytes,
                &runtime_resource_manifest,
                &runtime_overlay,
            )?;
            let mut pending = PendingSnapshotCleanup::new(snapshot_path, root_lease);
            let verified_root_lease = pending.root_handle().try_clone()?;
            let manifest_path = root.join(MANIFEST_FILENAME);
            let environment_lease = acquire_environment_lease(&root.join(LIFETIME_LEASE))?;
            let interpreter_lease = open_regular(&fixed_interpreter(&root))?;
            let (snapshot_path, construction_root_handle) = pending.disarm();
            drop(construction_root_handle);
            Ok(Self {
                root,
                manifest_path,
                manifest,
                _root_lease: verified_root_lease,
                _interpreter_lease: interpreter_lease,
                environment_lease: Some(environment_lease),
                snapshot_path: Some(snapshot_path),
                snapshot_verification: Some(snapshot_verification),
                runtime_overlay,
                cleanup_authority: CleanupAuthority::BeforeChildSpawn,
            })
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = runtime_resource_manifest;
            let root = source_root.clone();
            let root_lease = open_directory(&source_root)?;
            let manifest_path = root.join(MANIFEST_FILENAME);
            let environment_lease = acquire_environment_lease(&root.join(LIFETIME_LEASE))?;
            let interpreter_lease = open_regular(&fixed_interpreter(&root))?;
            Ok(Self {
                root,
                manifest_path,
                manifest,
                _root_lease: root_lease,
                _interpreter_lease: interpreter_lease,
                environment_lease: Some(environment_lease),
                snapshot_path: None,
                runtime_overlay,
                cleanup_authority: CleanupAuthority::BeforeChildSpawn,
            })
        }
    }

    fn revalidate(&self) -> Result<()> {
        #[cfg(target_os = "macos")]
        if self.snapshot_path.is_some() {
            use std::os::fd::AsRawFd;
            return verify_snapshot_anchored(
                self._root_lease.as_raw_fd(),
                &self.manifest,
                self.snapshot_verification
                    .as_ref()
                    .context("sealed snapshot verification identity is unavailable")?,
                Some(&self.runtime_overlay),
            );
        }
        verify_environment_tree(&self.root, &self.manifest)
    }

    fn prove_child_and_reacquire_parent_lease(&mut self) -> Result<()> {
        let lease = self
            .environment_lease
            .take()
            .context("sealed environment parent lease was already released")?;
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            if unsafe { libc::flock(lease.as_raw_fd(), libc::LOCK_UN) } != 0 {
                return Err(std::io::Error::last_os_error())
                    .context("release parent lease before proving child lease");
            }
        }
        drop(lease);
        let path = self.root.join(LIFETIME_LEASE);
        verify_child_lifetime_lease(&path)?;
        self.environment_lease = Some(acquire_environment_lease(&path)?);
        Ok(())
    }

    fn cleanup_snapshot(&mut self) {
        self.environment_lease.take();
        if let Some(path) = self.snapshot_path.take() {
            #[cfg(target_os = "macos")]
            cleanup_macos_snapshot(&path, &self._root_lease);
        }
    }

    fn require_confirmed_child_exit(&mut self) {
        self.cleanup_authority = CleanupAuthority::ConfirmedChildExitRequired;
    }

    fn cleanup_after_confirmed_child_exit(&mut self) {
        self.cleanup_snapshot();
        self.cleanup_authority = CleanupAuthority::ConfirmedChildExitRequired;
    }
}

impl Drop for VerifiedEnvironment {
    fn drop(&mut self) {
        if self.cleanup_authority == CleanupAuthority::BeforeChildSpawn {
            self.cleanup_snapshot();
        }
    }
}

#[cfg(target_os = "macos")]
struct PendingSnapshotCleanup {
    path: Option<PathBuf>,
    root_handle: Option<File>,
}

#[cfg(target_os = "macos")]
impl PendingSnapshotCleanup {
    fn new(path: Option<PathBuf>, root_handle: File) -> Self {
        Self {
            path,
            root_handle: Some(root_handle),
        }
    }

    fn root_handle(&self) -> &File {
        self.root_handle
            .as_ref()
            .expect("armed snapshot cleanup must retain its root handle")
    }

    fn disarm(&mut self) -> (PathBuf, File) {
        let path = self
            .path
            .take()
            .expect("macOS snapshot construction must have a cleanup path");
        let root_handle = self
            .root_handle
            .take()
            .expect("macOS snapshot construction must have a root handle");
        (path, root_handle)
    }
}

#[cfg(target_os = "macos")]
impl Drop for PendingSnapshotCleanup {
    fn drop(&mut self) {
        if let (Some(path), Some(root_handle)) = (self.path.take(), self.root_handle.as_ref()) {
            cleanup_macos_snapshot(&path, root_handle);
        }
    }
}

#[cfg(target_os = "macos")]
fn cleanup_macos_snapshot(path: &Path, root_handle: &File) {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    let Ok(path_metadata) = fs::symlink_metadata(path) else {
        return;
    };
    let Ok(handle_metadata) = root_handle.metadata() else {
        return;
    };
    if !path_metadata.is_dir()
        || path_metadata.file_type().is_symlink()
        || path_metadata.dev() != handle_metadata.dev()
        || path_metadata.ino() != handle_metadata.ino()
    {
        return;
    }
    fn make_directories_writable(path: &Path) -> std::io::Result<()> {
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
        for entry in fs::read_dir(path)? {
            let entry = entry?;
            let metadata = fs::symlink_metadata(entry.path())?;
            if metadata.is_dir() && !metadata.file_type().is_symlink() {
                make_directories_writable(&entry.path())?;
            }
        }
        Ok(())
    }
    if make_directories_writable(path).is_ok() {
        let _ = fs::remove_dir_all(path);
    }
}

#[cfg(target_os = "macos")]
fn canonical_private_temp_root() -> Result<PathBuf> {
    fs::canonicalize(std::env::temp_dir())
        .context("[PYTHON_SEALED_SNAPSHOT_INVALID] canonicalize private temp root")
}

fn build_runtime_overlay(
    sealed_manifest: &SealedEnvironmentManifest,
    sealed_manifest_sha256: &str,
    outer_manifest: &crate::runtime_resource_integrity::VerifiedResourceManifest,
) -> Result<VerifiedRuntimeOverlay> {
    let mut entries = Vec::new();
    for sealed_entry in sealed_manifest
        .files
        .iter()
        .filter(|entry| entry.path.starts_with("app/"))
    {
        entries.push(outer_manifest.bind_sealed_application(
            &sealed_entry.path,
            sealed_entry.size,
            &sealed_entry.sha256,
        )?);
    }
    entries.sort_by(|left, right| left.path.cmp(&right.path));
    if entries.is_empty() {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] runtime overlay closure is empty");
    }
    let authority = RuntimeOverlayAuthority {
        schema: RUNTIME_OVERLAY_SCHEMA,
        outer_manifest_sha256: outer_manifest.sha256().to_owned(),
        sealed_manifest_sha256: sealed_manifest_sha256.to_owned(),
    };
    let bytes = serde_json::to_vec(&RuntimeOverlayDocument {
        schema: "io.tobkiri.runtime-resource-manifest.v1",
        overlay: authority.clone(),
        entries,
    })?;
    Ok(VerifiedRuntimeOverlay {
        sha256: sha256_bytes(&bytes),
        bytes,
        authority,
    })
}

#[cfg(target_os = "macos")]
fn create_macos_snapshot(
    config: &AppConfig,
    source_root: &Path,
    manifest: &SealedEnvironmentManifest,
    manifest_bytes: &[u8],
    runtime_resource_manifest: &crate::runtime_resource_integrity::VerifiedResourceManifest,
    runtime_overlay: &VerifiedRuntimeOverlay,
) -> Result<(PathBuf, File, Option<PathBuf>, SnapshotVerification)> {
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{DirBuilderExt, MetadataExt};

    let temp_root = canonical_private_temp_root()?;
    let snapshot_path = temp_root.join(format!(
        ".tobkiri-sealed-python-{}-{}",
        std::process::id(),
        random_nonce()
    ));
    let mut builder = fs::DirBuilder::new();
    builder.mode(0o700);
    builder
        .create(&snapshot_path)
        .context("[PYTHON_SEALED_SNAPSHOT_INVALID] create private snapshot root")?;

    (|| {
        let source = open_directory(source_root)?;
        let runtime_source = open_directory(&config.app_dir)?;
        let runtime_source_identity = runtime_source.metadata()?;
        let snapshot = match open_directory_inheritable(&snapshot_path) {
            Ok(snapshot) => snapshot,
            Err(error) => {
                let _ = fs::remove_dir(&snapshot_path);
                return Err(error);
            }
        };
        let mut pending = PendingSnapshotCleanup::new(Some(snapshot_path.clone()), snapshot);
        copy_anchored_file(
            source.as_raw_fd(),
            pending.root_handle().as_raw_fd(),
            Path::new(MANIFEST_FILENAME),
            false,
            manifest_bytes.len() as u64,
            &sha256_bytes(manifest_bytes),
        )?;
        for entry in &manifest.files {
            copy_anchored_file(
                source.as_raw_fd(),
                pending.root_handle().as_raw_fd(),
                Path::new(&entry.path),
                entry.executable,
                entry.size,
                &entry.sha256,
            )?;
        }
        write_anchored_file(
            pending.root_handle().as_raw_fd(),
            Path::new(SNAPSHOT_RUNTIME_MANIFEST),
            &runtime_overlay.bytes,
        )?;
        let runtime_source_after = runtime_source.metadata()?;
        if (
            runtime_source_identity.dev(),
            runtime_source_identity.ino(),
            runtime_source_identity.mtime(),
            runtime_source_identity.mtime_nsec(),
        ) != (
            runtime_source_after.dev(),
            runtime_source_after.ino(),
            runtime_source_after.mtime(),
            runtime_source_after.mtime_nsec(),
        ) {
            bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] outer runtime root changed during snapshot");
        }
        let reverified_runtime = crate::runtime_resource_integrity::verify(&config.app_dir)
            .context("[PYTHON_SEALED_SNAPSHOT_INVALID] outer runtime changed during snapshot")?;
        if &reverified_runtime != runtime_resource_manifest {
            bail!(
                "[PYTHON_SEALED_SNAPSHOT_INVALID] runtime resource authority changed during snapshot"
            );
        }
        seal_snapshot_directories(pending.root_handle().as_raw_fd(), manifest)?;
        let snapshot_verification = authenticate_snapshot_anchored(
            pending.root_handle().as_raw_fd(),
            manifest,
            manifest_bytes.len() as u64,
            &sha256_bytes(manifest_bytes),
            Some(runtime_overlay),
        )?;
        // A non-cooperating updater changing the signed source while it is
        // copied is detected before this snapshot becomes executable.
        verify_package_provenance(config, &manifest.package_provenance)?;
        let (cleanup_path, snapshot) = pending.disarm();
        Ok((
            snapshot_path.clone(),
            snapshot,
            Some(cleanup_path),
            snapshot_verification,
        ))
    })()
}

#[cfg(target_os = "macos")]
fn open_directory_inheritable(path: &Path) -> Result<File> {
    use std::os::fd::FromRawFd;
    use std::os::unix::ffi::OsStrExt;

    let path = std::ffi::CString::new(path.as_os_str().as_bytes())?;
    let fd = unsafe {
        libc::open(
            path.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error()).context("open snapshot root");
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

#[cfg(target_os = "macos")]
fn copy_anchored_file(
    source_root: std::os::fd::RawFd,
    destination_root: std::os::fd::RawFd,
    relative: &Path,
    executable: bool,
    expected_size: u64,
    expected_sha256: &str,
) -> Result<()> {
    copy_anchored_file_as(
        source_root,
        destination_root,
        relative,
        relative,
        executable,
        expected_size,
        expected_sha256,
    )
}

#[cfg(target_os = "macos")]
fn copy_anchored_file_as(
    source_root: std::os::fd::RawFd,
    destination_root: std::os::fd::RawFd,
    source_relative: &Path,
    destination_relative: &Path,
    executable: bool,
    expected_size: u64,
    expected_sha256: &str,
) -> Result<()> {
    use std::io::Write;
    use std::os::fd::FromRawFd;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;

    validate_relative_path(&source_relative.to_string_lossy())?;
    validate_relative_path(&destination_relative.to_string_lossy())?;
    let source_components = source_relative.components().collect::<Vec<_>>();
    let destination_components = destination_relative.components().collect::<Vec<_>>();
    let mut source_dir = duplicate_fd(source_root)?;
    let mut destination_dir = duplicate_fd(destination_root)?;
    for component in &source_components[..source_components.len().saturating_sub(1)] {
        let Component::Normal(name) = component else {
            bail!("unsafe snapshot component");
        };
        source_dir = openat_directory(source_dir, name)?;
    }
    for component in &destination_components[..destination_components.len().saturating_sub(1)] {
        let Component::Normal(name) = component else {
            bail!("unsafe snapshot component");
        };
        destination_dir = mkdirat_open(destination_dir, name)?;
    }
    let source_name = source_components
        .last()
        .and_then(|component| match component {
            Component::Normal(name) => Some(name),
            _ => None,
        })
        .context("empty snapshot file path")?;
    let destination_name = destination_components
        .last()
        .and_then(|component| match component {
            Component::Normal(name) => Some(name),
            _ => None,
        })
        .context("empty snapshot destination file path")?;
    let source_name = std::ffi::CString::new(source_name.as_bytes())?;
    let destination_name = std::ffi::CString::new(destination_name.as_bytes())?;
    let source_fd = unsafe {
        libc::openat(
            source_dir,
            source_name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if source_fd < 0 {
        return Err(std::io::Error::last_os_error()).context("open anchored snapshot source");
    }
    let destination_fd = unsafe {
        libc::openat(
            destination_dir,
            destination_name.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            if executable { 0o555 } else { 0o444 },
        )
    };
    if destination_fd < 0 {
        unsafe { libc::close(source_fd) };
        return Err(std::io::Error::last_os_error()).context("create anchored snapshot file");
    }
    let mut source = unsafe { File::from_raw_fd(source_fd) };
    let mut destination = unsafe { File::from_raw_fd(destination_fd) };
    let before = source.metadata()?;
    if !before.is_file() || before.nlink() != 1 || before.len() != expected_size {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] source is not a singly-linked regular file");
    }
    let mut digest = Sha256::new();
    let mut copied = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = source.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
        destination.write_all(&buffer[..count])?;
        copied = copied.saturating_add(count as u64);
        if copied > expected_size {
            bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] source grew during snapshot copy");
        }
    }
    destination.flush()?;
    let after = source.metadata()?;
    if before.dev() != after.dev()
        || before.ino() != after.ino()
        || before.len() != after.len()
        || after.nlink() != 1
        || before.mtime() != after.mtime()
        || before.mtime_nsec() != after.mtime_nsec()
    {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] source changed during snapshot copy");
    }
    if copied != expected_size || hex::encode(digest.finalize()) != expected_sha256 {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] source digest changed during snapshot copy");
    }
    let destination_identity = snapshot_file_identity(destination_relative, &destination)?;
    let expected_mode = if executable { 0o555 } else { 0o444 };
    if destination_identity.size != expected_size
        || destination_identity.links != 1
        || destination_identity.mode != expected_mode
    {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] copied file metadata rejected");
    }
    unsafe {
        libc::close(source_dir);
        libc::close(destination_dir);
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn snapshot_file_identity(relative: &Path, file: &File) -> Result<SnapshotFileIdentity> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    let metadata = file.metadata()?;
    if !metadata.is_file() {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] snapshot entry is not a regular file");
    }
    Ok(SnapshotFileIdentity {
        path: relative.to_string_lossy().replace('\\', "/"),
        device: metadata.dev(),
        inode: metadata.ino(),
        size: metadata.len(),
        mode: metadata.permissions().mode() & 0o777,
        links: metadata.nlink(),
        modified_seconds: metadata.mtime(),
        modified_nanoseconds: metadata.mtime_nsec(),
        changed_seconds: metadata.ctime(),
        changed_nanoseconds: metadata.ctime_nsec(),
    })
}

#[cfg(target_os = "macos")]
fn write_anchored_file(
    destination_root: std::os::fd::RawFd,
    destination_relative: &Path,
    payload: &[u8],
) -> Result<()> {
    use std::io::Write;
    use std::os::fd::FromRawFd;
    use std::os::unix::ffi::OsStrExt;

    validate_relative_path(&destination_relative.to_string_lossy())?;
    let components = destination_relative.components().collect::<Vec<_>>();
    let mut destination_dir = duplicate_fd(destination_root)?;
    for component in &components[..components.len().saturating_sub(1)] {
        let Component::Normal(name) = component else {
            bail!("unsafe runtime overlay component");
        };
        destination_dir = mkdirat_open(destination_dir, name)?;
    }
    let name = components
        .last()
        .and_then(|component| match component {
            Component::Normal(name) => Some(name),
            _ => None,
        })
        .context("empty runtime overlay path")?;
    let name = std::ffi::CString::new(name.as_bytes())?;
    let fd = unsafe {
        libc::openat(
            destination_dir,
            name.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o444,
        )
    };
    unsafe { libc::close(destination_dir) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error()).context("create anchored runtime overlay");
    }
    let mut destination = unsafe { File::from_raw_fd(fd) };
    destination.write_all(payload)?;
    destination.flush()?;
    let identity = snapshot_file_identity(destination_relative, &destination)?;
    if identity.size != payload.len() as u64 || identity.links != 1 || identity.mode != 0o444 {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] runtime overlay metadata rejected");
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn duplicate_fd(fd: std::os::fd::RawFd) -> Result<std::os::fd::RawFd> {
    let duplicate = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) };
    if duplicate < 0 {
        return Err(std::io::Error::last_os_error()).context("duplicate snapshot root handle");
    }
    Ok(duplicate)
}

#[cfg(target_os = "macos")]
fn openat_directory(parent: std::os::fd::RawFd, name: &OsStr) -> Result<std::os::fd::RawFd> {
    use std::os::unix::ffi::OsStrExt;
    let name = std::ffi::CString::new(name.as_bytes())?;
    let child = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    unsafe { libc::close(parent) };
    if child < 0 {
        return Err(std::io::Error::last_os_error()).context("open anchored snapshot directory");
    }
    Ok(child)
}

#[cfg(target_os = "macos")]
fn mkdirat_open(parent: std::os::fd::RawFd, name: &OsStr) -> Result<std::os::fd::RawFd> {
    use std::os::unix::ffi::OsStrExt;
    let name = std::ffi::CString::new(name.as_bytes())?;
    let created = unsafe { libc::mkdirat(parent, name.as_ptr(), 0o700) };
    if created != 0 && std::io::Error::last_os_error().raw_os_error() != Some(libc::EEXIST) {
        unsafe { libc::close(parent) };
        return Err(std::io::Error::last_os_error()).context("create anchored snapshot directory");
    }
    openat_directory(parent, OsStr::from_bytes(name.as_bytes()))
}

#[cfg(target_os = "macos")]
fn seal_snapshot_directories(
    root: std::os::fd::RawFd,
    manifest: &SealedEnvironmentManifest,
) -> Result<()> {
    use std::collections::BTreeSet;
    use std::os::fd::AsRawFd;
    let mut directories = BTreeSet::new();
    for file in &manifest.files {
        let mut parent = Path::new(&file.path).parent();
        while let Some(value) = parent.filter(|value| !value.as_os_str().is_empty()) {
            directories.insert(value.to_path_buf());
            parent = value.parent();
        }
    }
    for relative in directories.iter().rev() {
        let directory = openat_relative_directory(root, relative)?;
        if unsafe { libc::fchmod(directory.as_raw_fd(), 0o555) } != 0 {
            return Err(std::io::Error::last_os_error()).context("seal snapshot directory");
        }
    }
    if unsafe { libc::fchmod(root, 0o555) } != 0 {
        return Err(std::io::Error::last_os_error()).context("seal snapshot root");
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn openat_relative_directory(root: std::os::fd::RawFd, relative: &Path) -> Result<File> {
    use std::os::fd::FromRawFd;

    validate_relative_path(&relative.to_string_lossy())?;
    let mut directory = duplicate_fd(root)?;
    for component in relative.components() {
        let Component::Normal(name) = component else {
            unsafe { libc::close(directory) };
            bail!("unsafe snapshot directory component");
        };
        directory = openat_directory(directory, name)?;
    }
    Ok(unsafe { File::from_raw_fd(directory) })
}

#[cfg(target_os = "macos")]
fn openat_relative_regular(root: std::os::fd::RawFd, relative: &Path) -> Result<File> {
    use std::os::fd::FromRawFd;
    use std::os::unix::ffi::OsStrExt;

    validate_relative_path(&relative.to_string_lossy())?;
    let parent = relative
        .parent()
        .filter(|value| !value.as_os_str().is_empty());
    let directory = if let Some(parent) = parent {
        let parent = openat_relative_directory(root, parent)?;
        use std::os::fd::IntoRawFd;
        parent.into_raw_fd()
    } else {
        duplicate_fd(root)?
    };
    let name = std::ffi::CString::new(
        relative
            .file_name()
            .context("empty anchored file path")?
            .as_bytes(),
    )?;
    let fd = unsafe {
        libc::openat(
            directory,
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    unsafe { libc::close(directory) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error()).context("open anchored regular file");
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

#[cfg(target_os = "macos")]
fn authenticate_snapshot_file(
    root: std::os::fd::RawFd,
    relative: &Path,
    expected_size: u64,
    expected_sha256: &str,
    expected_mode: u32,
) -> Result<SnapshotFileIdentity> {
    let mut file = openat_relative_regular(root, relative)?;
    let before = snapshot_file_identity(relative, &file)?;
    if before.size != expected_size || before.links != 1 || before.mode != expected_mode {
        bail!(
            "[PYTHON_SEALED_SNAPSHOT_INVALID] anchored metadata changed: {}",
            relative.display()
        );
    }
    let mut digest = Sha256::new();
    let mut authenticated = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
        authenticated = authenticated.saturating_add(count as u64);
        if authenticated > expected_size {
            bail!(
                "[PYTHON_SEALED_SNAPSHOT_INVALID] anchored file grew: {}",
                relative.display()
            );
        }
    }
    let after = snapshot_file_identity(relative, &file)?;
    if before != after {
        bail!(
            "[PYTHON_SEALED_SNAPSHOT_INVALID] anchored file changed while authenticating: {}",
            relative.display()
        );
    }
    if authenticated != expected_size || hex::encode(digest.finalize()) != expected_sha256 {
        bail!(
            "[PYTHON_SEALED_SNAPSHOT_INVALID] anchored digest changed: {}",
            relative.display()
        );
    }
    Ok(after)
}

#[cfg(target_os = "macos")]
fn authenticate_snapshot_anchored(
    root: std::os::fd::RawFd,
    manifest: &SealedEnvironmentManifest,
    expected_manifest_size: u64,
    expected_manifest_digest: &str,
    runtime_overlay: Option<&VerifiedRuntimeOverlay>,
) -> Result<SnapshotVerification> {
    let mut files = Vec::with_capacity(manifest.files.len() + 2);
    files.push(authenticate_snapshot_file(
        root,
        Path::new(MANIFEST_FILENAME),
        expected_manifest_size,
        expected_manifest_digest,
        0o444,
    )?);
    for entry in &manifest.files {
        files.push(authenticate_snapshot_file(
            root,
            Path::new(&entry.path),
            entry.size,
            &entry.sha256,
            if entry.executable { 0o555 } else { 0o444 },
        )?);
    }
    if let Some(overlay) = runtime_overlay {
        files.push(authenticate_snapshot_file(
            root,
            Path::new(SNAPSHOT_RUNTIME_MANIFEST),
            overlay.bytes.len() as u64,
            &overlay.sha256,
            0o444,
        )?);
    }
    let verification = SnapshotVerification { files };
    verify_snapshot_anchored(root, manifest, &verification, runtime_overlay)?;
    Ok(verification)
}

#[cfg(target_os = "macos")]
fn verify_snapshot_anchored(
    root: std::os::fd::RawFd,
    manifest: &SealedEnvironmentManifest,
    verification: &SnapshotVerification,
    runtime_overlay: Option<&VerifiedRuntimeOverlay>,
) -> Result<()> {
    use std::collections::BTreeSet;
    use std::os::unix::fs::PermissionsExt;

    let mut expected_identity_paths = manifest
        .files
        .iter()
        .map(|entry| entry.path.clone())
        .chain(std::iter::once(MANIFEST_FILENAME.to_owned()))
        .collect::<Vec<_>>();
    if runtime_overlay.is_some() {
        expected_identity_paths.push(SNAPSHOT_RUNTIME_MANIFEST.to_owned());
    }
    expected_identity_paths.sort();
    let mut identity_paths = verification
        .files
        .iter()
        .map(|identity| identity.path.clone())
        .collect::<Vec<_>>();
    identity_paths.sort();
    if identity_paths != expected_identity_paths {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] snapshot identity closure changed");
    }
    for expected in &verification.files {
        let file = openat_relative_regular(root, Path::new(&expected.path))?;
        let actual = snapshot_file_identity(Path::new(&expected.path), &file)?;
        if &actual != expected {
            bail!(
                "[PYTHON_SEALED_SNAPSHOT_INVALID] anchored file identity changed: {}",
                expected.path
            );
        }
    }
    let mut directories = BTreeSet::new();
    for entry in &manifest.files {
        let mut parent = Path::new(&entry.path).parent();
        while let Some(value) = parent.filter(|value| !value.as_os_str().is_empty()) {
            directories.insert(value.to_path_buf());
            parent = value.parent();
        }
    }
    for directory in &directories {
        let handle = openat_relative_directory(root, directory)?;
        let metadata = handle.metadata()?;
        if !metadata.is_dir() || metadata.permissions().mode() & 0o777 != 0o555 {
            bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] anchored directory changed");
        }
    }
    let root_metadata = unsafe {
        let mut value = std::mem::MaybeUninit::<libc::stat>::zeroed();
        if libc::fstat(root, value.as_mut_ptr()) != 0 {
            return Err(std::io::Error::last_os_error()).context("inspect snapshot root handle");
        }
        value.assume_init()
    };
    if root_metadata.st_mode & 0o777 != 0o555 {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] anchored root mode changed");
    }
    let mut actual_files = Vec::new();
    let mut actual_directories = Vec::new();
    collect_anchored_inventory(
        root,
        Path::new(""),
        &mut actual_files,
        &mut actual_directories,
    )?;
    actual_files.sort();
    actual_directories.sort();
    let mut expected_files = manifest
        .files
        .iter()
        .map(|entry| entry.path.clone())
        .collect::<Vec<_>>();
    if runtime_overlay.is_some() {
        expected_files.push(SNAPSHOT_RUNTIME_MANIFEST.to_owned());
        expected_files.sort();
    }
    let expected_directories = portable_directory_inventory(directories);
    if actual_files != expected_files || actual_directories != expected_directories {
        let missing_file = expected_files
            .iter()
            .find(|path| actual_files.binary_search(path).is_err());
        let extra_file = actual_files
            .iter()
            .find(|path| expected_files.binary_search(path).is_err());
        let missing_directory = expected_directories
            .iter()
            .find(|path| actual_directories.binary_search(path).is_err());
        let extra_directory = actual_directories
            .iter()
            .find(|path| expected_directories.binary_search(path).is_err());
        bail!(
            "[PYTHON_SEALED_SNAPSHOT_INVALID] anchored inventory has missing or extra entries \
             (files actual={} expected={}, first missing={missing_file:?}, first extra={extra_file:?}; \
             directories actual={} expected={}, first missing={missing_directory:?}, first extra={extra_directory:?})",
            actual_files.len(),
            expected_files.len(),
            actual_directories.len(),
            expected_directories.len(),
        );
    }
    Ok(())
}

fn portable_directory_inventory(directories: impl IntoIterator<Item = PathBuf>) -> Vec<String> {
    let mut portable = directories
        .into_iter()
        .map(|path| path.to_string_lossy().replace('\\', "/"))
        .collect::<Vec<_>>();
    // PathBuf orders by components, whereas the anchored inventory contains
    // flattened portable strings.  Canonicalize in the representation that is
    // compared so equal inventories cannot fail solely due to ordering.
    portable.sort();
    portable
}

#[cfg(target_os = "macos")]
fn collect_anchored_inventory(
    directory: std::os::fd::RawFd,
    relative: &Path,
    files: &mut Vec<String>,
    directories: &mut Vec<String>,
) -> Result<()> {
    use std::ffi::{CStr, OsString};
    use std::os::unix::ffi::{OsStrExt, OsStringExt};

    // `dup` would share the directory offset with the retained root lease.
    // Open `.` relative to the anchored descriptor so every inventory pass has
    // an independent file description and repeated revalidation starts at the
    // beginning of the directory.
    let dot = c".";
    let enumeration = unsafe {
        libc::openat(
            directory,
            dot.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if enumeration < 0 {
        return Err(std::io::Error::last_os_error()).context("open anchored inventory directory");
    }
    let stream = unsafe { libc::fdopendir(enumeration) };
    if stream.is_null() {
        unsafe { libc::close(enumeration) };
        return Err(std::io::Error::last_os_error()).context("enumerate anchored snapshot");
    }
    let result = (|| {
        loop {
            unsafe { *libc::__error() = 0 };
            let entry = unsafe { libc::readdir(stream) };
            if entry.is_null() {
                let error = std::io::Error::last_os_error();
                if error.raw_os_error() != Some(0) {
                    return Err(error).context("read anchored snapshot directory");
                }
                break;
            }
            let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
            if name == b"." || name == b".." {
                continue;
            }
            let name = OsString::from_vec(name.to_vec());
            let name_c = std::ffi::CString::new(name.as_bytes())?;
            let mut stat = std::mem::MaybeUninit::<libc::stat>::zeroed();
            if unsafe {
                libc::fstatat(
                    directory,
                    name_c.as_ptr(),
                    stat.as_mut_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            } != 0
            {
                return Err(std::io::Error::last_os_error())
                    .context("inspect anchored snapshot entry");
            }
            let stat = unsafe { stat.assume_init() };
            let path = relative.join(&name);
            if stat.st_mode & libc::S_IFMT == libc::S_IFDIR {
                directories.push(path.to_string_lossy().replace('\\', "/"));
                let child = openat_directory(duplicate_fd(directory)?, &name)?;
                let nested = collect_anchored_inventory(child, &path, files, directories);
                unsafe { libc::close(child) };
                nested?;
            } else if stat.st_mode & libc::S_IFMT == libc::S_IFREG {
                if path != Path::new(MANIFEST_FILENAME) {
                    files.push(path.to_string_lossy().replace('\\', "/"));
                }
            } else {
                bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] anchored tree contains a link or special file");
            }
        }
        Ok(())
    })();
    unsafe { libc::closedir(stream) };
    result
}

fn validate_manifest_contract(manifest: &SealedEnvironmentManifest) -> Result<()> {
    if manifest.schema != MANIFEST_SCHEMA
        || manifest.platform != std::env::consts::OS
        || normalize_architecture(&manifest.architecture)
            != normalize_architecture(std::env::consts::ARCH)
        || manifest.package_provenance.kind != required_package_provenance_kind()
        || manifest.package_provenance.package_id != "dev.rumiai.app"
    {
        bail!("[PYTHON_SEALED_INVALID] sealed Python platform/package contract mismatch");
    }
    let version_parts = manifest.python_version.split('.').collect::<Vec<_>>();
    if version_parts.len() != 3
        || version_parts
            .iter()
            .any(|part| part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()))
    {
        bail!("[PYTHON_SEALED_INVALID] Python version is not an exact patch version");
    }
    for digest in [
        &manifest.environment_digest,
        &manifest.package_provenance.release_digest,
        &manifest.sentinels.stdlib_sha256,
        &manifest.sentinels.site_packages_sha256,
        &manifest.sentinels.native_sha256,
    ] {
        require_sha256(digest)?;
    }
    let mut sorted = manifest.files.clone();
    sorted.sort_by(|left, right| left.path.cmp(&right.path));
    if sorted != manifest.files {
        bail!("[PYTHON_SEALED_INVALID] sealed file inventory is not sorted");
    }
    let mut unique = HashSet::new();
    for file in &manifest.files {
        validate_relative_path(&file.path)?;
        require_sha256(&file.sha256)?;
        if !unique.insert(file.path.as_str()) {
            bail!("[PYTHON_SEALED_INVALID] duplicate sealed file path");
        }
    }
    for required in required_layout_paths() {
        if !unique.contains(required) {
            bail!("[PYTHON_SEALED_INVALID] missing fixed sealed path {required}");
        }
    }
    if !unique.contains(DIRECTORY_MODES_FILENAME) {
        bail!("[PYTHON_SEALED_INVALID] missing sealed directory mode evidence");
    }
    let interpreter = if cfg!(windows) {
        "venv/Scripts/python.exe"
    } else {
        "venv/bin/python3"
    };
    if manifest
        .files
        .iter()
        .find(|file| file.path == interpreter)
        .is_none_or(|file| !file.executable)
    {
        bail!("[PYTHON_SEALED_INVALID] fixed interpreter is not executable");
    }
    let bootstrap = fixed_bootstrap_path(&manifest.python_version)?;
    if !unique.contains(bootstrap.as_str()) {
        bail!("[PYTHON_SEALED_INVALID] missing fixed sealed bootstrap {bootstrap}");
    }
    let inventory_digest = sha256_bytes(&serde_json::to_vec(&manifest.files)?);
    if inventory_digest != manifest.environment_digest {
        bail!("[PYTHON_SEALED_INVALID] environment digest does not match file inventory");
    }
    Ok(())
}

fn required_package_provenance_kind() -> &'static str {
    if cfg!(target_os = "macos") {
        "pinned-python-build-standalone-v1"
    } else if cfg!(windows) {
        "windows-authenticode-v1"
    } else {
        "linux-immutable-package-v1"
    }
}

fn verify_environment_tree(root: &Path, manifest: &SealedEnvironmentManifest) -> Result<()> {
    let metadata = fs::symlink_metadata(root)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        bail!("[PYTHON_SEALED_INVALID] environment root is linked or missing");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o777 != 0o555 {
            bail!("[PYTHON_SEALED_INVALID] environment root mode changed");
        }
    }
    let mut actual = Vec::new();
    let mut actual_directories = Vec::new();
    collect_files(root, root, &mut actual, &mut actual_directories)?;
    actual.sort();
    actual_directories.sort();
    let expected = manifest
        .files
        .iter()
        .map(|file| file.path.clone())
        .collect::<Vec<_>>();
    if actual != expected {
        bail!("[PYTHON_SEALED_INVALID] environment has missing or extra files");
    }
    let mut expected_directories = HashSet::new();
    for file in &manifest.files {
        let mut parent = Path::new(&file.path).parent();
        while let Some(directory) = parent.filter(|directory| !directory.as_os_str().is_empty()) {
            expected_directories.insert(directory.to_string_lossy().replace('\\', "/"));
            parent = directory.parent();
        }
    }
    let mut expected_directories = expected_directories.into_iter().collect::<Vec<_>>();
    expected_directories.sort();
    if actual_directories != expected_directories {
        bail!("[PYTHON_SEALED_INVALID] environment has missing or extra directories");
    }
    verify_directory_mode_evidence(root, &actual_directories)?;
    for entry in &manifest.files {
        let path = root.join(&entry.path);
        let metadata = fs::symlink_metadata(&path)?;
        if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() != entry.size
        {
            bail!(
                "[PYTHON_SEALED_INVALID] sealed file metadata changed: {}",
                entry.path
            );
        }
        if has_multiple_links(&path, &metadata)? {
            bail!(
                "[PYTHON_SEALED_INVALID] sealed file is multiply linked: {}",
                entry.path
            );
        }
        if sha256_bytes(&read_bounded_regular(&path, entry.size.saturating_add(1))?) != entry.sha256
        {
            bail!(
                "[PYTHON_SEALED_INVALID] sealed file digest changed: {}",
                entry.path
            );
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let executable = metadata.permissions().mode() & 0o111 != 0;
            let expected_mode = if entry.executable { 0o555 } else { 0o444 };
            if executable != entry.executable
                || metadata.permissions().mode() & 0o777 != expected_mode
            {
                bail!(
                    "[PYTHON_SEALED_INVALID] sealed file permissions changed: {}",
                    entry.path
                );
            }
        }
    }
    Ok(())
}

fn verify_directory_mode_evidence(root: &Path, directories: &[String]) -> Result<()> {
    let evidence_path = root.join(DIRECTORY_MODES_FILENAME);
    let evidence: SealedDirectoryModes =
        serde_json::from_slice(&read_bounded_regular(&evidence_path, 4 * 1024 * 1024)?)
            .context("[PYTHON_SEALED_INVALID] malformed sealed directory mode evidence")?;
    let expected = SealedDirectoryModes {
        schema: DIRECTORY_MODES_SCHEMA.to_string(),
        directories: std::iter::once(SealedDirectoryMode {
            path: ".".to_string(),
            mode: "0555".to_string(),
        })
        .chain(directories.iter().map(|path| SealedDirectoryMode {
            path: path.clone(),
            mode: "0555".to_string(),
        }))
        .collect(),
    };
    if evidence != expected {
        bail!("[PYTHON_SEALED_INVALID] sealed directory mode evidence changed");
    }
    #[cfg(unix)]
    for entry in &evidence.directories {
        use std::os::unix::fs::PermissionsExt;
        let path = if entry.path == "." {
            root.to_path_buf()
        } else {
            root.join(&entry.path)
        };
        let metadata = fs::symlink_metadata(&path)?;
        if !metadata.is_dir()
            || metadata.file_type().is_symlink()
            || metadata.permissions().mode() & 0o777 != 0o555
        {
            bail!(
                "[PYTHON_SEALED_INVALID] sealed directory mode changed: {}",
                entry.path
            );
        }
    }
    Ok(())
}

#[cfg(unix)]
fn has_multiple_links(_path: &Path, metadata: &fs::Metadata) -> Result<bool> {
    use std::os::unix::fs::MetadataExt;
    Ok(metadata.nlink() != 1)
}

#[cfg(windows)]
fn has_multiple_links(path: &Path, _metadata: &fs::Metadata) -> Result<bool> {
    use std::mem::MaybeUninit;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };
    let file = open_regular(path)?;
    let mut information = MaybeUninit::<BY_HANDLE_FILE_INFORMATION>::zeroed();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), information.as_mut_ptr()) } == 0 {
        return Err(std::io::Error::last_os_error()).context("inspect sealed file link count");
    }
    Ok(unsafe { information.assume_init() }.nNumberOfLinks != 1)
}

#[cfg(not(any(unix, windows)))]
fn has_multiple_links(_path: &Path, _metadata: &fs::Metadata) -> Result<bool> {
    bail!("sealed file link-count inspection is unavailable")
}

fn wait_for_attestation(
    child: &mut Child,
    path: &Path,
    nonce: &str,
    role: PythonRole,
    verified: &VerifiedEnvironment,
) -> Result<()> {
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    loop {
        match fs::symlink_metadata(path) {
            Ok(metadata) => {
                if metadata.file_type().is_symlink() || !metadata.is_file() {
                    bail!(
                        "[PYTHON_SEALED_ATTESTATION_INVALID] attestation is not a private regular file"
                    );
                }
                // Bootstrap's no-replace publication has two names until the
                // temporary name is unlinked.  Only nlink=1 is the completed,
                // versioned attestation-file lifecycle.
                if has_multiple_links(path, &metadata)? {
                    if let Some(status) = child.try_wait()? {
                        bail!("[PYTHON_SEALED_ATTESTATION_MISSING] sealed Python exited during attestation publication: {status}");
                    }
                    if Instant::now() >= deadline {
                        bail!("[PYTHON_SEALED_ATTESTATION_INVALID] attestation publication did not complete");
                    }
                    thread::sleep(Duration::from_millis(20));
                    continue;
                }
                let bytes = read_attestation_file(path)?;
                let attestation: StartupAttestation = serde_json::from_slice(&bytes)
                    .context("[PYTHON_SEALED_ATTESTATION_INVALID] malformed startup attestation")?;
                validate_attestation(&attestation, nonce, role, verified)?;
                return Ok(());
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error)
                    .context("[PYTHON_SEALED_ATTESTATION_INVALID] inspect startup attestation")
            }
        }
        if let Some(status) = child.try_wait()? {
            bail!("[PYTHON_SEALED_ATTESTATION_MISSING] sealed Python exited before attestation: {status}");
        }
        if Instant::now() >= deadline {
            bail!(
                "[PYTHON_SEALED_ATTESTATION_TIMEOUT] sealed Python did not attest before deadline"
            );
        }
        thread::sleep(Duration::from_millis(20));
    }
}

fn validate_attestation_file(path: &Path, metadata: &fs::Metadata) -> Result<()> {
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || has_multiple_links(path, metadata)?
    {
        bail!("[PYTHON_SEALED_ATTESTATION_INVALID] attestation is not a private regular file");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        if metadata.uid() != unsafe { libc::geteuid() }
            || metadata.permissions().mode() & 0o777 != 0o600
        {
            bail!("[PYTHON_SEALED_ATTESTATION_INVALID] attestation owner or permissions changed");
        }
    }
    Ok(())
}

fn read_attestation_file(path: &Path) -> Result<Vec<u8>> {
    let before = fs::symlink_metadata(path)?;
    validate_attestation_file(path, &before)?;
    let mut file = open_regular(path)?;
    let opened = file.metadata()?;
    if !same_attestation_identity(&before, &opened) {
        bail!("[PYTHON_SEALED_ATTESTATION_INVALID] attestation changed while opened");
    }
    let mut bytes = Vec::with_capacity(opened.len() as usize);
    (&mut file)
        .take(MAX_ATTESTATION_BYTES.saturating_add(1))
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 != opened.len() || bytes.len() as u64 > MAX_ATTESTATION_BYTES {
        bail!("[PYTHON_SEALED_ATTESTATION_INVALID] attestation changed while read");
    }
    let after_handle = file.metadata()?;
    let after_path = fs::symlink_metadata(path)?;
    validate_attestation_file(path, &after_path)?;
    if !same_attestation_identity(&opened, &after_handle)
        || !same_attestation_identity(&opened, &after_path)
    {
        bail!("[PYTHON_SEALED_ATTESTATION_INVALID] attestation changed after read");
    }
    Ok(bytes)
}

#[cfg(unix)]
fn same_attestation_identity(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    left.dev() == right.dev()
        && left.ino() == right.ino()
        && left.len() == right.len()
        && left.mtime() == right.mtime()
        && left.mtime_nsec() == right.mtime_nsec()
        && left.permissions().mode() == right.permissions().mode()
        && left.nlink() == right.nlink()
        && left.uid() == right.uid()
}

#[cfg(windows)]
fn same_attestation_identity(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    left.volume_serial_number() == right.volume_serial_number()
        && left.file_index() == right.file_index()
        && left.number_of_links() == right.number_of_links()
        && left.file_size() == right.file_size()
        && left.last_write_time() == right.last_write_time()
        && left.file_attributes() == right.file_attributes()
}

#[cfg(not(any(unix, windows)))]
fn same_attestation_identity(_left: &fs::Metadata, _right: &fs::Metadata) -> bool {
    false
}

fn validate_attestation(
    value: &StartupAttestation,
    nonce: &str,
    role: PythonRole,
    verified: &VerifiedEnvironment,
) -> Result<()> {
    let root = fs::canonicalize(&verified.root)?;
    let executable = fs::canonicalize(fixed_interpreter(&verified.root))?;
    let prefix = fs::canonicalize(verified.root.join(fixed_venv_prefix()))?;
    let base_prefix = fs::canonicalize(verified.root.join("runtime"))?;
    if value.schema != ATTESTATION_SCHEMA
        || value.nonce != nonce
        || value.role != role.name()
        || value.environment_digest != verified.manifest.environment_digest
        || fs::canonicalize(&value.executable).ok().as_deref() != Some(executable.as_path())
        || fs::canonicalize(&value.prefix).ok().as_deref() != Some(prefix.as_path())
        || fs::canonicalize(&value.base_prefix).ok().as_deref() != Some(base_prefix.as_path())
        || !value.lifetime_lease
        || value.stdlib_sha256 != verified.manifest.sentinels.stdlib_sha256
        || value.site_packages_sha256 != verified.manifest.sentinels.site_packages_sha256
        || value.native_sha256 != verified.manifest.sentinels.native_sha256
        || value.runtime_overlay_sha256 != verified.runtime_overlay.sha256
        || value.outer_runtime_manifest_sha256
            != verified.runtime_overlay.authority.outer_manifest_sha256
    {
        bail!("[PYTHON_SEALED_ATTESTATION_INVALID] startup identity mismatch");
    }
    let expected_sys_path = expected_attested_sys_path(verified, role)?;
    let mut actual_sys_path = HashSet::new();
    for path in &value.sys_path {
        let Ok(path) = fs::canonicalize(path) else {
            bail!("[PYTHON_SEALED_ATTESTATION_INVALID] sys.path identity is invalid");
        };
        if !actual_sys_path.insert(path) {
            bail!("[PYTHON_SEALED_ATTESTATION_INVALID] sys.path identity is invalid");
        }
    }
    if actual_sys_path != expected_sys_path {
        bail!("[PYTHON_SEALED_ATTESTATION_INVALID] sys.path escaped sealed environment");
    }
    Ok(())
}

fn expected_attested_sys_path(
    verified: &VerifiedEnvironment,
    role: PythonRole,
) -> Result<HashSet<PathBuf>> {
    let mut version = verified.manifest.python_version.split('.');
    let major = version.next().unwrap_or_default();
    let minor = version.next().unwrap_or_default();
    if major.is_empty() || minor.is_empty() {
        bail!("[PYTHON_SEALED_ATTESTATION_INVALID] Python version is invalid");
    }
    let (zip, mut directories) = if cfg!(windows) {
        (
            format!("runtime/python{major}{minor}.zip"),
            vec![
                "runtime".to_string(),
                "runtime/Lib".to_string(),
                "runtime/DLLs".to_string(),
                "venv/Lib/site-packages".to_string(),
                "app".to_string(),
            ],
        )
    } else {
        (
            format!("runtime/lib/python{major}{minor}.zip"),
            vec![
                format!("runtime/lib/python{major}.{minor}"),
                format!("runtime/lib/python{major}.{minor}/lib-dynload"),
                format!("venv/lib/python{major}.{minor}/site-packages"),
                "app".to_string(),
            ],
        )
    };
    if role == PythonRole::Defaultspack {
        directories.push("app/ecosystem/defaultspack".to_string());
    }
    let file_paths = verified
        .manifest
        .files
        .iter()
        .map(|entry| entry.path.as_str())
        .collect::<HashSet<_>>();
    let mut directory_paths = HashSet::new();
    for entry in &verified.manifest.files {
        let mut parent = Path::new(&entry.path).parent();
        while let Some(path) = parent {
            if path.as_os_str().is_empty() {
                break;
            }
            directory_paths.insert(path.to_string_lossy().into_owned());
            parent = path.parent();
        }
    }
    if directories
        .iter()
        .any(|relative| !directory_paths.contains(relative))
    {
        bail!("[PYTHON_SEALED_ATTESTATION_INVALID] manifest omits an import root");
    }
    let mut expected = directories
        .into_iter()
        .map(|relative| fs::canonicalize(verified.root.join(relative)))
        .collect::<io::Result<HashSet<_>>>()?;
    if file_paths.contains(zip.as_str()) {
        expected.insert(fs::canonicalize(verified.root.join(zip))?);
    }
    Ok(expected)
}

/// Prove that the bootstrap holds the environment lease before the child is
/// returned. The bootstrap contract requires retaining this lock until exit;
/// package replacement takes the exclusive side of the same lock.
fn verify_child_lifetime_lease(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        let file = open_regular(path)?;
        let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        if result == 0 {
            unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) };
            bail!("[PYTHON_SEALED_LEASE_MISSING] bootstrap did not retain the environment lease");
        }
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::EWOULDBLOCK) {
            return Err(error).context("[PYTHON_SEALED_LEASE_INVALID] could not test child lease");
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        match OpenOptions::new()
            .read(true)
            .write(true)
            .share_mode(0)
            .open(path)
        {
            Ok(_) => bail!(
                "[PYTHON_SEALED_LEASE_MISSING] bootstrap did not retain the environment lease"
            ),
            Err(error) if error.raw_os_error() == Some(32) => {}
            Err(error) => {
                return Err(error)
                    .context("[PYTHON_SEALED_LEASE_INVALID] could not test child lease")
            }
        }
    }
    Ok(())
}

fn acquire_environment_lease(path: &Path) -> Result<File> {
    let file = open_regular(path)?;
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_SH | libc::LOCK_NB) } != 0 {
            return Err(std::io::Error::last_os_error())
                .context("[PYTHON_SEALED_LEASE_BUSY] environment replacement is active");
        }
    }
    Ok(file)
}

fn prepare_attestation_path(config: &AppConfig, nonce: &str) -> Result<PathBuf> {
    let root = config
        .user_data_dir
        .parent()
        .unwrap_or(&config.user_data_dir)
        .join(".sealed-python-attestation");
    if let Ok(metadata) = fs::symlink_metadata(&root) {
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            bail!("sealed Python attestation root is unsafe");
        }
    } else {
        fs::create_dir(&root)?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
    }
    let path = root.join(format!("startup-{nonce}.json"));
    if path.exists() {
        bail!("sealed Python attestation nonce collided");
    }
    Ok(path)
}

fn collect_files(
    root: &Path,
    current: &Path,
    output: &mut Vec<String>,
    directories: &mut Vec<String>,
) -> Result<()> {
    for entry in fs::read_dir(current)? {
        let entry = entry?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() {
            bail!("[PYTHON_SEALED_INVALID] sealed tree contains a symlink");
        }
        if metadata.is_dir() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                if metadata.permissions().mode() & 0o022 != 0 {
                    bail!("[PYTHON_SEALED_INVALID] sealed directory is writable");
                }
            }
            directories.push(
                path.strip_prefix(root)?
                    .to_string_lossy()
                    .replace('\\', "/"),
            );
            collect_files(root, &path, output, directories)?;
        } else if metadata.is_file() {
            // The manifest authenticates the rest of the tree and is therefore
            // intentionally absent from its own file inventory.  It remains a
            // required regular file; only omit it from `output`, not from the
            // accepted file-type branch.
            if path.strip_prefix(root)? != Path::new(MANIFEST_FILENAME) {
                output.push(
                    path.strip_prefix(root)?
                        .to_string_lossy()
                        .replace('\\', "/"),
                );
            }
        } else {
            bail!("[PYTHON_SEALED_INVALID] sealed tree contains a special file");
        }
    }
    Ok(())
}

fn validate_relative_path(value: &str) -> Result<()> {
    let path = Path::new(value);
    if value.contains('\\')
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        bail!("[PYTHON_SEALED_INVALID] unsafe sealed file path {value:?}");
    }
    Ok(())
}

fn read_bounded_regular(path: &Path, limit: u64) -> Result<Vec<u8>> {
    let before = fs::symlink_metadata(path)?;
    if before.file_type().is_symlink()
        || !before.is_file()
        || before.len() > limit
        || has_multiple_links(path, &before)?
    {
        bail!(
            "refusing unsafe or oversized sealed file {}",
            path.display()
        );
    }
    let mut file = open_regular(path)?;
    let opened = file.metadata()?;
    if !same_attestation_identity(&before, &opened) {
        bail!("sealed file changed while opened {}", path.display());
    }
    let mut bytes = Vec::with_capacity(opened.len() as usize);
    (&mut file)
        .take(limit.saturating_add(1))
        .read_to_end(&mut bytes)?;
    let after_handle = file.metadata()?;
    let after_path = fs::symlink_metadata(path)?;
    if bytes.len() as u64 != opened.len()
        || !same_attestation_identity(&opened, &after_handle)
        || !same_attestation_identity(&opened, &after_path)
    {
        bail!("sealed file changed while reading {}", path.display());
    }
    Ok(bytes)
}

fn open_regular(path: &Path) -> Result<File> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        Ok(OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)?)
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        return Ok(OpenOptions::new()
            .read(true)
            .share_mode(1)
            .custom_flags(0x0020_0000)
            .open(path)?);
    }
    #[cfg(not(any(unix, windows)))]
    Ok(File::open(path)?)
}

fn open_directory(path: &Path) -> Result<File> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        Ok(OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_DIRECTORY)
            .open(path)?)
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        return Ok(OpenOptions::new()
            .read(true)
            .share_mode(1)
            .custom_flags(0x0200_0000 | 0x0020_0000)
            .open(path)?);
    }
    #[cfg(not(any(unix, windows)))]
    Ok(File::open(path)?)
}

fn fixed_interpreter(root: &Path) -> PathBuf {
    if cfg!(windows) {
        root.join("venv/Scripts/python.exe")
    } else {
        root.join("venv/bin/python3")
    }
}

fn fixed_venv_prefix() -> &'static str {
    "venv"
}

fn fixed_bootstrap_path(python_version: &str) -> Result<String> {
    let mut parts = python_version.split('.');
    let major = parts.next().unwrap_or_default();
    let minor = parts.next().unwrap_or_default();
    if major.is_empty() || minor.is_empty() {
        bail!("invalid Python version for bootstrap path");
    }
    Ok(if cfg!(windows) {
        "venv/Lib/site-packages/tobkiri_sealed/bootstrap.py".into()
    } else {
        format!("venv/lib/python{major}.{minor}/site-packages/tobkiri_sealed/bootstrap.py")
    })
}

fn required_layout_paths() -> &'static [&'static str] {
    if cfg!(windows) {
        &[
            DIRECTORY_MODES_FILENAME,
            "venv/Scripts/python.exe",
            "app/kernel_entry.py",
            "app/defaultspack_entry.py",
            "app/host_helper_entry.py",
            "lease.v1",
            "sentinels/stdlib.sha256",
            "sentinels/site-packages.sha256",
            "sentinels/native.sha256",
        ]
    } else {
        &[
            DIRECTORY_MODES_FILENAME,
            "venv/bin/python3",
            "app/kernel_entry.py",
            "app/defaultspack_entry.py",
            "app/host_helper_entry.py",
            "lease.v1",
            "sentinels/stdlib.sha256",
            "sentinels/site-packages.sha256",
            "sentinels/native.sha256",
        ]
    }
}

fn require_sha256(value: &str) -> Result<()> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        bail!("invalid SHA-256 identity");
    }
    Ok(())
}

fn sha256_bytes(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

fn random_nonce() -> String {
    let mut nonce = [0_u8; 32];
    rand::thread_rng().fill_bytes(&mut nonce);
    hex::encode(nonce)
}

fn normalize_architecture(value: &str) -> &str {
    match value {
        "arm64" => "aarch64",
        "amd64" => "x86_64",
        other => other,
    }
}

#[cfg(target_os = "macos")]
fn verify_package_provenance(config: &AppConfig, provenance: &PackageProvenance) -> Result<()> {
    if provenance.kind != required_package_provenance_kind() {
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] packaged Python provenance kind mismatch");
    }
    let bundle = config
        .app_dir
        .ancestors()
        .find(|path| path.extension().is_some_and(|extension| extension == "app"))
        .ok_or_else(|| {
            anyhow::anyhow!("[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] app bundle root not found")
        })?;
    verify_macos_static_code(bundle)
}

#[cfg(not(target_os = "macos"))]
fn verify_package_provenance(_config: &AppConfig, provenance: &PackageProvenance) -> Result<()> {
    let required = required_package_provenance_kind();
    if provenance.kind != required {
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] packaged Python provenance kind mismatch");
    }
    bail!("[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] this platform has no implemented immutable package proof")
}

#[cfg(target_os = "macos")]
fn verify_macos_static_code(bundle: &Path) -> Result<()> {
    verify_macos_static_code_for_policy(
        bundle,
        option_env!("TOBKIRI_MACOS_ARTIFACT_POLICY").unwrap_or("production-v1"),
        option_env!("TOBKIRI_MACOS_ARTIFACT_IDENTITY").unwrap_or_default(),
    )
}

#[cfg(target_os = "macos")]
fn macos_code_requirement(policy: &str, identity: &str) -> Result<(&'static str, String)> {
    match policy {
        "production-v1" => {
            if !identity.is_empty() {
                bail!("[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] OSS production artifacts may not claim an Apple signing identity");
            }
            Ok(("dev.rumiai.app", "identifier \"dev.rumiai.app\"".to_owned()))
        }
        "ci-e2e-v1" => {
            require_sha256(identity).context(
                "[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] CI signing identity is not build-bound",
            )?;
            Ok((
                "dev.tobkiri.launcher.ci-e2e",
                "identifier \"dev.tobkiri.launcher.ci-e2e\"".to_owned(),
            ))
        }
        _ => bail!("[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] unknown build-bound artifact policy"),
    }
}

#[cfg(target_os = "macos")]
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MacosCiAttestation {
    schema: String,
    policy: String,
    bundle_identifier: String,
    certificate_sha256: String,
    files: Vec<MacosCiAttestedFile>,
    signature: String,
}

#[cfg(target_os = "macos")]
#[derive(Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct MacosCiAttestedFile {
    path: String,
    sha256: String,
}

#[cfg(target_os = "macos")]
const MACOS_CI_ATTESTED_PATHS: &[&str] = &[
    "Contents/MacOS/tobkiri-launcher",
    "Contents/MacOS/tobkiri-packvm-vz-helper",
    "Contents/Resources/app/python-runtime/sealed-environment.v1.json",
    "Contents/Resources/app/runtime-resource-manifest.v1.json",
    "Contents/Resources/ci-e2e-artifact-policy.v1.json",
    "Contents/Resources/packvm-vz-provisioning.v1.json",
    "Contents/Resources/packvm-vz-helper.manifest.v1.json",
    "Contents/Resources/ci-e2e-signing-certificate.der",
];

#[cfg(target_os = "macos")]
const MACOS_CI_MACHO_ATTESTED_PATHS: &[&str] = &[
    "Contents/MacOS/tobkiri-launcher",
    "Contents/MacOS/tobkiri-packvm-vz-helper",
];

#[cfg(target_os = "macos")]
fn verify_macos_ci_attestation(bundle: &Path, certificate_sha256: &str) -> Result<()> {
    const CERTIFICATE_NAME: &str = "ci-e2e-signing-certificate.der";
    const ATTESTATION_NAME: &str = "ci-e2e-startup-attestation.v1.json";

    require_sha256(certificate_sha256)?;
    let resources = bundle.join("Contents/Resources");
    let certificate = read_bounded_regular(&resources.join(CERTIFICATE_NAME), 16 * 1024)
        .context("[PYTHON_SEALED_PROVENANCE_INVALID] CI certificate is unsafe")?;
    if sha256_bytes(&certificate) != certificate_sha256 {
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] CI certificate differs from the build-bound identity");
    }
    let attestation_bytes = read_bounded_regular(&resources.join(ATTESTATION_NAME), 64 * 1024)
        .context("[PYTHON_SEALED_PROVENANCE_INVALID] CI attestation is unsafe")?;
    let attestation: MacosCiAttestation = serde_json::from_slice(&attestation_bytes)
        .context("[PYTHON_SEALED_PROVENANCE_INVALID] CI attestation is malformed")?;
    if attestation.schema != "io.tobkiri.macos-ci-e2e-attestation.v1"
        || attestation.policy != "ci-e2e-v1"
        || attestation.bundle_identifier != "dev.tobkiri.launcher.ci-e2e"
        || attestation.certificate_sha256 != certificate_sha256
    {
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] CI attestation domain is invalid");
    }
    let expected_files = MACOS_CI_ATTESTED_PATHS
        .iter()
        .map(|relative| {
            let bytes = read_bounded_regular(&bundle.join(relative), 32 * 1024 * 1024)
                .with_context(|| {
                    format!(
                        "[PYTHON_SEALED_PROVENANCE_INVALID] CI attested path is unsafe: {relative}"
                    )
                })?;
            Ok(MacosCiAttestedFile {
                path: (*relative).to_owned(),
                sha256: if MACOS_CI_MACHO_ATTESTED_PATHS.contains(relative) {
                    macho_code_sha256(&bytes)?
                } else {
                    sha256_bytes(&bytes)
                },
            })
        })
        .collect::<Result<Vec<_>>>()?;
    if attestation.files != expected_files {
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] CI attested file identity changed");
    }
    let mut message = format!(
        "TOBKIRI-CI-E2E-ATTESTATION-V1\nbundle_identifier=dev.tobkiri.launcher.ci-e2e\ncertificate_sha256={certificate_sha256}\n"
    );
    for file in &expected_files {
        message.push_str(&file.path);
        message.push('\0');
        message.push_str(&file.sha256);
        message.push('\n');
    }
    let public_key = option_env!("TOBKIRI_MACOS_CI_PUBLIC_KEY").unwrap_or_default();
    let public_key_bytes: [u8; 32] = BASE64
        .decode(public_key)
        .context("[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] CI public key is invalid")?
        .try_into()
        .map_err(|_| {
            anyhow::anyhow!(
                "[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] CI public key length is invalid"
            )
        })?;
    let signature_bytes: [u8; 64] = BASE64
        .decode(attestation.signature)
        .context("[PYTHON_SEALED_PROVENANCE_INVALID] CI signature is malformed")?
        .try_into()
        .map_err(|_| {
            anyhow::anyhow!("[PYTHON_SEALED_PROVENANCE_INVALID] CI signature length is invalid")
        })?;
    VerifyingKey::from_bytes(&public_key_bytes)
        .context("[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] CI public key is invalid")?
        .verify(message.as_bytes(), &Signature::from_bytes(&signature_bytes))
        .context("[PYTHON_SEALED_PROVENANCE_INVALID] CI startup attestation signature rejected")
}

#[cfg(target_os = "macos")]
fn macho_code_sha256(bytes: &[u8]) -> Result<String> {
    fn word(bytes: &[u8], offset: usize) -> Result<u32> {
        Ok(u32::from_le_bytes(
            bytes
                .get(offset..offset + 4)
                .ok_or_else(|| anyhow::anyhow!("Mach-O field is truncated"))?
                .try_into()?,
        ))
    }
    if bytes.len() < 32 || bytes[..4] != [0xcf, 0xfa, 0xed, 0xfe] {
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] CI executable is not a thin 64-bit Mach-O");
    }
    let command_count = word(bytes, 16)? as usize;
    let command_end = 32usize
        .checked_add(word(bytes, 20)? as usize)
        .filter(|end| *end <= bytes.len())
        .ok_or_else(|| anyhow::anyhow!("Mach-O load commands exceed the executable"))?;
    let mut offset = 32usize;
    let mut signature = None;
    let mut linkedit_command = None;
    for _ in 0..command_count {
        let command = word(bytes, offset)?;
        let command_size = word(bytes, offset + 4)? as usize;
        if command_size < 8
            || offset
                .checked_add(command_size)
                .is_none_or(|end| end > command_end)
        {
            bail!("[PYTHON_SEALED_PROVENANCE_INVALID] Mach-O load command size is invalid");
        }
        if command == 0x1d {
            if command_size != 16 || signature.is_some() {
                bail!(
                    "[PYTHON_SEALED_PROVENANCE_INVALID] Mach-O code-signature command is invalid"
                );
            }
            signature = Some((
                offset,
                word(bytes, offset + 8)? as usize,
                word(bytes, offset + 12)? as usize,
            ));
        } else if command == 0x19
            && bytes.get(offset + 8..offset + 24).is_some_and(|name| {
                name.iter()
                    .copied()
                    .take_while(|byte| *byte != 0)
                    .eq(b"__LINKEDIT".iter().copied())
            })
        {
            if command_size < 72 || linkedit_command.is_some() {
                bail!("[PYTHON_SEALED_PROVENANCE_INVALID] Mach-O __LINKEDIT command is invalid");
            }
            linkedit_command = Some(offset);
        }
        offset += command_size;
    }
    let (command_offset, data_offset, data_size) = signature.ok_or_else(|| {
        anyhow::anyhow!(
            "[PYTHON_SEALED_PROVENANCE_INVALID] Mach-O code-signature command is missing"
        )
    })?;
    if offset != command_end
        || data_offset < command_end
        || data_offset.checked_add(data_size) != Some(bytes.len())
    {
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] Mach-O code-signature region is invalid");
    }
    let linkedit_command = linkedit_command.ok_or_else(|| {
        anyhow::anyhow!("[PYTHON_SEALED_PROVENANCE_INVALID] Mach-O __LINKEDIT command is missing")
    })?;
    let mut canonical = bytes[..data_offset].to_vec();
    canonical[command_offset + 8..command_offset + 16].fill(0);
    canonical[linkedit_command + 32..linkedit_command + 40].fill(0);
    canonical[linkedit_command + 48..linkedit_command + 56].fill(0);
    Ok(format!("{:x}", Sha256::digest(canonical)))
}

#[cfg(target_os = "macos")]
fn verify_macos_static_code_for_policy(bundle: &Path, policy: &str, identity: &str) -> Result<()> {
    use std::os::unix::ffi::OsStrExt;
    use std::ptr;

    type CFTypeRef = *const std::ffi::c_void;
    type CFAllocatorRef = *const std::ffi::c_void;
    type CFArrayRef = *const std::ffi::c_void;
    type CFDictionaryRef = *const std::ffi::c_void;
    type CFStringRef = *const std::ffi::c_void;
    type CFURLRef = *const std::ffi::c_void;
    type SecRequirementRef = *const std::ffi::c_void;
    type SecStaticCodeRef = *const std::ffi::c_void;
    #[link(name = "CoreFoundation", kind = "framework")]
    unsafe extern "C" {
        fn CFURLCreateFromFileSystemRepresentation(
            allocator: CFTypeRef,
            bytes: *const u8,
            length: isize,
            is_directory: u8,
        ) -> CFURLRef;
        fn CFStringCreateWithBytes(
            allocator: CFAllocatorRef,
            bytes: *const u8,
            length: isize,
            encoding: u32,
            is_external_representation: u8,
        ) -> CFStringRef;
        fn CFDictionaryGetValue(dictionary: CFDictionaryRef, key: CFTypeRef) -> CFTypeRef;
        fn CFArrayGetCount(array: CFArrayRef) -> isize;
        fn CFRelease(value: CFTypeRef);
    }
    #[link(name = "Security", kind = "framework")]
    unsafe extern "C" {
        static kSecCodeInfoCertificates: CFStringRef;
        fn SecStaticCodeCreateWithPath(
            path: CFURLRef,
            flags: u32,
            code: *mut SecStaticCodeRef,
        ) -> i32;
        fn SecStaticCodeCheckValidity(
            code: SecStaticCodeRef,
            flags: u32,
            requirement: CFTypeRef,
        ) -> i32;
        fn SecRequirementCreateWithString(
            text: CFStringRef,
            flags: u32,
            requirement: *mut SecRequirementRef,
        ) -> i32;
        fn SecCodeCopySigningInformation(
            code: SecStaticCodeRef,
            flags: u32,
            information: *mut CFDictionaryRef,
        ) -> i32;
    }
    const UTF8: u32 = 0x0800_0100;
    const VALIDATION_FLAGS: u32 =
        (1 << 29) | (1 << 9) | (1 << 8) | (1 << 7) | (1 << 4) | (1 << 3) | 1;

    let (bundle_identifier, requirement_text) = macos_code_requirement(policy, identity)?;
    let resources = bundle.join("Contents/Resources");
    let policy_path = resources.join("ci-e2e-artifact-policy.v1.json");
    if policy == "ci-e2e-v1" {
        let expected = include_bytes!("../ci-e2e/ci-e2e-artifact-policy.v1.json");
        let actual = read_bounded_regular(&policy_path, 16 * 1024)
            .context("[PYTHON_SEALED_PROVENANCE_INVALID] CI artifact policy is unsafe")?;
        if actual.as_slice() != expected {
            bail!("[PYTHON_SEALED_PROVENANCE_INVALID] CI artifact policy differs from the build-bound domain");
        }
    } else {
        for marker in [
            "NON_PUBLISHABLE_CI_E2E_ARTIFACT.txt",
            "ci-e2e-artifact-policy.v1.json",
            "ci-e2e-signing-certificate.der",
            "ci-e2e-startup-attestation.v1.json",
        ] {
            if fs::symlink_metadata(resources.join(marker)).is_ok() {
                bail!("[PYTHON_SEALED_PROVENANCE_INVALID] production artifact carries a CI trust-domain marker");
            }
        }
    }

    let bytes = bundle.as_os_str().as_bytes();
    let url = unsafe {
        CFURLCreateFromFileSystemRepresentation(
            ptr::null(),
            bytes.as_ptr(),
            bytes.len() as isize,
            1,
        )
    };
    if url.is_null() {
        bail!("[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] failed to create bundle URL");
    }
    let mut code = ptr::null();
    let create = unsafe { SecStaticCodeCreateWithPath(url, 0, &mut code) };
    unsafe { CFRelease(url) };
    if create != 0 || code.is_null() {
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] SecStaticCodeCreateWithPath={create}");
    }

    let requirement_string = unsafe {
        CFStringCreateWithBytes(
            ptr::null(),
            requirement_text.as_ptr(),
            requirement_text.len() as isize,
            UTF8,
            0,
        )
    };
    if requirement_string.is_null() {
        unsafe { CFRelease(code) };
        bail!("[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] failed to create code requirement");
    }
    let mut requirement = ptr::null();
    let requirement_status =
        unsafe { SecRequirementCreateWithString(requirement_string, 0, &mut requirement) };
    unsafe { CFRelease(requirement_string) };
    if requirement_status != 0 || requirement.is_null() {
        unsafe { CFRelease(code) };
        bail!("[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] invalid build-bound code requirement ({requirement_status})");
    }
    let validity = unsafe { SecStaticCodeCheckValidity(code, VALIDATION_FLAGS, requirement) };
    unsafe { CFRelease(requirement) };
    if validity != 0 {
        unsafe { CFRelease(code) };
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] outer app signature rejected ({validity})");
    }

    let mut signing_information = ptr::null();
    let info_status =
        unsafe { SecCodeCopySigningInformation(code, 1 << 1, &mut signing_information) };
    if info_status != 0 || signing_information.is_null() {
        unsafe { CFRelease(code) };
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] signing certificate information unavailable ({info_status})");
    }
    let certificates = unsafe {
        CFDictionaryGetValue(signing_information, kSecCodeInfoCertificates) as CFArrayRef
    };
    if !certificates.is_null() && unsafe { CFArrayGetCount(certificates) } != 0 {
        unsafe {
            CFRelease(signing_information);
            CFRelease(code);
        }
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] outer signature must remain explicitly ad-hoc");
    }
    unsafe { CFRelease(signing_information) };

    if policy == "ci-e2e-v1" {
        let attestation = verify_macos_ci_attestation(bundle, identity);
        unsafe { CFRelease(code) };
        attestation?;
        debug_assert!(requirement_text.contains(bundle_identifier));
        return Ok(());
    }
    unsafe { CFRelease(code) };
    debug_assert!(requirement_text.contains(bundle_identifier));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn digest(byte: char) -> String {
        byte.to_string().repeat(64)
    }

    fn test_runtime_overlay() -> VerifiedRuntimeOverlay {
        let authority = RuntimeOverlayAuthority {
            schema: RUNTIME_OVERLAY_SCHEMA,
            outer_manifest_sha256: digest('8'),
            sealed_manifest_sha256: digest('9'),
        };
        let bytes = b"test-runtime-overlay".to_vec();
        VerifiedRuntimeOverlay {
            sha256: sha256_bytes(&bytes),
            bytes,
            authority,
        }
    }

    #[test]
    fn runtime_overlay_projects_exact_sealed_catalog_and_lock_closure() {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-runtime-overlay-source-{}-{}",
            std::process::id(),
            random_nonce()
        ));
        let resources = [
            ("core_runtime/bootstrap.py", b"bootstrap\n".as_slice()),
            (
                "ecosystem/defaultspack/v4/catalog.lock.json",
                b"{\"lock\":true}\n".as_slice(),
            ),
        ];
        for (relative, payload) in resources {
            let path = root.join("python-runtime/app").join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, payload).unwrap();
        }
        let entries = resources
            .iter()
            .map(|(path, payload)| {
                serde_json::json!({
                    "path": format!("python-runtime/app/{path}"),
                    "size": payload.len(),
                    "sha256": sha256_bytes(payload),
                })
            })
            .collect::<Vec<_>>();
        fs::write(
            root.join(crate::runtime_resource_integrity::MANIFEST_NAME),
            serde_json::to_vec(&serde_json::json!({
                "schema": "io.tobkiri.runtime-resource-manifest.v1",
                "entries": entries,
            }))
            .unwrap(),
        )
        .unwrap();
        let outer = crate::runtime_resource_integrity::verify(&root).unwrap();
        let mut sealed = minimal_manifest();
        sealed.files = resources
            .iter()
            .map(|(path, payload)| SealedFile {
                path: format!("app/{path}"),
                size: payload.len() as u64,
                sha256: sha256_bytes(payload),
                executable: false,
            })
            .collect();
        let overlay = build_runtime_overlay(&sealed, &digest('9'), &outer).unwrap();
        let document: serde_json::Value = serde_json::from_slice(&overlay.bytes).unwrap();
        assert_eq!(
            document["entries"].as_array().unwrap().len(),
            resources.len()
        );
        assert_eq!(document["entries"][0]["path"], "core_runtime/bootstrap.py");
        assert_eq!(document["overlay"]["outer_manifest_sha256"], outer.sha256());

        sealed.files[0].sha256 = digest('0');
        assert!(build_runtime_overlay(&sealed, &digest('9'), &outer).is_err());

        let legacy_root = root.join("legacy-domain");
        fs::create_dir_all(legacy_root.join("core_runtime")).unwrap();
        fs::write(
            legacy_root.join("core_runtime/bootstrap.py"),
            b"bootstrap\n",
        )
        .unwrap();
        fs::write(
            legacy_root.join(crate::runtime_resource_integrity::MANIFEST_NAME),
            serde_json::to_vec(&serde_json::json!({
                "schema": "io.tobkiri.runtime-resource-manifest.v1",
                "entries": [{
                    "path": "core_runtime/bootstrap.py",
                    "size": 10,
                    "sha256": sha256_bytes(b"bootstrap\n"),
                }],
            }))
            .unwrap(),
        )
        .unwrap();
        let legacy_outer = crate::runtime_resource_integrity::verify(&legacy_root).unwrap();
        let mut legacy_sealed = minimal_manifest();
        legacy_sealed.files = vec![SealedFile {
            path: "app/core_runtime/bootstrap.py".into(),
            size: 10,
            sha256: sha256_bytes(b"bootstrap\n"),
            executable: false,
        }];
        assert!(build_runtime_overlay(&legacy_sealed, &digest('9'), &legacy_outer).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    fn minimal_manifest() -> SealedEnvironmentManifest {
        let files = required_layout_paths()
            .iter()
            .map(|path| (*path).to_string())
            .chain(std::iter::once(fixed_bootstrap_path("3.13.13").unwrap()))
            .map(|path| {
                let executable = path.ends_with("python3") || path.ends_with("python.exe");
                SealedFile {
                    path,
                    size: 1,
                    sha256: digest('a'),
                    executable,
                }
            })
            .collect::<Vec<_>>();
        let environment_digest = sha256_bytes(&serde_json::to_vec(&files).unwrap());
        SealedEnvironmentManifest {
            schema: MANIFEST_SCHEMA.into(),
            environment_digest,
            platform: std::env::consts::OS.into(),
            architecture: std::env::consts::ARCH.into(),
            python_version: "3.13.13".into(),
            package_provenance: PackageProvenance {
                kind: required_package_provenance_kind().into(),
                package_id: "dev.rumiai.app".into(),
                release_digest: digest('b'),
            },
            sentinels: SentinelContract {
                stdlib_sha256: digest('c'),
                site_packages_sha256: digest('d'),
                native_sha256: digest('e'),
            },
            files,
        }
    }

    fn materialized_environment() -> (PathBuf, SealedEnvironmentManifest) {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-sealed-python-{}-{}",
            std::process::id(),
            random_nonce()
        ));
        let mut manifest = minimal_manifest();
        for entry in &mut manifest.files {
            let path = root.join(&entry.path);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, b"x").unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mode = if entry.executable { 0o555 } else { 0o444 };
                fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
            }
            entry.size = 1;
            entry.sha256 = sha256_bytes(b"x");
        }
        let mut directories = Vec::new();
        collect_files(&root, &root, &mut Vec::new(), &mut directories).unwrap();
        directories.sort();
        let evidence = SealedDirectoryModes {
            schema: DIRECTORY_MODES_SCHEMA.to_string(),
            directories: std::iter::once(SealedDirectoryMode {
                path: ".".to_string(),
                mode: "0555".to_string(),
            })
            .chain(directories.iter().map(|path| SealedDirectoryMode {
                path: path.clone(),
                mode: "0555".to_string(),
            }))
            .collect(),
        };
        let evidence_bytes = serde_json::to_vec(&evidence).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                root.join(DIRECTORY_MODES_FILENAME),
                fs::Permissions::from_mode(0o644),
            )
            .unwrap();
        }
        fs::write(root.join(DIRECTORY_MODES_FILENAME), &evidence_bytes).unwrap();
        let evidence_entry = manifest
            .files
            .iter_mut()
            .find(|entry| entry.path == DIRECTORY_MODES_FILENAME)
            .unwrap();
        evidence_entry.size = evidence_bytes.len() as u64;
        evidence_entry.sha256 = sha256_bytes(&evidence_bytes);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                root.join(DIRECTORY_MODES_FILENAME),
                fs::Permissions::from_mode(0o444),
            )
            .unwrap();
            for directory in &directories {
                fs::set_permissions(root.join(directory), fs::Permissions::from_mode(0o555))
                    .unwrap();
            }
            fs::set_permissions(&root, fs::Permissions::from_mode(0o555)).unwrap();
        }
        manifest
            .files
            .sort_by(|left, right| left.path.cmp(&right.path));
        manifest.environment_digest = sha256_bytes(&serde_json::to_vec(&manifest.files).unwrap());
        (root, manifest)
    }

    #[cfg(unix)]
    fn make_test_tree_writable(root: &Path) {
        use std::os::unix::fs::PermissionsExt;
        let mut entries = fs::read_dir(root)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect::<Vec<_>>();
        while let Some(path) = entries.pop() {
            let metadata = fs::symlink_metadata(&path).unwrap();
            if metadata.is_dir() {
                fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
                entries.extend(
                    fs::read_dir(&path)
                        .unwrap()
                        .map(|entry| entry.unwrap().path()),
                );
            } else if metadata.is_file() {
                let mode = if metadata.permissions().mode() & 0o111 != 0 {
                    0o755
                } else {
                    0o644
                };
                fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
            }
        }
        fs::set_permissions(root, fs::Permissions::from_mode(0o755)).unwrap();
    }

    #[cfg(target_os = "macos")]
    fn test_snapshot_environment(label: &str) -> (PathBuf, VerifiedEnvironment) {
        let (source, manifest) = materialized_environment();
        let root = std::env::temp_dir().join(format!(
            ".tobkiri-sealed-python-{label}-{}-{}",
            std::process::id(),
            random_nonce()
        ));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&source, fs::Permissions::from_mode(0o755)).unwrap();
        }
        fs::rename(source, &root).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
        }
        fs::write(
            root.join(MANIFEST_FILENAME),
            serde_json::to_vec(&manifest).unwrap(),
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                root.join(MANIFEST_FILENAME),
                fs::Permissions::from_mode(0o444),
            )
            .unwrap();
            fs::set_permissions(&root, fs::Permissions::from_mode(0o555)).unwrap();
        }
        let environment = VerifiedEnvironment {
            manifest_path: root.join(MANIFEST_FILENAME),
            _root_lease: open_directory(&root).unwrap(),
            _interpreter_lease: open_regular(&fixed_interpreter(&root)).unwrap(),
            environment_lease: None,
            snapshot_path: Some(root.clone()),
            snapshot_verification: None,
            runtime_overlay: test_runtime_overlay(),
            cleanup_authority: CleanupAuthority::BeforeChildSpawn,
            root: root.clone(),
            manifest,
        };
        (root, environment)
    }

    #[cfg(target_os = "macos")]
    fn child_with_snapshot(label: &str, script: &str) -> (PathBuf, PythonChild) {
        let (path, environment) = test_snapshot_environment(label);
        let child = Command::new("/bin/sh")
            .args(["-c", script])
            .spawn()
            .unwrap();
        (path, PythonChild::packaged(child, environment))
    }

    #[cfg(target_os = "macos")]
    fn wait_for_snapshot_cleanup(path: &Path) {
        let deadline = Instant::now() + Duration::from_secs(3);
        while path.exists() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
        }
        assert!(!path.exists(), "snapshot was not cleaned after child exit");
    }

    #[test]
    fn strict_manifest_accepts_only_fixed_sorted_layout() {
        let mut manifest = minimal_manifest();
        manifest
            .files
            .sort_by(|left, right| left.path.cmp(&right.path));
        manifest.environment_digest = sha256_bytes(&serde_json::to_vec(&manifest.files).unwrap());
        validate_manifest_contract(&manifest).unwrap();
        manifest.files.push(SealedFile {
            path: "../escape".into(),
            size: 0,
            sha256: digest('f'),
            executable: false,
        });
        assert!(validate_manifest_contract(&manifest).is_err());
    }

    #[test]
    fn sealed_inventory_accepts_regular_manifest_but_does_not_inventory_it() {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-sealed-manifest-inventory-{}-{}",
            std::process::id(),
            random_nonce()
        ));
        fs::create_dir(&root).unwrap();
        fs::write(root.join(MANIFEST_FILENAME), b"{}").unwrap();
        fs::write(root.join("python"), b"binary").unwrap();

        let mut files = Vec::new();
        let mut directories = Vec::new();
        collect_files(&root, &root, &mut files, &mut directories).unwrap();

        assert_eq!(files, vec!["python"]);
        assert!(directories.is_empty());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn portable_directory_inventory_sorts_flattened_paths() {
        let directories = [
            PathBuf::from("runtime/lib/python"),
            PathBuf::from("runtime-lib/python"),
            PathBuf::from("runtime/lib"),
        ]
        .into_iter()
        .collect::<std::collections::BTreeSet<_>>();

        assert_eq!(
            portable_directory_inventory(directories),
            vec!["runtime-lib/python", "runtime/lib", "runtime/lib/python"]
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn sealed_snapshot_parent_is_canonical() {
        let parent = canonical_private_temp_root().unwrap();

        assert_eq!(fs::canonicalize(&parent).unwrap(), parent);
    }

    #[test]
    fn sealed_digest_domain_rejects_sha256_prefix() {
        let mut manifest = minimal_manifest();
        manifest
            .files
            .sort_by(|left, right| left.path.cmp(&right.path));
        manifest.environment_digest = sha256_bytes(&serde_json::to_vec(&manifest.files).unwrap());
        manifest.sentinels.stdlib_sha256 = format!("sha256:{}", digest('c'));
        assert!(validate_manifest_contract(&manifest).is_err());
    }

    #[test]
    fn package_provenance_identity_is_exact_and_manifest_bound() {
        let mut manifest = minimal_manifest();
        manifest
            .files
            .sort_by(|left, right| left.path.cmp(&right.path));
        manifest.environment_digest = sha256_bytes(&serde_json::to_vec(&manifest.files).unwrap());
        validate_manifest_contract(&manifest).unwrap();

        let expected_manifest_digest = sha256_bytes(&serde_json::to_vec(&manifest).unwrap());

        let mut wrong_kind = manifest.clone();
        wrong_kind.package_provenance.kind = "apple-code-signature-v1".into();
        assert!(validate_manifest_contract(&wrong_kind).is_err());

        let mut wrong_package = manifest.clone();
        wrong_package.package_provenance.package_id = "dev.tobkiri.other".into();
        assert!(validate_manifest_contract(&wrong_package).is_err());

        let mut malformed_release = manifest.clone();
        malformed_release.package_provenance.release_digest = format!("sha256:{}", digest('b'));
        assert!(validate_manifest_contract(&malformed_release).is_err());

        let mut substituted_release = manifest;
        substituted_release.package_provenance.release_digest = digest('f');
        validate_manifest_contract(&substituted_release).unwrap();
        assert_ne!(
            sha256_bytes(&serde_json::to_vec(&substituted_release).unwrap()),
            expected_manifest_digest
        );
    }

    #[test]
    fn role_arguments_cannot_select_python_entrypoint() {
        assert!(RoleArguments::defaultspack([OsString::from("-c")]).is_err());
        assert!(RoleArguments::defaultspack([OsString::from("-m")]).is_err());
        assert!(RoleArguments::defaultspack([OsString::from("--port=8766")]).is_ok());
        assert!(RoleArguments::defaultspack([OsString::from("--api-token=secret")]).is_err());
    }

    #[test]
    fn packaged_packvm_binding_is_derived_from_exact_outer_bundle_bytes() {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-packvm-bundle-binding-{}-{}",
            std::process::id(),
            random_nonce()
        ));
        let app_dir = root.join("Tobkiri Launcher.app/Contents/Resources/app");
        fs::create_dir_all(&app_dir).unwrap();
        let resources = app_dir.parent().unwrap();
        let provisioning = br#"{"schema":"io.tobkiri.packvm-vz-provisioning.v1"}"#;
        let helper = br#"{"schema":"io.tobkiri.packvm-vz-helper-manifest.v1"}"#;
        fs::write(
            resources.join("packvm-vz-provisioning.v1.json"),
            provisioning,
        )
        .unwrap();
        fs::write(resources.join("packvm-vz-helper.manifest.v1.json"), helper).unwrap();

        let binding = packaged_packvm_bundle_binding_from_app_dir(&app_dir)
            .unwrap()
            .unwrap();
        assert_eq!(
            binding.root,
            fs::canonicalize(root.join("Tobkiri Launcher.app")).unwrap()
        );
        assert_eq!(binding.provisioning_sha256, sha256_bytes(provisioning));
        assert_eq!(binding.helper_manifest_sha256, sha256_bytes(helper));

        fs::write(
            resources.join("packvm-vz-provisioning.v1.json"),
            b"substituted",
        )
        .unwrap();
        let substituted = packaged_packvm_bundle_binding_from_app_dir(&app_dir)
            .unwrap()
            .unwrap();
        assert_ne!(substituted.provisioning_sha256, binding.provisioning_sha256);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn non_bundle_runtime_has_no_packvm_binding() {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-packvm-unbundled-{}-{}",
            std::process::id(),
            random_nonce()
        ));
        fs::create_dir_all(&root).unwrap();
        assert!(packaged_packvm_bundle_binding_from_app_dir(&root)
            .unwrap()
            .is_none());
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn all_roles_use_one_wire_and_only_defaultspack_receives_role_arguments() {
        let mut command = Command::new("python");
        append_launch_wire(
            &mut command,
            PythonRole::Defaultspack,
            "nonce",
            Path::new("attestation"),
            Path::new("manifest"),
            Path::new("environment"),
            &digest('a'),
            &digest('b'),
            None,
            RoleArguments::defaultspack([OsString::from("--port=8766")]).unwrap(),
        )
        .unwrap();
        let arguments = command
            .get_args()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        assert_eq!(
            arguments[arguments.len() - 2..],
            [protocol::ARG_SEPARATOR, "--port=8766"]
        );

        let mut bypass = Command::new("python");
        assert!(append_launch_wire(
            &mut bypass,
            PythonRole::HostHelper,
            "nonce",
            Path::new("attestation"),
            Path::new("manifest"),
            Path::new("environment"),
            &digest('a'),
            &digest('b'),
            None,
            RoleArguments(vec![OsString::from("unexpected")]),
        )
        .is_err());
    }

    #[test]
    fn packaged_environment_is_cleared_allowlisted_and_rejects_metadata_injection() {
        let mut command = Command::new("python");
        command.env("REPO", "/untrusted/inherited");
        {
            let mut role = RoleCommand::packaged(&mut command, PythonRole::Defaultspack);
            role.env("DEFAULTS_HTTP_PORT", "8766")
                .env("RUMI_DEFAULTSPACK_SURFACE", "webview");
            role.finish().unwrap();
        }
        let environment = command
            .get_envs()
            .map(|(key, value)| (key.to_os_string(), value.map(OsStr::to_os_string)))
            .collect::<BTreeMap<_, _>>();
        assert!(!environment.contains_key(OsStr::new("REPO")));
        assert_eq!(
            environment.get(OsStr::new("DEFAULTS_HTTP_PORT")),
            Some(&Some(OsString::from("8766")))
        );

        for key in [
            "REPO",
            "RUMI_CORE_DIR",
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "PYTHONSTARTUP",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "PATH",
        ] {
            let mut command = Command::new("python");
            let error = {
                let mut role = RoleCommand::packaged(&mut command, PythonRole::Defaultspack);
                role.envs([("DEFAULTS_HTTP_PORT", "8766"), (key, "/untrusted/metadata")]);
                role.finish().unwrap_err()
            };
            assert!(error.to_string().contains(key));
            assert!(!error.to_string().contains("/untrusted/metadata"));
        }
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn verified_environment_drop_cleans_every_pre_child_failure_stage() {
        for stage in ["configure", "revalidate", "spawn"] {
            let (path, environment) = test_snapshot_environment(stage);
            let result: Result<()> = (|| {
                let _environment = environment;
                bail!("injected {stage} failure")
            })();
            assert!(result.is_err());
            assert!(!path.exists(), "snapshot leaked after {stage} failure");
        }
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn pending_construction_guard_cleans_partial_environment() {
        let (path, mut environment) = test_snapshot_environment("partial");
        let snapshot_path = environment.snapshot_path.take();
        let root_handle = environment._root_lease.try_clone().unwrap();
        drop(environment);
        let _pending = PendingSnapshotCleanup::new(snapshot_path, root_handle);
        drop(_pending);
        assert!(!path.exists());
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn snapshot_cleanup_never_follows_substituted_paths_or_symlinks() {
        use std::os::unix::fs::symlink;

        let external = std::env::temp_dir().join(format!(
            "tobkiri-cleanup-external-{}-{}",
            std::process::id(),
            random_nonce()
        ));
        fs::create_dir(&external).unwrap();
        fs::write(external.join("preserve"), b"external").unwrap();

        let (symlink_path, symlink_environment) = test_snapshot_environment("symlink");
        make_test_tree_writable(&symlink_path);
        fs::remove_dir_all(symlink_path.join("app")).unwrap();
        symlink(&external, symlink_path.join("app")).unwrap();
        drop(symlink_environment);
        assert!(external.join("preserve").exists());
        assert!(!symlink_path.exists());

        let (path, environment) = test_snapshot_environment("path-swap");
        let original = path.with_extension("original");
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        fs::rename(&path, &original).unwrap();
        fs::create_dir(&path).unwrap();
        fs::write(path.join("preserve"), b"replacement").unwrap();
        drop(environment);
        assert!(path.join("preserve").exists());
        assert!(original.exists());
        fs::remove_dir_all(path).unwrap();
        make_test_tree_writable(&original);
        fs::remove_dir_all(original).unwrap();
        fs::remove_dir_all(external).unwrap();
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn python_child_retains_snapshot_until_wait_kill_or_drop() {
        let (wait_path, mut waited) = child_with_snapshot("wait", "exit 0");
        assert!(wait_path.exists());
        waited.wait().unwrap();
        assert!(!wait_path.exists());

        let (kill_path, mut killed) = child_with_snapshot("kill", "sleep 30");
        assert!(kill_path.exists());
        killed.kill().unwrap();
        assert!(!kill_path.exists());

        let (drop_path, dropped) = child_with_snapshot("drop", "sleep 30");
        assert!(drop_path.exists());
        drop(dropped);
        assert!(!drop_path.exists());
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn child_operation_failures_preserve_snapshot_until_exit_confirmation() {
        let (try_wait_path, mut try_wait_child) = child_with_snapshot("try-wait-error", "sleep 1");
        try_wait_child.operation_failures.try_wait_once = true;
        assert!(try_wait_child.try_wait().is_err());
        assert!(try_wait_path.exists());
        try_wait_child.operation_failures.kill_once = true;
        drop(try_wait_child);
        assert!(try_wait_path.exists());
        assert!(fs::create_dir(&try_wait_path).is_err());
        wait_for_snapshot_cleanup(&try_wait_path);

        let (kill_path, mut kill_child) = child_with_snapshot("kill-error", "sleep 1");
        kill_child.operation_failures.kill_once = true;
        assert!(kill_child.kill().is_err());
        assert!(kill_path.exists());
        assert!(fs::create_dir(&kill_path).is_err());
        wait_for_snapshot_cleanup(&kill_path);

        let (wait_path, mut wait_child) = child_with_snapshot("wait-error", "sleep 30");
        wait_child.operation_failures.wait_once = true;
        assert!(wait_child.wait().is_err());
        assert!(wait_path.exists());
        drop(wait_child);
        wait_for_snapshot_cleanup(&wait_path);
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn kill_wait_and_repeated_status_queries_preserve_child_api() {
        let (path, mut child) = child_with_snapshot("kill-wait-api", "sleep 30");
        let pid = child.id();
        child.kill().unwrap();
        let status = child.wait().unwrap();
        assert_eq!(child.id(), pid);
        assert_eq!(child.wait().unwrap(), status);
        assert_eq!(child.try_wait().unwrap(), Some(status));
        assert!(!path.exists());

        let (natural_path, mut natural) = child_with_snapshot("natural-exit-api", "exit 7");
        let natural_status = natural.wait().unwrap();
        assert_eq!(natural_status.code(), Some(7));
        assert_eq!(natural.try_wait().unwrap(), Some(natural_status));
        assert_eq!(natural.wait().unwrap(), natural_status);
        assert!(!natural_path.exists());
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn reaper_owned_child_completes_bounded_wait_and_caches_status() {
        let (path, mut child) = child_with_snapshot("reaper-api", "sleep 1; exit 9");
        child.operation_failures.kill_once = true;
        assert!(child.kill().is_err());
        assert_eq!(child.try_wait().unwrap(), None);
        assert!(path.exists());
        let status = child.wait().unwrap();
        assert_eq!(status.code(), Some(9));
        assert_eq!(child.try_wait().unwrap(), Some(status));
        assert_eq!(child.wait().unwrap(), status);
        assert!(!path.exists());
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn reaper_timeout_and_poll_error_are_cached_safe_residue_states() {
        for (label, timeout) in [("reaper-timeout", true), ("reaper-poll-error", false)] {
            let (path, mut child) = child_with_snapshot(label, "sleep 1");
            child.operation_failures.kill_once = true;
            if timeout {
                child.operation_failures.reaper_timeout_once = true;
            } else {
                child.operation_failures.reaper_poll_error_once = true;
            }
            assert!(child.kill().is_err());
            let first = child.wait().unwrap_err().to_string();
            let repeated = child.try_wait().unwrap_err().to_string();
            assert_eq!(first, repeated);
            assert!(first.contains("snapshot was preserved"));
            assert!(path.exists());
            thread::sleep(Duration::from_millis(1200));
            assert!(path.exists());
            make_test_tree_writable(&path);
            fs::remove_dir_all(path).unwrap();
        }
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn failed_reaper_handoff_preserves_snapshot_instead_of_deleting_live_imports() {
        let (path, mut child) = child_with_snapshot("handoff-error", "sleep 1");
        child.operation_failures.kill_once = true;
        child.operation_failures.reaper_handoff_once = true;
        assert!(child.kill().is_err());
        let first = child.wait().unwrap_err().to_string();
        let repeated = child.try_wait().unwrap_err().to_string();
        assert_eq!(first, repeated);
        assert!(first.contains("snapshot was preserved"));
        drop(child);
        assert!(path.exists());
        assert!(fs::create_dir(&path).is_err());
        thread::sleep(Duration::from_millis(1200));
        assert!(path.exists(), "failed handoff must leave a safe residue");
        make_test_tree_writable(&path);
        fs::remove_dir_all(path).unwrap();
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn startup_failure_paths_use_confirmed_termination_boundary() {
        for diagnostic in [
            "startup attestation failure",
            "child lifetime lease proof failure",
        ] {
            let (path, mut child) = child_with_snapshot(diagnostic, "sleep 1");
            child.operation_failures.kill_once = true;
            child.terminate_and_confirm_or_reap(diagnostic, true);
            assert!(path.exists(), "live child lost snapshot after {diagnostic}");
            wait_for_snapshot_cleanup(&path);
        }
    }

    #[test]
    fn unknown_manifest_fields_are_rejected() {
        let mut value = serde_json::to_value(minimal_manifest()).unwrap();
        value
            .as_object_mut()
            .unwrap()
            .insert("extra".into(), true.into());
        assert!(serde_json::from_value::<SealedEnvironmentManifest>(value).is_err());
    }

    #[test]
    fn complete_tree_rejects_missing_tampered_extra_and_linked_content() {
        let (root, manifest) = materialized_environment();
        verify_environment_tree(&root, &manifest).unwrap();

        #[cfg(unix)]
        make_test_tree_writable(&root);

        let victim = root.join("app/kernel_entry.py");
        fs::write(&victim, b"tampered").unwrap();
        assert!(verify_environment_tree(&root, &manifest).is_err());
        fs::write(&victim, b"x").unwrap();

        fs::write(root.join("extra.py"), b"x").unwrap();
        assert!(verify_environment_tree(&root, &manifest).is_err());
        fs::remove_file(root.join("extra.py")).unwrap();
        fs::create_dir(root.join("empty-extra")).unwrap();
        assert!(verify_environment_tree(&root, &manifest).is_err());
        fs::remove_dir(root.join("empty-extra")).unwrap();
        fs::remove_file(&victim).unwrap();
        assert!(verify_environment_tree(&root, &manifest).is_err());

        #[cfg(unix)]
        {
            fs::hard_link(root.join("lease.v1"), &victim).unwrap();
            assert!(verify_environment_tree(&root, &manifest).is_err());
            fs::remove_file(&victim).unwrap();
            std::os::unix::fs::symlink(root.join("lease.v1"), &victim).unwrap();
            assert!(verify_environment_tree(&root, &manifest).is_err());
        }
        fs::remove_dir_all(root).ok();
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn anchored_copy_ignores_path_replacement_and_rejects_hardlinks() {
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::{DirBuilderExt, PermissionsExt};

        let (source_path, manifest) = materialized_environment();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&source_path, fs::Permissions::from_mode(0o755)).unwrap();
        }
        fs::write(
            source_path.join(MANIFEST_FILENAME),
            serde_json::to_vec(&manifest).unwrap(),
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                source_path.join(MANIFEST_FILENAME),
                fs::Permissions::from_mode(0o444),
            )
            .unwrap();
            fs::set_permissions(&source_path, fs::Permissions::from_mode(0o555)).unwrap();
        }
        let source = open_directory(&source_path).unwrap();
        let moved = source_path.with_extension("held");
        #[cfg(unix)]
        fs::set_permissions(&source_path, fs::Permissions::from_mode(0o755)).unwrap();
        fs::rename(&source_path, &moved).unwrap();
        fs::create_dir(&source_path).unwrap();
        fs::create_dir_all(source_path.join("app")).unwrap();
        fs::write(source_path.join("app/kernel_entry.py"), b"malicious").unwrap();

        let destination_path = source_path.with_extension("snapshot");
        let mut builder = fs::DirBuilder::new();
        builder.mode(0o700).create(&destination_path).unwrap();
        let destination = open_directory_inheritable(&destination_path).unwrap();
        let kernel_entry = manifest
            .files
            .iter()
            .find(|entry| entry.path == "app/kernel_entry.py")
            .unwrap();
        copy_anchored_file(
            source.as_raw_fd(),
            destination.as_raw_fd(),
            Path::new("app/kernel_entry.py"),
            false,
            kernel_entry.size,
            &kernel_entry.sha256,
        )
        .unwrap();
        assert_eq!(
            fs::read(destination_path.join("app/kernel_entry.py")).unwrap(),
            b"x"
        );
        let manifest_bytes = serde_json::to_vec(&manifest).unwrap();
        copy_anchored_file(
            source.as_raw_fd(),
            destination.as_raw_fd(),
            Path::new(MANIFEST_FILENAME),
            false,
            manifest_bytes.len() as u64,
            &sha256_bytes(&manifest_bytes),
        )
        .unwrap();
        for entry in &manifest.files {
            if entry.path != "app/kernel_entry.py" {
                copy_anchored_file(
                    source.as_raw_fd(),
                    destination.as_raw_fd(),
                    Path::new(&entry.path),
                    entry.executable,
                    entry.size,
                    &entry.sha256,
                )
                .unwrap();
            }
        }
        seal_snapshot_directories(destination.as_raw_fd(), &manifest).unwrap();
        fs::set_permissions(
            destination_path.join("app/kernel_entry.py"),
            fs::Permissions::from_mode(0o644),
        )
        .unwrap();
        fs::write(destination_path.join("app/kernel_entry.py"), b"z").unwrap();
        fs::set_permissions(
            destination_path.join("app/kernel_entry.py"),
            fs::Permissions::from_mode(0o444),
        )
        .unwrap();
        assert!(authenticate_snapshot_anchored(
            destination.as_raw_fd(),
            &manifest,
            manifest_bytes.len() as u64,
            &sha256_bytes(&manifest_bytes),
            None,
        )
        .is_err());
        fs::set_permissions(
            destination_path.join("app/kernel_entry.py"),
            fs::Permissions::from_mode(0o644),
        )
        .unwrap();
        fs::write(destination_path.join("app/kernel_entry.py"), b"x").unwrap();
        fs::set_permissions(
            destination_path.join("app/kernel_entry.py"),
            fs::Permissions::from_mode(0o444),
        )
        .unwrap();
        let snapshot_verification = authenticate_snapshot_anchored(
            destination.as_raw_fd(),
            &manifest,
            manifest_bytes.len() as u64,
            &sha256_bytes(&manifest_bytes),
            None,
        )
        .unwrap();
        verify_snapshot_anchored(
            destination.as_raw_fd(),
            &manifest,
            &snapshot_verification,
            None,
        )
        .unwrap();
        verify_snapshot_anchored(
            destination.as_raw_fd(),
            &manifest,
            &snapshot_verification,
            None,
        )
        .expect("anchored inventory must be repeatable on the retained root lease");

        // After the one destination authentication pass, a same-size rewrite
        // is rejected from its anchored inode timestamps without another
        // content read.
        fs::set_permissions(
            destination_path.join("app/kernel_entry.py"),
            fs::Permissions::from_mode(0o644),
        )
        .unwrap();
        fs::write(destination_path.join("app/kernel_entry.py"), b"z").unwrap();
        fs::set_permissions(
            destination_path.join("app/kernel_entry.py"),
            fs::Permissions::from_mode(0o444),
        )
        .unwrap();
        assert!(verify_snapshot_anchored(
            destination.as_raw_fd(),
            &manifest,
            &snapshot_verification,
            None,
        )
        .is_err());

        let digest_rejected_path = source_path.with_extension("digest-rejected");
        let mut digest_rejected_builder = fs::DirBuilder::new();
        digest_rejected_builder
            .mode(0o700)
            .create(&digest_rejected_path)
            .unwrap();
        let digest_rejected = open_directory_inheritable(&digest_rejected_path).unwrap();
        assert!(copy_anchored_file(
            source.as_raw_fd(),
            digest_rejected.as_raw_fd(),
            Path::new("app/kernel_entry.py"),
            false,
            kernel_entry.size,
            &digest('0'),
        )
        .is_err());

        #[cfg(unix)]
        fs::set_permissions(moved.join("app"), fs::Permissions::from_mode(0o755)).unwrap();
        fs::hard_link(
            moved.join("app/kernel_entry.py"),
            moved.join("app/kernel_entry-copy.py"),
        )
        .unwrap();
        let rejected_path = source_path.with_extension("rejected");
        let mut rejected_builder = fs::DirBuilder::new();
        rejected_builder.mode(0o700).create(&rejected_path).unwrap();
        let rejected = open_directory_inheritable(&rejected_path).unwrap();
        assert!(copy_anchored_file(
            source.as_raw_fd(),
            rejected.as_raw_fd(),
            Path::new("app/kernel_entry.py"),
            false,
            kernel_entry.size,
            &kernel_entry.sha256,
        )
        .is_err());

        cleanup_macos_snapshot(&destination_path, &destination);
        fs::remove_dir_all(digest_rejected_path).ok();
        fs::remove_dir_all(rejected_path).ok();
        fs::remove_dir_all(source_path).ok();
        fs::remove_dir_all(moved).ok();
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn unsigned_macos_source_is_rejected_as_package_provenance() {
        let path = std::env::temp_dir().join(format!(
            "Unsigned-Tobkiri-{}-{}.app",
            std::process::id(),
            random_nonce()
        ));
        fs::create_dir(&path).unwrap();
        let error = verify_macos_static_code_for_policy(&path, "production-v1", "").unwrap_err();
        assert!(error
            .to_string()
            .contains("PYTHON_SEALED_PROVENANCE_INVALID"));
        let unavailable =
            verify_macos_static_code_for_policy(&path, "production-v1", "ABC1234567").unwrap_err();
        assert!(unavailable
            .to_string()
            .contains("PYTHON_SEALED_PROVENANCE_UNAVAILABLE"));
        fs::remove_dir(path).ok();
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn macos_artifact_policy_rejects_identity_and_domain_swaps() {
        let production = macos_code_requirement("production-v1", "").unwrap();
        assert_eq!(production.0, "dev.rumiai.app");
        assert_eq!(production.1, "identifier \"dev.rumiai.app\"");

        let ci = macos_code_requirement("ci-e2e-v1", &digest('a')).unwrap();
        assert_eq!(ci.0, "dev.tobkiri.launcher.ci-e2e");
        assert!(!ci.1.contains("dev.rumiai.app\" and anchor"));
        for (policy, identity) in [
            ("production-v1", "ABC1234567"),
            ("ci-e2e-v1", "ABC1234567"),
            ("ad-hoc", &digest('c')[..]),
        ] {
            assert!(macos_code_requirement(policy, identity).is_err());
        }
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn ci_attestation_binds_the_packvm_helper_and_manifests() {
        assert_eq!(
            MACOS_CI_ATTESTED_PATHS,
            [
                "Contents/MacOS/tobkiri-launcher",
                "Contents/MacOS/tobkiri-packvm-vz-helper",
                "Contents/Resources/app/python-runtime/sealed-environment.v1.json",
                "Contents/Resources/app/runtime-resource-manifest.v1.json",
                "Contents/Resources/ci-e2e-artifact-policy.v1.json",
                "Contents/Resources/packvm-vz-provisioning.v1.json",
                "Contents/Resources/packvm-vz-helper.manifest.v1.json",
                "Contents/Resources/ci-e2e-signing-certificate.der",
            ]
        );
        assert_eq!(
            MACOS_CI_MACHO_ATTESTED_PATHS,
            [
                "Contents/MacOS/tobkiri-launcher",
                "Contents/MacOS/tobkiri-packvm-vz-helper",
            ]
        );
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn ad_hoc_macos_ci_bundle_is_rejected_without_build_bound_certificate() {
        let path = std::env::temp_dir().join(format!(
            "Tobkiri-CI-AdHoc-{}-{}.app",
            std::process::id(),
            random_nonce()
        ));
        let executable_dir = path.join("Contents/MacOS");
        fs::create_dir_all(&executable_dir).unwrap();
        let resources_dir = path.join("Contents/Resources");
        fs::create_dir(&resources_dir).unwrap();
        fs::copy("/usr/bin/true", executable_dir.join("fixture")).unwrap();
        fs::write(
            resources_dir.join("ci-e2e-artifact-policy.v1.json"),
            include_bytes!("../ci-e2e/ci-e2e-artifact-policy.v1.json"),
        )
        .unwrap();
        fs::write(
            path.join("Contents/Info.plist"),
            br#"<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.tobkiri.launcher.ci-e2e</string>
<key>CFBundleExecutable</key><string>fixture</string>
<key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>"#,
        )
        .unwrap();
        let status = Command::new("/usr/bin/codesign")
            .args(["--force", "--sign", "-"])
            .arg(&path)
            .status()
            .unwrap();
        assert!(status.success());
        let error =
            verify_macos_static_code_for_policy(&path, "ci-e2e-v1", &digest('d')).unwrap_err();
        assert!(error
            .to_string()
            .contains("PYTHON_SEALED_PROVENANCE_INVALID"));
        fs::remove_dir_all(path).ok();
    }

    #[test]
    #[cfg(unix)]
    fn child_lifetime_lease_is_proven_not_asserted() {
        use std::os::fd::AsRawFd;

        let (root, _) = materialized_environment();
        let path = root.join(LIFETIME_LEASE);
        assert!(verify_child_lifetime_lease(&path).is_err());
        let held = open_regular(&path).unwrap();
        assert_eq!(unsafe { libc::flock(held.as_raw_fd(), libc::LOCK_SH) }, 0);
        verify_child_lifetime_lease(&path).unwrap();
        assert_eq!(unsafe { libc::flock(held.as_raw_fd(), libc::LOCK_UN) }, 0);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn attestation_binds_role_prefixes_paths_sentinels_and_digest() {
        let (root, mut manifest) = materialized_environment();
        #[cfg(unix)]
        make_test_tree_writable(&root);
        let import_files = [
            "app/ecosystem/defaultspack/__init__.py",
            "runtime/lib/python3.13/os.py",
            "runtime/lib/python3.13/lib-dynload/_ssl.so",
            "venv/lib/python3.13/site-packages/fixture.py",
        ];
        for relative in import_files {
            let path = root.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, b"x").unwrap();
            manifest.files.push(SealedFile {
                path: relative.into(),
                size: 1,
                sha256: sha256_bytes(b"x"),
                executable: false,
            });
        }
        manifest
            .files
            .sort_by(|left, right| left.path.cmp(&right.path));
        let verified = VerifiedEnvironment {
            manifest_path: root.join(MANIFEST_FILENAME),
            _root_lease: open_directory(&root).unwrap(),
            _interpreter_lease: open_regular(&fixed_interpreter(&root)).unwrap(),
            environment_lease: None,
            snapshot_path: None,
            #[cfg(target_os = "macos")]
            snapshot_verification: None,
            runtime_overlay: test_runtime_overlay(),
            cleanup_authority: CleanupAuthority::BeforeChildSpawn,
            root: root.clone(),
            manifest: manifest.clone(),
        };
        let mut attestation = StartupAttestation {
            schema: ATTESTATION_SCHEMA.into(),
            nonce: "nonce".into(),
            role: protocol::ROLE_TYPED.into(),
            environment_digest: manifest.environment_digest.clone(),
            executable: fs::canonicalize(fixed_interpreter(&root))
                .unwrap()
                .to_string_lossy()
                .into_owned(),
            prefix: fs::canonicalize(root.join("venv"))
                .unwrap()
                .to_string_lossy()
                .into_owned(),
            base_prefix: fs::canonicalize(root.join("runtime"))
                .unwrap()
                .to_string_lossy()
                .into_owned(),
            sys_path: [
                "app",
                "runtime/lib/python3.13",
                "runtime/lib/python3.13/lib-dynload",
                "venv/lib/python3.13/site-packages",
            ]
            .into_iter()
            .map(|relative| {
                fs::canonicalize(root.join(relative))
                    .unwrap()
                    .to_string_lossy()
                    .into_owned()
            })
            .collect(),
            stdlib_sha256: manifest.sentinels.stdlib_sha256.clone(),
            site_packages_sha256: manifest.sentinels.site_packages_sha256.clone(),
            native_sha256: manifest.sentinels.native_sha256.clone(),
            runtime_overlay_sha256: verified.runtime_overlay.sha256.clone(),
            outer_runtime_manifest_sha256: verified
                .runtime_overlay
                .authority
                .outer_manifest_sha256
                .clone(),
            lifetime_lease: true,
        };
        validate_attestation(&attestation, "nonce", PythonRole::Kernel, &verified).unwrap();
        attestation.role = protocol::ROLE_DEFAULTSPACK.into();
        attestation.sys_path.push(
            fs::canonicalize(root.join("app/ecosystem/defaultspack"))
                .unwrap()
                .to_string_lossy()
                .into_owned(),
        );
        validate_attestation(&attestation, "nonce", PythonRole::Defaultspack, &verified).unwrap();
        attestation.sys_path.pop();
        assert!(
            validate_attestation(&attestation, "nonce", PythonRole::Defaultspack, &verified)
                .is_err()
        );
        attestation.role = protocol::ROLE_TYPED.into();
        attestation.sys_path.push(attestation.sys_path[0].clone());
        assert!(
            validate_attestation(&attestation, "nonce", PythonRole::Kernel, &verified).is_err()
        );
        attestation.sys_path.pop();
        attestation.environment_digest = digest('f');
        assert!(
            validate_attestation(&attestation, "nonce", PythonRole::Kernel, &verified).is_err()
        );
        fs::remove_dir_all(root).ok();
    }
}
