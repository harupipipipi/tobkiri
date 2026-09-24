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
// Resolution failures retry with the standard restart backoff for roughly
// the same ~5-minute budget a cold Kernel needs to commit runtime authority,
// then the pending launch is abandoned with a terminal log instead of
// retrying silently forever.
const DEFAULTSPACK_MAX_RESOLUTION_FAILURES: u32 = 60;
// Recheck cadence while an adopted Launcher-owned listener still owns the
// port. A respawn against it can only fail with EADDRINUSE, so the monitor
// defers instead of churning spawn attempts.
const DEFAULTSPACK_ADOPTED_LISTENER_RECHECK_INTERVAL: Duration = Duration::from_secs(5);
// Bound the restart churn while an unowned process squats on the port.
// After this many consecutive failures the pending launch is abandoned; an
// explicit launch request re-seeds the metadata and may retry.
const DEFAULTSPACK_MAX_OCCUPIED_PORT_RESTARTS: u32 = 60;
// Leave enough of the product's five-second quit budget for the desktop
// shells to observe the stopped listeners and terminate after this group.
const DEFAULTSPACK_STOP_TIMEOUT: Duration = Duration::from_secs(2);
#[cfg(unix)]
// A loaded macOS runner has taken longer than 750 ms to report every group
// member gone after SIGKILL. Keep the observation bounded inside the product's
// five-second quit contract without treating signal delivery as termination.
const DEFAULTSPACK_FORCE_KILL_TIMEOUT: Duration = Duration::from_millis(1_500);
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
    /// Consecutive authority-resolution failures while a restart was
    /// pending. Bounded separately so an unresolvable active Profile cannot
    /// retry silently forever.
    consecutive_resolve_failures: u32,
    next_restart_at: Option<Instant>,
    started_at: Option<Instant>,
    active_run_id: Option<String>,
    active_guardian_pid: Option<u32>,
}

/// Compatibility alias for the existing Launcher composition root.
pub(crate) type DefaultspackManager = ApplicationProcessManager;

/// Compatibility alias for focused lifecycle tests and old internal names.
type DefaultspackState = ApplicationProcessState;

