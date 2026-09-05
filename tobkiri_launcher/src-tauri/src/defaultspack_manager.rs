//! Lifecycle supervision for one Launcher-owned application process.
//!
//! The launcher owns only processes it starts itself. The historical
//! Defaultspack adapter remains at the composition boundary, while the
//! lifecycle state is fenced by the complete Profile execution identity.

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::process::ExitStatus;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Context, Result};
use log::{error, info, warn};
use rand::{distributions::Alphanumeric, Rng};

use crate::config::AppConfig;
use crate::debug_approval::DebugApprovalManager;
use crate::dock_registration::{spawn_defaultspack_local_server, DefaultspackDesktopMetadata};
use crate::host_broker::BrokerAttestationIdentity;
use crate::process_utils;

const DEFAULTSPACK_MONITOR_INTERVAL: Duration = Duration::from_millis(250);
const DEFAULTSPACK_RESTART_INITIAL_BACKOFF: Duration = Duration::from_millis(250);
const DEFAULTSPACK_RESTART_MAX_BACKOFF: Duration = Duration::from_secs(5);
const DEFAULTSPACK_STABLE_RUN_WINDOW: Duration = Duration::from_secs(30);
const DEFAULTSPACK_STOP_TIMEOUT: Duration = Duration::from_secs(5);
#[cfg(unix)]
const SYSTEM_KILL: &str = "/bin/kill";
#[cfg(all(test, unix))]
const SYSTEM_SHELL: &str = "/bin/sh";

fn execution_identity_matches(
    current: &crate::host_contract::ExecutionProfileIdentity,
    requested: &crate::host_contract::ExecutionProfileIdentity,
) -> bool {
    current.matches(requested)
}

/// Identity of one materialized Application instance.
///
/// The optional application fields retain compatibility with generic callers;
/// the dock metadata path populates all of them. Keeping the fields in the key
/// prevents a process from being reused for a different Application or
/// artifact merely because its Profile ID was unchanged.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ApplicationInstanceKey {
    pub(crate) application_id: Option<String>,
    pub(crate) provider_id: Option<String>,
    pub(crate) function_id: Option<String>,
    pub(crate) artifact_digest: Option<String>,
    pub(crate) execution_identity: crate::host_contract::ExecutionProfileIdentity,
}

impl ApplicationInstanceKey {
    pub(crate) fn matches(&self, other: &Self) -> bool {
        self == other
    }
}

fn application_instance_key(metadata: &DefaultspackDesktopMetadata) -> ApplicationInstanceKey {
    ApplicationInstanceKey {
        application_id: Some(metadata.application_id().to_owned()),
        provider_id: Some(metadata.provider_id().to_owned()),
        function_id: Some(metadata.function_id().to_owned()),
        artifact_digest: Some(metadata.artifact_digest().to_owned()),
        execution_identity: metadata.execution_identity().clone(),
    }
}

fn application_metadata_matches(
    current: &DefaultspackDesktopMetadata,
    requested: &DefaultspackDesktopMetadata,
) -> bool {
    application_instance_matches(
        &application_instance_key(current),
        &application_instance_key(requested),
    )
}

fn application_instance_matches(
    current: &ApplicationInstanceKey,
    requested: &ApplicationInstanceKey,
) -> bool {
    current.matches(requested)
}

/// Tracks one Application child started by this Launcher instance.
pub(crate) struct ApplicationProcessManager {
    config: AppConfig,
    shutdown_requested: Arc<AtomicBool>,
    broker_attestation: BrokerAttestationIdentity,
    debug_approval: Arc<DebugApprovalManager>,
    state: Mutex<ApplicationProcessState>,
}

#[derive(Default)]
struct ApplicationProcessState {
    child: Option<crate::python_env::PythonChild>,
    /// Process groups created by this Launcher. Keep the ids even after the
    /// direct pack-shell child exits because its Python descendant may still
    /// be serving 8766 as an orphan.
    owned_process_groups: Vec<u32>,
    launch_metadata: Option<DefaultspackDesktopMetadata>,
    restart_in_progress: bool,
    stop_requested: bool,
    consecutive_failures: u32,
    next_restart_at: Option<Instant>,
    started_at: Option<Instant>,
    active_run_id: Option<String>,
    active_guardian_pid: Option<u32>,
}

/// Compatibility alias for the existing Launcher composition root.
pub(crate) type DefaultspackManager = ApplicationProcessManager;

/// Compatibility alias for focused lifecycle tests and old internal names.
type DefaultspackState = ApplicationProcessState;

impl ApplicationProcessManager {
    pub(crate) fn new(
        config: AppConfig,
        shutdown_requested: Arc<AtomicBool>,
        broker_attestation: BrokerAttestationIdentity,
        debug_approval: Arc<DebugApprovalManager>,
    ) -> Self {
        Self {
            config,
            shutdown_requested,
            broker_attestation,
            debug_approval,
            state: Mutex::new(ApplicationProcessState::default()),
        }
    }

    /// Start Defaultspack when this Launcher does not already own a live child.
    ///
    /// A restart already in progress is reused instead of spawning a duplicate.
    pub(crate) fn start_or_reuse(&self, metadata: DefaultspackDesktopMetadata) -> Result<()> {
        let mut replaced_child = None;
        let mut replaced_run_id = None;
        let should_spawn = {
            let mut state = self.lock_state()?;
            if self.shutdown_requested.load(Ordering::SeqCst) {
                return Err(anyhow!("Defaultspack launch was requested during shutdown"));
            }

            state.stop_requested = false;
            if state.child.is_some() {
                let child_status = state
                    .child
                    .as_mut()
                    .expect("managed child was checked above")
                    .try_wait()
                    .context("failed to inspect managed Defaultspack process")?;
                match child_status {
                    None => {
                        let identity_matches =
                            state.launch_metadata.as_ref().is_some_and(|current| {
                                application_metadata_matches(current, &metadata)
                            });
                        if identity_matches {
                            info!(
                                "Defaultspack already running under Launcher supervision (pid {})",
                                state
                                    .child
                                    .as_ref()
                                    .expect("managed child is still present")
                                    .id()
                            );
                            return Ok(());
                        }
                        warn!(
                            "Managed Defaultspack identity changed; replacing the live child before reuse"
                        );
                        replaced_child = state.child.take();
                        replaced_run_id = state.active_run_id.take();
                        state.active_guardian_pid = None;
                        state.launch_metadata = None;
                        state.owned_process_groups.retain(|pid| {
                            replaced_child
                                .as_ref()
                                .map_or(true, |child| child.id() != *pid)
                        });
                    }
                    Some(status) => {
                        warn!(
                            "Managed Defaultspack exited before reuse (status {status}); starting a replacement"
                        );
                        state.child = None;
                        if let Some(run_id) = state.active_run_id.take() {
                            self.debug_approval.unregister_guardian(&run_id);
                        }
                        state.active_guardian_pid = None;
                        state.record_unexpected_exit(status);
                    }
                }
            }

            if state.restart_in_progress {
                if state
                    .launch_metadata
                    .as_ref()
                    .is_some_and(|current| !application_metadata_matches(current, &metadata))
                {
                    return Err(anyhow!(
                        "Defaultspack restart is in progress for a different execution Profile"
                    ));
                }
                info!("Defaultspack restart is already in progress; reusing it");
                return Ok(());
            }

            if state
                .next_restart_at
                .is_some_and(|restart_at| restart_at > Instant::now())
            {
                info!("Defaultspack restart is already scheduled; preserving its backoff");
                return Ok(());
            }

            state.launch_metadata = Some(metadata.clone());
            state.next_restart_at = None;
            state.restart_in_progress = true;
            true
        };

        if let Some(run_id) = replaced_run_id.as_deref() {
            self.debug_approval.unregister_guardian(run_id);
        }
        if let Some(mut child) = replaced_child {
            info!(
                "Stopping managed Defaultspack child with stale Profile identity (pid {})",
                child.id()
            );
            stop_child(&mut child)?;
        }
        if should_spawn {
            self.spawn_and_track(metadata, "initial launch")?;
        }
        Ok(())
    }

    /// Returns whether the manager owns a running process or a pending restart.
    pub(crate) fn has_managed_process(&self) -> Result<bool> {
        let mut state = self.lock_state()?;
        if let Some(child) = state.child.as_mut() {
            match child
                .try_wait()
                .context("failed to inspect managed Defaultspack process")?
            {
                None => return Ok(true),
                Some(status) => {
                    warn!("Managed Defaultspack exited with {status}; scheduling a restart");
                    state.child = None;
                    if let Some(run_id) = state.active_run_id.take() {
                        self.debug_approval.unregister_guardian(&run_id);
                    }
                    state.active_guardian_pid = None;
                    state.record_unexpected_exit(status);
                }
            }
        }

        Ok(!state.stop_requested && (state.restart_in_progress || state.launch_metadata.is_some()))
    }