/// Who currently holds the Defaultspack port when a restart comes due.
enum PendingRestartPort {
    /// The port is free or owned by the still-managed child.
    Free,
    /// An adopted Launcher-owned listener still owns the port.
    AdoptedListener(u32),
    /// A process outside Launcher ownership holds the port.
    Occupied { pid: u32, port: u16 },
}

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
                if state.launch_metadata.as_ref().map_or(true, |current| {
                    !application_metadata_matches(current, &metadata)
                }) {
                    // A restart for a superseded execution Profile can never
                    // satisfy this request. Adopt the freshly resolved
                    // metadata so the in-flight spawn is discarded and the
                    // next respawn publishes the active Host contract.
                    state.launch_metadata = Some(metadata.clone());
                    info!(
                        "Defaultspack restart is in progress for a superseded execution Profile; adopting the active one"
                    );
                    return Ok(());
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
        let (child, owned_process_groups, active_run_id, adopted_listener) = {
            let mut state = self.lock_state()?;
            state.stop_requested = true;
            let adopted_listener = state.active_guardian_pid.take().and_then(|pid| {
                state
                    .launch_metadata
                    .as_ref()
                    .map(|metadata| (pid, metadata.port()))
            });
            state.launch_metadata = None;
            state.next_restart_at = None;
            state.restart_in_progress = false;
            state.consecutive_failures = 0;
            state.consecutive_resolve_failures = 0;
            state.started_at = None;
            (
                state.child.take(),
                std::mem::take(&mut state.owned_process_groups),
                state.active_run_id.take(),
                adopted_listener,
            )
        };
        if let Some(run_id) = active_run_id.as_deref() {
            self.debug_approval.unregister_guardian(run_id);
        }

        let had_live_child = child.is_some();
        let child_pid = child.as_ref().map(crate::python_env::PythonChild::id);

        #[cfg(unix)]
        stop_owned_unix_process_groups(child, owned_process_groups)?;

        #[cfg(not(unix))]
        {
            let _ = owned_process_groups;
            if let Some(mut child) = child {
                info!("Stopping managed Defaultspack (pid {})", child.id());
                stop_child(&mut child)?;
            }
        }

        // A guardian adopted outside the managed child is only covered by the
        // owned groups when it happens to lead one; reap it by pid otherwise.
        if let Some((pid, port)) = adopted_listener {
            if Some(pid) != child_pid {
                if let Err(error) = stop_adopted_defaultspack_listener(pid, port) {
                    warn!("Failed to stop adopted Defaultspack listener {pid}: {error:#}");
                }
            }
        }

        if !had_live_child {
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
        let restart_needed = {
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

            if state.launch_metadata.is_none() {
                return Ok(());
            }
            state.restart_in_progress = true;
            true
        };

        if !restart_needed {
            return Ok(());
        }

        // A respawn can only claim the port when it is free. Defer while an
        // adopted Launcher-owned listener still serves it, and bound the
        // churn while an unowned process squats on it. An inspection failure
        // takes the standard backoff so `restart_in_progress` never wedges.
        let port_gate = match self.pending_restart_port_gate() {
            Ok(gate) => gate,
            Err(error) => {
                let delay = self.record_spawn_failure()?;
                warn!(
                    "Defaultspack restart deferred for {} ms; port ownership could not be inspected: {error:#}",
                    delay.as_millis()
                );
                return Ok(());
            }
        };
        match port_gate {
            PendingRestartPort::AdoptedListener(pid) => {
                info!(
                    "Defaultspack restart deferred: adopted listener pid {pid} still owns the port"
                );
                let mut state = self.lock_state()?;
                state.restart_in_progress = false;
                state.next_restart_at =
                    Some(Instant::now() + DEFAULTSPACK_ADOPTED_LISTENER_RECHECK_INTERVAL);
                return Ok(());
            }
            PendingRestartPort::Occupied { pid, port } => {
                let mut state = self.lock_state()?;
                state.restart_in_progress = false;
                if state.consecutive_failures >= DEFAULTSPACK_MAX_OCCUPIED_PORT_RESTARTS {
                    error!(
                        "Defaultspack restart abandoned after {} consecutive failures; port {port} is held by unowned listener pid {pid} and must be freed before the next launch request",
                        state.consecutive_failures
                    );
                    state.launch_metadata = None;
                    state.next_restart_at = None;
                } else {
                    let delay = state.record_restart_failure();
                    warn!(
                        "Defaultspack restart deferred for {} ms; port {port} is held by unowned listener pid {pid} ({}/{})",
                        delay.as_millis(),
                        state.consecutive_failures,
                        DEFAULTSPACK_MAX_OCCUPIED_PORT_RESTARTS
                    );
                }
                return Ok(());
            }
            PendingRestartPort::Free => {}
        }

        // Re-resolve the launch metadata instead of replaying the stored copy:
        // a Profile rotation (pack enable/disable, recapture) commits a new
        // execution identity, and respawning the stale one would publish a
        // Host contract the restored session cannot match.
        let metadata =
            match crate::dock_registration::read_defaultspack_desktop_metadata(&self.config) {
                Ok(metadata) => metadata,
                Err(error) => {
                    self.defer_restart_for_resolution_failure(&error)?;
                    return Ok(());
                }
            };

        // The authority can rotate while a resolution is in flight. Read it
        // once more before this result may overwrite an identity adopted by a
        // concurrent launch, so an older in-flight resolution can never
        // displace a newer one.
        match crate::dock_registration::read_defaultspack_desktop_metadata(&self.config) {
            Ok(current) if application_metadata_matches(&current, &metadata) => {}
            Ok(_) => {
                info!(
                    "Defaultspack execution Profile rotated while restart metadata was in flight; deferring to the next monitor pass"
                );
                self.clear_restart_in_progress()?;
                return Ok(());
            }
            Err(error) => {
                self.defer_restart_for_resolution_failure(&error)?;
                return Ok(());
            }
        }

        {
            let mut state = self.lock_state()?;
            state.consecutive_resolve_failures = 0;
            if state.stop_requested
                || self.shutdown_requested.load(Ordering::SeqCst)
                || state.child.is_some()
            {
                state.restart_in_progress = false;
                return Ok(());
            }
            if state
                .launch_metadata
                .as_ref()
                .is_some_and(|current| !application_metadata_matches(current, &metadata))
            {
                info!(
                    "Defaultspack execution Profile rotated; restarting under the refreshed identity"
                );
            }
            state.launch_metadata = Some(metadata.clone());
        }

        if let Err(error) = self.spawn_and_track(metadata, "automatic restart") {
            error!("Failed to restart Defaultspack: {error:#}");
        }
        Ok(())
    }

    fn spawn_and_track(&self, metadata: DefaultspackDesktopMetadata, reason: &str) -> Result<()> {
        let run_id = managed_defaultspack_run_id();
        // The authority can rotate between the caller's resolution and this
        // spawn. Re-verify the resolved identity immediately before the Host
        // contract is written so a superseded execution identity is never
        // bound into a new child.
        match crate::dock_registration::read_defaultspack_desktop_metadata(&self.config) {
            Ok(current) if application_metadata_matches(&current, &metadata) => {}
            Ok(_) => {
                info!(
                    "Defaultspack {reason} aborted; the active execution Profile rotated before the Host contract was written"
                );
                self.clear_restart_in_progress()?;
                return Err(anyhow!(
                    "Defaultspack {reason} used a superseded execution Profile"
                ));
            }
            Err(error) => {
                self.defer_restart_for_resolution_failure(&error)?;
                return Err(error).context(format!(
                    "Defaultspack {reason} could not re-verify the active execution Profile"
                ));
            }
        }
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

        // The authority may also have rotated while the child was being
        // created; confirm the spawned identity once more before it is
        // registered as the guardian and allowed to own `state.child`.
        let spawned_identity_is_current =
            match crate::dock_registration::read_defaultspack_desktop_metadata(&self.config) {
                Ok(current) => application_metadata_matches(&current, &metadata),
                Err(error) => {
                    warn!(
                        "Defaultspack {reason} could not re-verify the active execution Profile after spawn: {error:#}"
                    );
                    if let Err(stop_error) = stop_child(&mut child) {
                        warn!(
                            "Failed to stop the unverifiable Defaultspack child {pid}: {stop_error:#}"
                        );
                    }
                    self.defer_restart_for_resolution_failure(&error)?;
                    return Err(anyhow!(
                        "Defaultspack {reason} discarded a child whose execution Profile could not be re-verified"
                    ));
                }
            };
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
            } else if !spawned_identity_is_current {
                // The authority rotated while this process was being created;
                // registering it would pin a superseded identity.
                true
            } else if state
                .launch_metadata
                .as_ref()
                .is_some_and(|current| !application_metadata_matches(current, &metadata))
            {
                // A newer execution Profile was adopted while this process was
                // being created; registering it would pin stale identity and
                // Host-contract state onto a superseded child.
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
                    // Registration failures share the restart backoff so a
                    // persistently rejected guardian cannot loop unaccounted.
                    let delay = state.record_restart_failure();
                    info!(
                        "Defaultspack guardian registration failed; retry is scheduled after {} ms",
                        delay.as_millis()
                    );
                    true
                } else {
                    state.child = Some(child);
                    state.launch_metadata = Some(metadata);
                    state.next_restart_at = None;
                    state.consecutive_resolve_failures = 0;
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
        if !state.owned_process_groups.contains(&process_id) {
            // A listener adopted outside the managed child may still lead its
            // own process group; recording it lets stop() reap that group.
            state.owned_process_groups.push(process_id);
        }
        state.launch_metadata = Some(metadata.clone());
        state.stop_requested = false;
        Ok(())
    }

    fn record_spawn_failure(&self) -> Result<Duration> {
        let mut state = self.lock_state()?;
        state.restart_in_progress = false;
        Ok(state.record_restart_failure())
    }

    /// Defer the pending restart after an authority-resolution failure.
    ///
    /// Resolution failures share the restart backoff so the monitor cannot
    /// spin on a persistently unresolvable Profile. After
    /// `DEFAULTSPACK_MAX_RESOLUTION_FAILURES` consecutive failures the
    /// pending launch is abandoned with a terminal log; a later launch
    /// request can still re-seed `launch_metadata` and retry.
    fn defer_restart_for_resolution_failure(&self, error: &anyhow::Error) -> Result<()> {
        let mut state = self.lock_state()?;
        state.restart_in_progress = false;
        state.consecutive_resolve_failures = state.consecutive_resolve_failures.saturating_add(1);
        let delay = state.record_restart_failure();
        if state.consecutive_resolve_failures >= DEFAULTSPACK_MAX_RESOLUTION_FAILURES {
            error!(
                "Defaultspack restart abandoned after {} consecutive execution-Profile resolution failures; a new launch request is required (last error: {error:#})",
                state.consecutive_resolve_failures
            );
            state.launch_metadata = None;
            state.next_restart_at = None;
        } else {
            warn!(
                "Defaultspack restart deferred for {} ms; the active execution Profile cannot be resolved ({}/{}): {error:#}",
                delay.as_millis(),
                state.consecutive_resolve_failures,
                DEFAULTSPACK_MAX_RESOLUTION_FAILURES
            );
        }
        Ok(())
    }

    /// Release the in-progress restart marker without scheduling a penalty
    /// so the next monitor pass re-resolves the current authority.
    fn clear_restart_in_progress(&self) -> Result<()> {
        let mut state = self.lock_state()?;
        state.restart_in_progress = false;
        Ok(())
    }

    /// Inspect the configured port before a pending respawn.
    ///
    /// The port check runs outside the state lock and is only reached when a
    /// restart is actually due, so it cannot slow the common monitor path.
    fn pending_restart_port_gate(&self) -> Result<PendingRestartPort> {
        let (guardian_pid, port) = {
            let state = self.lock_state()?;
            if state.child.is_some() {
                return Ok(PendingRestartPort::Free);
            }
            let Some(metadata) = state.launch_metadata.as_ref() else {
                return Ok(PendingRestartPort::Free);
            };
            (state.active_guardian_pid, metadata.port())
        };
        match crate::kernel_manager::detect_port_listener(port)? {
            None => Ok(PendingRestartPort::Free),
            Some(listener) if Some(listener.pid) == guardian_pid => {
                Ok(PendingRestartPort::AdoptedListener(listener.pid))
            }
            Some(listener) => Ok(PendingRestartPort::Occupied {
                pid: listener.pid,
                port,
            }),
        }
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

#[cfg(unix)]
fn stop_owned_unix_process_groups(
    mut child: Option<crate::python_env::PythonChild>,
    owned_process_groups: Vec<u32>,
) -> Result<()> {
    let child_group = child.as_ref().map(crate::python_env::PythonChild::id);
    let mut groups = owned_process_groups
        .into_iter()
        .filter(|process_group| Some(*process_group) != child_group)
        .collect::<Vec<_>>();
    groups.sort_unstable();
    groups.dedup();

    // A wrapper can exit while its process group remains alive, and multiple
    // previously-owned groups can therefore be retained for shutdown. Their
    // grace windows are independent. Stop them concurrently so the product's
    // fixed quit budget is not multiplied by the number of owned groups.
    thread::scope(|scope| -> Result<()> {
        let child_stop = child.as_mut().map(|child| {
            info!("Stopping managed Defaultspack (pid {})", child.id());
            scope.spawn(move || stop_child(child))
        });
        let group_stops = groups
            .into_iter()
            .map(|process_group| scope.spawn(move || stop_unix_process_group_id(process_group)))
            .collect::<Vec<_>>();

        if let Some(stop) = child_stop {
            stop.join()
                .map_err(|_| anyhow!("Defaultspack child stop worker panicked"))??;
        }
        for stop in group_stops {
            stop.join()
                .map_err(|_| anyhow!("Defaultspack process-group stop worker panicked"))??;
        }
        Ok(())
    })
}

/// Stop a guardian listener that was adopted outside the managed child.
/// The pid+port check fences signal delivery against pid reuse.
fn stop_adopted_defaultspack_listener(pid: u32, port: u16) -> Result<()> {
    match crate::kernel_manager::detect_port_listener(port)? {
        Some(listener) if listener.pid == pid => {
            crate::kernel_manager::terminate_external_listener(pid, port)
        }
        _ => Ok(()),
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
    return stop_unix_process_group(child, "Defaultspack");

    #[cfg(not(unix))]
    stop_non_unix_child(child)
}

/// Graceful Unix shutdown: TERM -> bounded wait -> KILL -> zombie-aware
/// confirmation. `label` identifies the process in logs and errors. The
/// child normally leads its own process group so descendants exit with it;
/// a child spawned without a group falls back to direct-pid signalling.
#[cfg(unix)]
pub(crate) fn stop_unix_process_group(
    child: &mut crate::python_env::PythonChild,
    label: &str,
) -> Result<()> {
    let pid = child.id();
    let _ = child
        .try_wait()
        .with_context(|| format!("failed to inspect {label} process before stopping"))?;

    // The pack-shell wrapper can exit before the desktop app it spawned. Wait
    // for the entire group, not only the direct child, so its listener cannot
    // survive Launcher shutdown.
    let group_leader = process_group_exists(pid);
    if group_leader {
        let _ = send_process_group_signal(pid, "-TERM");
    } else {
        let _ = send_unix_process_signal(pid, "-TERM");
    }

    let deadline = Instant::now() + DEFAULTSPACK_STOP_TIMEOUT;
    while Instant::now() < deadline {
        let child_status = child
            .try_wait()
            .with_context(|| format!("failed to wait for {label} after SIGTERM"))?;
        let exited = if group_leader {
            !process_group_exists(pid)
        } else {
            child_status.is_some()
        };
        if exited {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(100));
    }

    let sent_kill = if group_leader {
        send_process_group_signal(pid, "-KILL")
    } else {
        send_unix_process_signal(pid, "-KILL")
    };
    if child
        .try_wait()
        .with_context(|| format!("failed to inspect {label} after process-group kill"))?
        .is_none()
        && !sent_kill
    {
        if let Err(error) = child.kill() {
            if child
                .try_wait()
                .with_context(|| format!("failed to inspect {label} after kill race"))?
                .is_none()
            {
                return Err(error).with_context(|| format!("failed to kill {label} process"));
            }
            return Ok(());
        }
    }
    if child
        .try_wait()
        .with_context(|| format!("failed to inspect killed {label} child"))?
        .is_none()
    {
        child
            .wait()
            .with_context(|| format!("failed to wait for killed {label} process group"))?;
    }
    if group_leader && !wait_for_process_group_exit(pid, DEFAULTSPACK_FORCE_KILL_TIMEOUT) {
        return Err(anyhow!(
            "{label} process group {pid} remained live after SIGKILL"
        ));
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

    let sent_kill = send_process_group_signal(process_group, "-KILL");
    if !sent_kill && process_group_exists(process_group) {
        return Err(anyhow!(
            "failed to kill Defaultspack process group {process_group}"
        ));
    }
    if !wait_for_process_group_exit(process_group, DEFAULTSPACK_FORCE_KILL_TIMEOUT) {
        return Err(anyhow!(
            "Defaultspack process group {process_group} remained live after SIGKILL"
        ));
    }
    Ok(())
}

#[cfg(unix)]
fn wait_for_process_group_exit(process_group: u32, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if !process_group_exists(process_group) {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        thread::sleep(Duration::from_millis(25));
    }
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

/// Signal every member of the Unix process group led by `pid`. Returns
/// false when the group is already gone or the signal could not be sent.
#[cfg(unix)]
pub(crate) fn send_process_group_signal(pid: u32, signal: &str) -> bool {
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

/// Signal a single Unix process by pid. Returns false when the process is
/// already gone or the signal could not be sent.
#[cfg(unix)]
pub(crate) fn send_unix_process_signal(pid: u32, signal: &str) -> bool {
    match process_utils::command(SYSTEM_KILL)
        // `--` keeps a signal-looking argument shape identical to the
        // process-group variant.
        .args([signal, "--", &pid.to_string()])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
    {
        Ok(status) if status.success() => true,
        Ok(_) if signal == "-0" => false,
        Ok(status) => {
            warn!("Failed to send {signal} to process {pid}: {status}");
            false
        }
        Err(_) if signal == "-0" => false,
        Err(error) => {
            warn!("Failed to invoke kill for process {pid}: {error}");
            false
        }
    }
}

#[cfg(unix)]
pub(crate) fn process_group_exists(process_group: u32) -> bool {
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

    #[cfg(target_os = "macos")]
    {
        return macos_process_group_has_live_members(process_group).unwrap_or(true);
    }

    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    true
}

#[cfg(target_os = "macos")]
fn macos_process_group_has_live_members(process_group: u32) -> std::io::Result<bool> {
    if process_group == 0 || process_group > i32::MAX as u32 {
        return Err(std::io::Error::other("invalid macOS process group"));
    }
    let suggested =
        unsafe { libc::proc_listpgrppids(process_group as i32, std::ptr::null_mut(), 0) };
    if suggested <= 0 {
        return Ok(false);
    }
    let capacity = (suggested as usize).saturating_mul(2).max(64);
    let mut pids = vec![0_i32; capacity];
    let count = unsafe {
        libc::proc_listpgrppids(
            process_group as i32,
            pids.as_mut_ptr() as *mut libc::c_void,
            (pids.len() * std::mem::size_of::<i32>()) as i32,
        )
    };
    if count < 0 {
        return Err(std::io::Error::last_os_error());
    }
    for pid in pids.into_iter().take(count as usize).filter(|pid| *pid > 0) {
        let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
        let expected = std::mem::size_of::<libc::proc_bsdinfo>();
        let received = unsafe {
            libc::proc_pidinfo(
                pid,
                libc::PROC_PIDTBSDINFO,
                0,
                &mut info as *mut libc::proc_bsdinfo as *mut libc::c_void,
                expected as i32,
            )
        };
        if received == 0 {
            continue;
        }
        if received != expected as i32 || info.pbi_pid != pid as u32 {
            return Err(std::io::Error::other(
                "macOS returned an invalid process-group member",
            ));
        }
        if info.pbi_status != libc::SZOMB {
            return Ok(true);
        }
    }
    Ok(false)
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
    fn start_or_reuse_supersedes_a_restart_for_a_rotated_profile() {
        let manager = test_manager();
        let stale = DefaultspackDesktopMetadata::test_metadata(test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-stale",
            &"b".repeat(64),
        ));
        let requested = DefaultspackDesktopMetadata::test_metadata(test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-active",
            &"b".repeat(64),
        ));
        {
            let mut state = manager.lock_state().unwrap();
            state.restart_in_progress = true;
            state.launch_metadata = Some(stale.clone());
        }

        manager.start_or_reuse(requested.clone()).unwrap();

        let state = manager.lock_state().unwrap();
        assert!(state.restart_in_progress);
        assert!(application_metadata_matches(
            state.launch_metadata.as_ref().unwrap(),
            &requested
        ));
    }

    #[test]
    fn start_or_reuse_supersedes_a_restart_without_recorded_metadata() {
        let manager = test_manager();
        let requested = DefaultspackDesktopMetadata::test_metadata(test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-active",
            &"b".repeat(64),
        ));
        {
            let mut state = manager.lock_state().unwrap();
            state.restart_in_progress = true;
            state.launch_metadata = None;
        }

        manager.start_or_reuse(requested.clone()).unwrap();

        let state = manager.lock_state().unwrap();
        assert!(state.restart_in_progress);
        assert!(application_metadata_matches(
            state.launch_metadata.as_ref().unwrap(),
            &requested
        ));
    }

    #[test]
    fn start_or_reuse_reuses_a_restart_for_the_same_profile() {
        let manager = test_manager();
        let identity = test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-active",
            &"b".repeat(64),
        );
        let in_flight = DefaultspackDesktopMetadata::test_metadata(identity.clone());
        {
            let mut state = manager.lock_state().unwrap();
            state.restart_in_progress = true;
            state.launch_metadata = Some(in_flight);
        }

        manager
            .start_or_reuse(DefaultspackDesktopMetadata::test_metadata(identity.clone()))
            .unwrap();

        let state = manager.lock_state().unwrap();
        assert!(state.restart_in_progress);
        assert!(application_metadata_matches(
            state.launch_metadata.as_ref().unwrap(),
            &DefaultspackDesktopMetadata::test_metadata(identity)
        ));
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

        let started = Instant::now();
        manager.stop().unwrap();
        assert!(
            started.elapsed() < Duration::from_secs(4),
            "orphaned process-group shutdown exceeded the responsive bound"
        );

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
    fn explicit_stop_force_kills_a_group_with_a_term_ignoring_child() {
        let manager = test_manager();
        let ready_file = std::env::temp_dir().join(format!(
            "defaultspack-manager-term-ignore-{}-{}.ready",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let script = format!(
            "trap '' TERM; printf ready > {}; while :; do sleep 1; done",
            ready_file.display()
        );
        let mut command = process_utils::command(SYSTEM_SHELL);
        command.args(["-c", &script]);
        crate::dock_registration::configure_defaultspack_process_group(&mut command);
        let child = command.spawn().unwrap();
        let process_group = child.id();
        {
            let mut state = manager.lock_state().unwrap();
            state.owned_process_groups.push(process_group);
            state.child = Some(crate::python_env::PythonChild::development(child));
        }
        assert!((0..40).any(|_| {
            if ready_file.exists() {
                return true;
            }
            thread::sleep(Duration::from_millis(25));
            false
        }));

        let started = Instant::now();
        manager.stop().unwrap();
        fs::remove_file(ready_file).ok();

        assert!(
            started.elapsed() < Duration::from_secs(3),
            "forced process-group shutdown exceeded its share of the quit budget"
        );
        assert!(!process_group_exists(process_group));
    }

    #[cfg(unix)]
    #[test]
    fn explicit_stop_shares_one_quit_window_across_owned_process_groups() {
        let manager = test_manager();
        let unique = format!(
            "{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let ready_a =
            std::env::temp_dir().join(format!("defaultspack-manager-concurrent-a-{unique}.ready"));
        let ready_b =
            std::env::temp_dir().join(format!("defaultspack-manager-concurrent-b-{unique}.ready"));
        let spawn_group = |ready: &std::path::Path| {
            let script = format!(
                "trap '' TERM; printf ready > {}; while :; do sleep 1; done",
                ready.display()
            );
            let mut command = process_utils::command(SYSTEM_SHELL);
            command.args(["-c", &script]);
            crate::dock_registration::configure_defaultspack_process_group(&mut command);
            command.spawn().unwrap()
        };
        let child_a = spawn_group(&ready_a);
        let mut child_b = spawn_group(&ready_b);
        let group_a = child_a.id();
        let group_b = child_b.id();
        {
            let mut state = manager.lock_state().unwrap();
            state.owned_process_groups.extend([group_a, group_b]);
            state.child = Some(crate::python_env::PythonChild::development(child_a));
        }
        assert!((0..40).any(|_| {
            if ready_a.exists() && ready_b.exists() {
                return true;
            }
            thread::sleep(Duration::from_millis(25));
            false
        }));

        let started = Instant::now();
        manager.stop().unwrap();
        let elapsed = started.elapsed();
        let _ = child_b.wait();
        fs::remove_file(ready_a).ok();
        fs::remove_file(ready_b).ok();

        assert!(
            elapsed < Duration::from_secs(4),
            "owned process-group grace windows accumulated to {elapsed:?}"
        );
        assert!(!process_group_exists(group_a));
        assert!(!process_group_exists(group_b));
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

    fn pending_restart_metadata() -> DefaultspackDesktopMetadata {
        DefaultspackDesktopMetadata::test_metadata(test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-active",
            &"b".repeat(64),
        ))
    }

    #[test]
    fn resolution_failures_back_off_and_eventually_abandon_the_restart() {
        let manager = test_manager();
        let error = anyhow!("authority unavailable");
        {
            let mut state = manager.lock_state().unwrap();
            state.restart_in_progress = true;
            state.launch_metadata = Some(pending_restart_metadata());
        }

        manager
            .defer_restart_for_resolution_failure(&error)
            .unwrap();

        {
            let state = manager.lock_state().unwrap();
            assert!(!state.restart_in_progress);
            assert_eq!(state.consecutive_resolve_failures, 1);
            assert!(state.next_restart_at.is_some());
            assert!(state.launch_metadata.is_some());
        }

        {
            let mut state = manager.lock_state().unwrap();
            state.consecutive_resolve_failures = DEFAULTSPACK_MAX_RESOLUTION_FAILURES - 1;
            state.restart_in_progress = true;
        }
        manager
            .defer_restart_for_resolution_failure(&error)
            .unwrap();

        let state = manager.lock_state().unwrap();
        assert_eq!(
            state.consecutive_resolve_failures,
            DEFAULTSPACK_MAX_RESOLUTION_FAILURES
        );
        assert!(state.launch_metadata.is_none());
        assert!(state.next_restart_at.is_none());
    }

    #[test]
    fn monitor_once_defers_an_unresolvable_restart_with_backoff() {
        let manager = test_manager();
        {
            let mut state = manager.lock_state().unwrap();
            state.launch_metadata = Some(pending_restart_metadata());
        }

        // The test fixture has no signed catalog, so resolution fails and the
        // monitor must defer with a recorded failure instead of spinning.
        manager.monitor_once().unwrap();

        let state = manager.lock_state().unwrap();
        assert!(!state.restart_in_progress);
        assert_eq!(state.consecutive_resolve_failures, 1);
        assert!(state.next_restart_at.is_some());
        assert!(state.launch_metadata.is_some());
    }

    #[test]
    fn monitor_once_abandons_the_restart_after_bounded_resolution_failures() {
        let manager = test_manager();
        {
            let mut state = manager.lock_state().unwrap();
            state.launch_metadata = Some(pending_restart_metadata());
            state.consecutive_resolve_failures = DEFAULTSPACK_MAX_RESOLUTION_FAILURES - 1;
        }

        manager.monitor_once().unwrap();

        let state = manager.lock_state().unwrap();
        assert!(!state.restart_in_progress);
        assert!(state.launch_metadata.is_none());
        assert!(state.next_restart_at.is_none());
    }

    #[test]
    fn spawn_and_track_re_verifies_authority_before_writing_the_contract() {
        let manager = test_manager();
        let metadata = pending_restart_metadata();
        {
            let mut state = manager.lock_state().unwrap();
            state.restart_in_progress = true;
            state.launch_metadata = Some(metadata.clone());
        }

        let error = manager
            .spawn_and_track(metadata, "test launch")
            .unwrap_err();

        assert!(format!("{error:#}").contains("could not re-verify"));
        let state = manager.lock_state().unwrap();
        assert!(!state.restart_in_progress);
        assert_eq!(state.consecutive_resolve_failures, 1);
        assert!(state.next_restart_at.is_some());
    }

    #[test]
    fn clear_restart_in_progress_releases_without_a_penalty() {
        let manager = test_manager();
        {
            let mut state = manager.lock_state().unwrap();
            state.restart_in_progress = true;
        }

        manager.clear_restart_in_progress().unwrap();

        let state = manager.lock_state().unwrap();
        assert!(!state.restart_in_progress);
        assert!(state.next_restart_at.is_none());
        assert_eq!(state.consecutive_failures, 0);
    }
}