    pub(crate) fn managed_child_pid(&self) -> Result<Option<u32>> {
        let state = self.lock_state()?;
        Ok(state.child.as_ref().map(|child| child.id()))
    }

    /// Stop the managed child and disable all automatic restart paths.
    pub(crate) fn stop(&self) -> Result<()> {
        let (child, owned_process_groups, active_run_id) = {
            let mut state = self.lock_state()?;
            state.stop_requested = true;
            state.launch_metadata = None;
            state.next_restart_at = None;
            state.restart_in_progress = false;
            state.consecutive_failures = 0;
            state.started_at = None;
            state.active_guardian_pid = None;
            (
                state.child.take(),
                std::mem::take(&mut state.owned_process_groups),
                state.active_run_id.take(),
            )
        };
        if let Some(run_id) = active_run_id.as_deref() {
            self.debug_approval.unregister_guardian(run_id);
        }

        let mut stopped_child_group = None;
        if let Some(mut child) = child {
            stopped_child_group = Some(child.id());
            info!("Stopping managed Defaultspack (pid {})", child.id());
            stop_child(&mut child)?;
        }

        #[cfg(unix)]
        for process_group in owned_process_groups {
            if Some(process_group) != stopped_child_group {
                stop_unix_process_group_id(process_group)?;
            }
        }

        if stopped_child_group.is_none() {
            info!("No live managed Defaultspack child remained during stop");
        }
        info!("Managed Defaultspack process groups stopped");
        Ok(())
    }

    /// Start the background monitor. It exits when Launcher shutdown begins.
    pub(crate) fn spawn_exit_monitor(manager: Arc<Self>) {
        thread::spawn(move || loop {
            if manager.shutdown_requested.load(Ordering::SeqCst) {
                break;
            }

            if let Err(error) = manager.monitor_once() {
                error!("Defaultspack lifecycle monitor failed: {error:#}");
            }
            thread::sleep(DEFAULTSPACK_MONITOR_INTERVAL);
        });
    }

    fn monitor_once(&self) -> Result<()> {
        let restart_metadata = {
            let mut state = self.lock_state()?;
            if state.stop_requested || self.shutdown_requested.load(Ordering::SeqCst) {
                return Ok(());
            }

            if let Some(child) = state.child.as_mut() {
                match child
                    .try_wait()
                    .context("failed to inspect managed Defaultspack process")?
                {
                    None => return Ok(()),
                    Some(status) => {
                        warn!(
                            "Managed Defaultspack exited unexpectedly with {status}; it will be restarted"
                        );
                        state.child = None;
                        if let Some(run_id) = state.active_run_id.take() {
                            self.debug_approval.unregister_guardian(&run_id);
                        }
                        let delay = state.record_unexpected_exit(status);
                        info!(
                            "Defaultspack restart scheduled after {} ms",
                            delay.as_millis()
                        );
                    }
                }
            }

            if state.restart_in_progress
                || state
                    .next_restart_at
                    .is_some_and(|restart_at| restart_at > Instant::now())
            {
                return Ok(());
            }

            let Some(metadata) = state.launch_metadata.clone() else {
                return Ok(());
            };
            state.restart_in_progress = true;
            Some(metadata)
        };

        if let Some(metadata) = restart_metadata {
            if let Err(error) = self.spawn_and_track(metadata, "automatic restart") {
                error!("Failed to restart Defaultspack: {error:#}");
            }
        }
        Ok(())
    }

    fn spawn_and_track(&self, metadata: DefaultspackDesktopMetadata, reason: &str) -> Result<()> {
        let run_id = managed_defaultspack_run_id();
        let mut child = match spawn_defaultspack_local_server(
            &self.config,
            &metadata,
            &self.broker_attestation,
            &run_id,
        ) {
            Ok(child) => child,
            Err(error) => {
                let delay = self.record_spawn_failure()?;
                return Err(error).with_context(|| {
                    format!(
                        "Defaultspack {reason} failed; retry is scheduled after {} ms",
                        delay.as_millis()
                    )
                });
            }
        };
        let pid = child.id();
        self.drain_child_output(&mut child, pid);
        let mut registration_error = None;

        let should_stop_child = {
            let mut state = self.lock_state()?;
            state.restart_in_progress = false;
            if state.stop_requested || self.shutdown_requested.load(Ordering::SeqCst) {
                true
            } else if state.child.is_some() {
                // Another launcher action won the race while this process was
                // being created. Keep the existing child and avoid duplication.
                true
            } else {
                if !state.owned_process_groups.contains(&pid) {
                    state.owned_process_groups.push(pid);
                }
                if let Err(error) = self.debug_approval.register_guardian(
                    run_id.clone(),
                    pid,
                    self.config.venv_python().to_string_lossy().into_owned(),
                    self.config
                        .dev_workspace_root
                        .clone()
                        .unwrap_or_else(|| metadata.working_dir().to_path_buf()),
                    metadata.port(),
                    self.config.desktop_api_token_path(),
                    metadata.execution_identity().clone(),
                ) {
                    registration_error = Some(error);
                    true
                } else {
                    state.child = Some(child);
                    state.launch_metadata = Some(metadata);
                    state.next_restart_at = None;
                    state.started_at = Some(Instant::now());
                    state.active_run_id = Some(run_id.clone());
                    state.active_guardian_pid = Some(pid);
                    info!("Defaultspack {reason} started (pid {pid})");
                    return Ok(());
                }
            }
        };

        if should_stop_child {
            // This rejected child never became the registered guardian.
            // Unregistering by run id here could revoke a different listener
            // that won the startup race.
            info!("Discarding duplicate Defaultspack process (pid {pid})");
            stop_child(&mut child)?;
        }
        if let Some(error) = registration_error {
            return Err(anyhow!(
                "failed to register Launcher-owned Defaultspack child: {error}"
            ));
        }
        Ok(())
    }

    /// Register the actual authenticated HTTP listener as the guardian after
    /// the caller proves it descends from this Launcher. `pack-shell` is only
    /// a supervision wrapper and is never used as the lease guardian.
    pub(crate) fn register_launcher_owned_listener(
        &self,
        metadata: &DefaultspackDesktopMetadata,
        process_id: u32,
        executable_identity: String,
    ) -> Result<()> {
        let (run_id, old_run_id) = {
            let mut state = self.lock_state()?;
            if state.active_guardian_pid == Some(process_id)
                && state
                    .launch_metadata
                    .as_ref()
                    .is_some_and(|current| application_metadata_matches(current, metadata))
            {
                return Ok(());
            }
            let old_run_id = state.active_run_id.take();
            let run_id = old_run_id
                .clone()
                .unwrap_or_else(managed_defaultspack_run_id);
            state.active_guardian_pid = None;
            (run_id, old_run_id)
        };
        if let Some(old_run_id) = old_run_id.as_deref() {
            self.debug_approval.unregister_guardian(old_run_id);
        }
        self.debug_approval
            .register_guardian(
                run_id.clone(),
                process_id,
                executable_identity,
                self.config
                    .dev_workspace_root
                    .clone()
                    .unwrap_or_else(|| metadata.working_dir().to_path_buf()),
                metadata.port(),
                self.config.desktop_api_token_path(),
                metadata.execution_identity().clone(),
            )
            .map_err(|error| {
                anyhow!("failed to register authenticated Defaultspack listener: {error}")
            })?;
        let mut state = self.lock_state()?;
        state.active_run_id = Some(run_id);
        state.active_guardian_pid = Some(process_id);
        state.launch_metadata = Some(metadata.clone());
        state.stop_requested = false;
        Ok(())
    }

    fn record_spawn_failure(&self) -> Result<Duration> {
        let mut state = self.lock_state()?;
        state.restart_in_progress = false;
        Ok(state.record_restart_failure())
    }

    fn drain_child_output(&self, child: &mut crate::python_env::PythonChild, pid: u32) {
        let log_path = self.config.log_dir.join("defaultspack.log");
        if let Some(stdout) = child.stdout.take() {
            spawn_output_drain(stdout, log_path.clone(), pid, "stdout");
        }
        if let Some(stderr) = child.stderr.take() {
            spawn_output_drain(stderr, log_path, pid, "stderr");
        }
    }

    fn lock_state(&self) -> Result<std::sync::MutexGuard<'_, ApplicationProcessState>> {
        self.state
            .lock()
            .map_err(|error| anyhow!("Application process manager lock poisoned: {error}"))
    }
}

fn managed_defaultspack_run_id() -> String {
    std::env::var("RUMI_DEFAULTSPACK_DEBUG_RUN_ID")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| {
            let suffix: String = rand::thread_rng()
                .sample_iter(&Alphanumeric)
                .take(32)
                .map(char::from)
                .collect();
            format!("defaultspack-{suffix}")
        })
}

impl ApplicationProcessState {
    fn record_unexpected_exit(&mut self, _status: ExitStatus) -> Duration {
        if self
            .started_at
            .is_some_and(|started_at| started_at.elapsed() >= DEFAULTSPACK_STABLE_RUN_WINDOW)
        {
            self.consecutive_failures = 0;
        }
        self.started_at = None;
        self.record_restart_failure()
    }

    fn record_restart_failure(&mut self) -> Duration {
        self.consecutive_failures = self.consecutive_failures.saturating_add(1);
        let delay = defaultspack_restart_backoff(self.consecutive_failures);
        self.next_restart_at = Some(Instant::now() + delay);
        delay
    }
}

fn defaultspack_restart_backoff(consecutive_failures: u32) -> Duration {
    let exponent = consecutive_failures.saturating_sub(1).min(5);
    let multiplier = 1_u32 << exponent;
    DEFAULTSPACK_RESTART_INITIAL_BACKOFF
        .checked_mul(multiplier)
        .unwrap_or(DEFAULTSPACK_RESTART_MAX_BACKOFF)
        .min(DEFAULTSPACK_RESTART_MAX_BACKOFF)
}

fn spawn_output_drain<R>(
    mut reader: R,
    log_path: std::path::PathBuf,
    pid: u32,
    stream: &'static str,
) where
    R: Read + Send + 'static,
{
    thread::spawn(move || {
        let mut log_file = fs::create_dir_all(
            log_path
                .parent()
                .unwrap_or_else(|| std::path::Path::new(".")),
        )
        .and_then(|_| OpenOptions::new().create(true).append(true).open(&log_path))
        .map_err(|error| {
            error!(
                "Failed to open Defaultspack {stream} log {}: {error}",
                log_path.display()
            );
            error
        })
        .ok();
        let mut buffer = [0_u8; 8192];

        loop {
            match reader.read(&mut buffer) {
                Ok(0) => break,
                Ok(read) => {
                    let output = String::from_utf8_lossy(&buffer[..read]);
                    if stream == "stderr" {
                        warn!("Defaultspack [{stream} pid={pid}]: {}", output.trim_end());
                    } else {
                        info!("Defaultspack [{stream} pid={pid}]: {}", output.trim_end());
                    }
                    if let Some(file) = log_file.as_mut() {
                        if writeln!(file, "[{stream} pid={pid}] {}", output.trim_end()).is_err() {
                            log_file = None;
                        }
                    }
                }
                Err(error) => {
                    warn!("Failed to drain Defaultspack {stream} for pid {pid}: {error}");
                    break;
                }
            }
        }
    });
}

fn stop_child(child: &mut crate::python_env::PythonChild) -> Result<()> {
    #[cfg(unix)]
    return stop_unix_process_group(child);

    #[cfg(not(unix))]
    stop_non_unix_child(child)
}

#[cfg(unix)]
fn stop_unix_process_group(child: &mut crate::python_env::PythonChild) -> Result<()> {
    let pid = child.id();
    let _ = child
        .try_wait()
        .context("failed to inspect Defaultspack process before stopping")?;

    // The pack-shell wrapper can exit before the desktop app it spawned. Wait
    // for the entire group, not only the direct child, so its 8766 listener
    // cannot survive Launcher shutdown.
    let _ = send_process_group_signal(pid, "-TERM");

    let deadline = Instant::now() + DEFAULTSPACK_STOP_TIMEOUT;
    while Instant::now() < deadline {
        let _ = child
            .try_wait()
            .context("failed to wait for Defaultspack after SIGTERM")?;
        if !process_group_exists(pid) {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(100));
    }

    let sent_kill = send_process_group_signal(pid, "-KILL");
    if child
        .try_wait()
        .context("failed to inspect Defaultspack after process-group kill")?
        .is_none()
        && !sent_kill
    {
        if let Err(error) = child.kill() {
            if child
                .try_wait()
                .context("failed to inspect Defaultspack after kill race")?
                .is_none()
            {
                return Err(error).context("failed to kill Defaultspack process");
            }
            return Ok(());
        }
    }
    if child
        .try_wait()
        .context("failed to inspect killed Defaultspack child")?
        .is_none()
    {
        child
            .wait()
            .context("failed to wait for killed Defaultspack process group")?;
    }
    Ok(())
}

#[cfg(unix)]
fn stop_unix_process_group_id(process_group: u32) -> Result<()> {
    if !process_group_exists(process_group) {
        return Ok(());
    }

    let _ = send_process_group_signal(process_group, "-TERM");
    let deadline = Instant::now() + DEFAULTSPACK_STOP_TIMEOUT;
    while Instant::now() < deadline {
        if !process_group_exists(process_group) {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(100));
    }

    let _ = send_process_group_signal(process_group, "-KILL");
    Ok(())
}

#[cfg(not(unix))]
fn stop_non_unix_child(child: &mut crate::python_env::PythonChild) -> Result<()> {
    if child
        .try_wait()
        .context("failed to inspect Defaultspack process before stopping")?
        .is_some()
    {
        return Ok(());
    }

    if let Err(error) = child.kill() {
        if child
            .try_wait()
            .context("failed to inspect Defaultspack after kill race")?
            .is_none()
        {
            return Err(error).context("failed to kill Defaultspack process");
        }
        return Ok(());
    }
    child
        .wait()
        .context("failed to wait for killed Defaultspack")?;
    Ok(())
}

#[cfg(unix)]
fn send_process_group_signal(pid: u32, signal: &str) -> bool {
    let process_group = format!("-{pid}");
    let sent = match process_utils::command(SYSTEM_KILL)
        // `--` is required by GNU kill so a negative process-group id is not
        // parsed as another option or signal number.
        .args([signal, "--", &process_group])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
    {
        Ok(status) if status.success() => true,
        Ok(_) if signal == "-0" => false,
        Ok(status) => {
            warn!(
                "Failed to send {signal} to Defaultspack process group {process_group}: {status}"
            );
            false
        }
        Err(_) if signal == "-0" => false,
        Err(error) => {
            warn!("Failed to invoke kill for Defaultspack process group {process_group}: {error}");
            false
        }
    };
    sent
}

#[cfg(unix)]
fn process_group_exists(process_group: u32) -> bool {
    if !send_process_group_signal(process_group, "-0") {
        return false;
    }

    // GitHub's Linux runner acts as a child subreaper. A terminated orphan can
    // therefore remain as a zombie briefly, and `kill -0 -- -PGID` still
    // reports that process group as present even though no code can execute.
    // Do not spend the full shutdown timeout waiting for zombie-only groups.
    #[cfg(target_os = "linux")]
    {
        return linux_process_group_has_live_members(process_group).unwrap_or(true);
    }

    #[cfg(not(target_os = "linux"))]
    true
}

#[cfg(target_os = "linux")]
fn linux_process_group_has_live_members(process_group: u32) -> std::io::Result<bool> {
    let mut observation_error = None;
    for entry in fs::read_dir("/proc")? {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                observation_error.get_or_insert(error);
                continue;
            }
        };
        if entry.file_name().to_string_lossy().parse::<u32>().is_err() {
            continue;
        }
        let stat = match fs::read_to_string(entry.path().join("stat")) {
            Ok(stat) => stat,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                observation_error.get_or_insert(error);
                continue;
            }
        };
        let Some((state, member_group)) = linux_process_state_and_group(&stat) else {
            observation_error.get_or_insert_with(|| {
                std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "invalid Linux process stat record",
                )
            });
            continue;
        };
        if member_group == process_group && !matches!(state, 'Z' | 'X') {
            return Ok(true);
        }
    }
    match observation_error {
        Some(error) => Err(error),
        None => Ok(false),
    }
}

#[cfg(target_os = "linux")]
fn linux_process_state_and_group(stat: &str) -> Option<(char, u32)> {
    // `/proc/<pid>/stat` wraps the executable name in parentheses; the name
    // may itself contain spaces or parentheses, so split after the last `)`.
    let (_, fields) = stat.rsplit_once(") ")?;
    let mut fields = fields.split_whitespace();
    let state = fields.next()?.chars().next()?;
    let _parent_pid = fields.next()?;
    let process_group = fields.next()?.parse().ok()?;
    Some((state, process_group))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[cfg(unix)]
    fn process_id_is_live(process_id: u32) -> bool {
        #[cfg(target_os = "linux")]
        {
            return fs::read_to_string(format!("/proc/{process_id}/stat"))
                .ok()
                .and_then(|stat| linux_process_state_and_group(&stat))
                .is_some_and(|(state, _)| !matches!(state, 'Z' | 'X'));
        }

        #[cfg(not(target_os = "linux"))]
        process_utils::command(SYSTEM_KILL)
            .args(["-0", &process_id.to_string()])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .is_ok_and(|status| status.success())
    }

    fn test_config() -> AppConfig {
        AppConfig {
            app_dir: PathBuf::from("/tmp/defaultspack-manager-test/runtime"),
            rumi_home: PathBuf::from("/tmp/defaultspack-manager-test/runtime"),
            python_dir: PathBuf::from("/tmp/defaultspack-manager-test/python"),
            uv_path: PathBuf::from("/tmp/defaultspack-manager-test/uv"),
            venv_dir: PathBuf::from("/tmp/defaultspack-manager-test/venv"),
            user_data_dir: PathBuf::from("/tmp/defaultspack-manager-test/user_data"),
            log_dir: PathBuf::from("/tmp/defaultspack-manager-test/logs"),
            kernel_port: 8765,
            dev_workspace_root: None,
        }
    }

    fn test_manager() -> DefaultspackManager {
        let config = test_config();
        let debug_approval = Arc::new(DebugApprovalManager::new(
            config.log_dir.join("debug-approval-test.jsonl"),
        ));
        DefaultspackManager::new(
            config,
            Arc::new(AtomicBool::new(false)),
            BrokerAttestationIdentity::generate(),
            debug_approval,
        )
    }

    fn test_execution_identity(
        profile_id: &str,
        profile_revision: &str,
        activation_id: &str,
        plan_digest: &str,
    ) -> crate::host_contract::ExecutionProfileIdentity {
        crate::host_contract::ExecutionProfileIdentity::new(
            profile_id,
            format!("sha256:{profile_revision}"),
            format!("activation:{activation_id}"),
            format!("sha256:{plan_digest}"),
        )
        .unwrap()
    }

    #[test]
    fn manager_reuse_requires_the_complete_execution_profile_identity() {
        let current = test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-test",
            &"b".repeat(64),
        );
        let requested = test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-next-activation",
            &"b".repeat(64),
        );
        assert!(!execution_identity_matches(&current, &requested));
        assert!(execution_identity_matches(&current, &current));
    }

    #[test]
    fn application_instance_key_fences_application_and_artifact_identity() {
        let identity = test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-test",
            &"b".repeat(64),
        );
        let current = ApplicationInstanceKey {
            application_id: Some("application.alpha".into()),
            provider_id: Some("provider.alpha".into()),
            function_id: Some("function.alpha".into()),
            artifact_digest: Some(format!("sha256:{}", "c".repeat(64))),
            execution_identity: identity.clone(),
        };
        let mut different_application = current.clone();
        different_application.application_id = Some("application.beta".into());
        let mut different_artifact = current.clone();
        different_artifact.artifact_digest = Some(format!("sha256:{}", "d".repeat(64)));
        let mut unknown_activation = current.clone();
        unknown_activation.execution_identity = test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-next",
            &"b".repeat(64),
        );

        assert!(application_instance_matches(&current, &current));
        assert!(!application_instance_matches(
            &current,
            &different_application
        ));
        assert!(!application_instance_matches(&current, &different_artifact));
        assert!(!application_instance_matches(&current, &unknown_activation));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_process_stat_parser_handles_parentheses_in_command_name() {
        assert_eq!(
            linux_process_state_and_group("123 (worker ) helper) Z 1 77 77 0"),
            Some(('Z', 77))
        );
    }

    #[test]
    fn restart_backoff_is_bounded() {
        assert_eq!(defaultspack_restart_backoff(1), Duration::from_millis(250));
        assert_eq!(defaultspack_restart_backoff(2), Duration::from_millis(500));
        assert_eq!(defaultspack_restart_backoff(6), Duration::from_secs(5));
        assert_eq!(
            defaultspack_restart_backoff(u32::MAX),
            Duration::from_secs(5)
        );
    }

    #[cfg(unix)]
    #[test]
    fn successful_exit_still_schedules_a_restart() {
        let status = std::process::Command::new("sh")
            .args(["-c", "exit 0"])
            .status()
            .unwrap();
        assert!(status.success());

        let mut state = DefaultspackState::default();
        let delay = state.record_unexpected_exit(status);

        assert_eq!(delay, DEFAULTSPACK_RESTART_INITIAL_BACKOFF);
        assert!(state.next_restart_at.is_some());
    }

    #[cfg(unix)]
    #[test]
    fn stable_run_resets_the_restart_penalty() {
        let mut state = DefaultspackState {
            consecutive_failures: 4,
            started_at: Some(Instant::now() - DEFAULTSPACK_STABLE_RUN_WINDOW),
            ..Default::default()
        };

        let status = std::process::Command::new("sh")
            .args(["-c", "exit 0"])
            .status()
            .unwrap();
        let delay = state.record_unexpected_exit(status);

        assert_eq!(delay, DEFAULTSPACK_RESTART_INITIAL_BACKOFF);
    }

    #[cfg(unix)]
    #[test]
    fn explicit_stop_terminates_the_defaultspack_process_group() {
        let manager = test_manager();
        let pid_file = std::env::temp_dir().join(format!(
            "defaultspack-manager-child-{}-{}.pid",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let script = format!(
            "sleep 30 & child=$!; printf '%s' \"$child\" > {}; exit 0",
            pid_file.display()
        );
        let mut command = process_utils::command(SYSTEM_SHELL);
        command.args(["-c", &script]);
        crate::dock_registration::configure_defaultspack_process_group(&mut command);
        let child = command.spawn().unwrap();
        {
            let mut state = manager.lock_state().unwrap();
            state.owned_process_groups.push(child.id());
            state.child = Some(crate::python_env::PythonChild::development(child));
            state.restart_in_progress = true;
        }

        let descendant_pid = (0..20)
            .find_map(|_| {
                let result = fs::read_to_string(&pid_file)
                    .ok()
                    .and_then(|pid| pid.trim().parse::<u32>().ok());
                if result.is_none() {
                    thread::sleep(Duration::from_millis(25));
                }
                result
            })
            .expect("shell did not record its Defaultspack descendant pid");

        let shell_exited = (0..20).any(|_| {
            let exited = manager
                .lock_state()
                .unwrap()
                .child
                .as_mut()
                .unwrap()
                .try_wait()
                .unwrap()
                .is_some();
            if !exited {
                thread::sleep(Duration::from_millis(25));
            }
            exited
        });
        assert!(shell_exited, "pack-shell wrapper did not exit before stop");

        manager.stop().unwrap();

        let state = manager.lock_state().unwrap();
        assert!(state.stop_requested);
        assert!(!state.restart_in_progress);
        assert!(state.child.is_none());
        assert!(state.owned_process_groups.is_empty());
        assert!(state.launch_metadata.is_none());
        drop(state);
        let descendant_alive = process_id_is_live(descendant_pid);
        fs::remove_file(pid_file).ok();
        assert!(
            !descendant_alive,
            "Defaultspack descendant {descendant_pid} survived process-group shutdown"
        );
    }

    #[cfg(unix)]
    #[test]
    fn explicit_stop_terminates_an_orphaned_owned_process_group() {
        let manager = test_manager();
        let pid_file = std::env::temp_dir().join(format!(
            "defaultspack-manager-orphan-{}-{}.pid",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let script = format!(
            "sleep 30 & child=$!; printf '%s' \"$child\" > {}; exit 0",
            pid_file.display()
        );
        let mut command = process_utils::command(SYSTEM_SHELL);
        command.args(["-c", &script]);
        crate::dock_registration::configure_defaultspack_process_group(&mut command);
        let mut child = command.spawn().unwrap();
        let process_group = child.id();
        child.wait().unwrap();

        let descendant_pid = (0..20)
            .find_map(|_| {
                let result = fs::read_to_string(&pid_file)
                    .ok()
                    .and_then(|pid| pid.trim().parse::<u32>().ok());
                if result.is_none() {
                    thread::sleep(Duration::from_millis(25));
                }
                result
            })
            .expect("shell did not record its orphaned Defaultspack descendant pid");
        {
            let mut state = manager.lock_state().unwrap();
            state.owned_process_groups.push(process_group);
        }

        manager.stop().unwrap();

        let descendant_alive = process_id_is_live(descendant_pid);
        fs::remove_file(pid_file).ok();
        assert!(
            !descendant_alive,
            "orphaned Defaultspack descendant {descendant_pid} survived shutdown"
        );
    }
}
