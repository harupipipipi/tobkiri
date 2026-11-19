//! Lifecycle supervision for one Launcher-owned application process.
//!
//! The launcher owns only processes it starts itself. The historical
//! Defaultspack adapter remains at the composition boundary, while the
//! lifecycle state is fenced by the complete Profile execution identity.

// std::fs is only referenced by the Linux /proc inspection helpers and
// tests; gate the import so other targets do not see an unused import.
#[cfg(any(test, target_os = "linux"))]
use std::fs;
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
use crate::dock_registration::{
    spawn_defaultspack_local_server, DefaultspackDesktopMetadata, LaunchMetadataUse,
};
use crate::host_broker::BrokerAttestationIdentity;
use crate::process_utils;
use crate::shutdown_budget;
#[cfg(unix)]
use crate::shutdown_budget::{wait_for_graceful_exit, GracefulWait, ProcessObservation};

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
#[cfg(unix)]
// A loaded macOS runner has taken longer than 750 ms to report every group
// member gone after SIGKILL. Keep observation inside the outer watchdog
// without treating signal delivery as termination.
const DEFAULTSPACK_FORCE_KILL_TIMEOUT: Duration = shutdown_budget::FORCE_KILL_OBSERVATION;
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

/// The exact listener observed before the authenticated runtime identity
/// probe. The caller keeps this private evidence until the Shell receipt.
#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ProviderPrepareListener {
    proof: crate::native_harness_bootstrap::VerifiedRegistryLaunch,
    process: crate::shell_handoff::LauncherProcessIdentity,
    application: ApplicationInstanceKey,
    port: u16,
    generation: Option<u64>,
    runtime_instance: Option<String>,
}

#[cfg(all(target_os = "macos", feature = "native-first-setup"))]
impl ProviderPrepareListener {
    pub(crate) fn verified_source(
        &self,
    ) -> &crate::native_harness_bootstrap::VerifiedRegistryLaunch {
        &self.proof
    }

    pub(crate) fn matches_process(
        &self,
        process: &crate::shell_handoff::LauncherProcessIdentity,
    ) -> bool {
        &self.process == process
    }

    pub(crate) fn matches_runtime_instance(&self, instance: &str) -> bool {
        self.runtime_instance.as_deref() == Some(instance)
    }

    pub(crate) fn bind_runtime_instance(mut self, instance: String) -> Self {
        self.runtime_instance = Some(instance);
        self
    }

    pub(crate) fn observe(
        proof: crate::native_harness_bootstrap::VerifiedRegistryLaunch,
        metadata: &DefaultspackDesktopMetadata,
        pid: u32,
    ) -> Option<Self> {
        if !proof.identity.matches(metadata.execution_identity())
            || proof.catalog_revision != metadata.catalog_revision()
        {
            return None;
        }
        let process = crate::shell_handoff::process_identity(pid).ok()?;
        Some(Self {
            proof,
            process,
            application: application_instance_key(metadata),
            port: metadata.port(),
            generation: None,
            runtime_instance: None,
        })
    }

    pub(crate) fn owns_socket(&self) -> bool {
        self.process_is_current()
            && crate::kernel_manager::detect_port_listener_pid(self.port)
                .is_ok_and(|pid| pid == Some(self.process.pid))
            && self.process_is_current()
    }

    fn process_is_current(&self) -> bool {
        crate::shell_handoff::process_identity(self.process.pid)
            .is_ok_and(|observed| observed == self.process)
    }
}

/// Tracks one Application child started by this Launcher instance.
pub(crate) struct ApplicationProcessManager {
    config: AppConfig,
    shutdown_requested: Arc<AtomicBool>,
    broker_attestation: BrokerAttestationIdentity,
    debug_approval: Arc<DebugApprovalManager>,
    state: Mutex<ApplicationProcessState>,
}

/// A process group this Launcher created, recorded with the kernel
/// start-time token of its leader pid. The marker is re-verified before
/// any group signal so a pid recycled by a foreign process cannot
/// redirect the kill onto that process's group.
#[derive(Clone)]
struct OwnedProcessGroup {
    pgid: u32,
    leader_marker: Option<u64>,
}

impl OwnedProcessGroup {
    fn record(pgid: u32) -> Self {
        Self {
            pgid,
            leader_marker: crate::process_utils::process_start_marker(pgid),
        }
    }

    #[cfg(unix)]
    fn leader_is_current(&self) -> bool {
        let Ok(pid) = i32::try_from(self.pgid) else {
            return false;
        };
        // A verified PID can still belong to a process in somebody else's
        // group. Only a leader may authorize a signal to its numeric PGID.
        if unsafe { libc::getpgid(pid) } != pid {
            return false;
        }
        matches!(
            (self.leader_marker, crate::process_utils::process_start_marker(self.pgid)),
            (Some(recorded), Some(current)) if recorded == current
        )
    }
}

#[derive(Default)]
struct ApplicationProcessState {
    child: Option<crate::python_env::PythonChild>,
    /// Process groups created by this Launcher. Keep the ids even after the
    /// direct pack-shell child exits because its Python descendant may still
    /// be serving 8766 as an orphan.
    owned_process_groups: Vec<OwnedProcessGroup>,
    /// Port of the adopted guardian listener, kept separately from
    /// launch_metadata so abandoning a pending restart cannot lose the
    /// pid+port pair needed to reap it on stop().
    adopted_listener_port: Option<u16>,
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
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    provider_prepare: Option<Arc<ProviderPrepareListener>>,
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    provider_listener_generation: u64,
}

/// Compatibility alias for the existing Launcher composition root.
pub(crate) type DefaultspackManager = ApplicationProcessManager;
type MetadataReader<'a> = dyn Fn(LaunchMetadataUse) -> Result<DefaultspackDesktopMetadata> + 'a;

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

    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn capture_provider_listener(
        &self,
        mut candidate: ProviderPrepareListener,
    ) -> Result<Option<ProviderPrepareListener>> {
        let state = self.lock_state()?;
        candidate.generation = Some(state.provider_listener_generation);
        Ok((!self.shutdown_requested.load(Ordering::Acquire)
            && state.matches_provider_listener(&candidate)
            && self.has_provider_guardian(&state, &candidate))
        .then_some(candidate))
    }

    /// Publish evidence only for the listener still owned after a foreground
    /// Shell handoff. Keeping one slot avoids carrying eligibility into retries.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn admit_provider_prepare(
        &self,
        listener: Option<ProviderPrepareListener>,
    ) -> Result<()> {
        let mut state = self.lock_state()?;
        state.provider_prepare = listener
            .filter(|candidate| {
                !self.shutdown_requested.load(Ordering::Acquire)
                    && state.matches_provider_listener(candidate)
                    && candidate.process_is_current()
                    && self.has_provider_guardian(&state, candidate)
            })
            .map(Arc::new);
        Ok(())
    }

    /// Recheck the exact admitted listener against live ownership before
    /// borrowing its verification evidence for a separately authorized request.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn admitted_provider_prepare(
        &self,
        identity: &crate::host_contract::ExecutionProfileIdentity,
    ) -> Result<Option<Arc<ProviderPrepareListener>>> {
        let candidate = {
            let state = self.lock_state()?;
            state
                .provider_prepare
                .as_ref()
                .filter(|candidate| {
                    candidate.proof.identity.matches(identity)
                        && state.matches_provider_listener(candidate)
                })
                .cloned()
        };
        let Some(candidate) = candidate.filter(|candidate| candidate.owns_socket()) else {
            return Ok(None);
        };
        let state = self.lock_state()?;
        Ok((self.provider_prepare_still_admitted(&state, &candidate)
            && self.has_provider_guardian(&state, &candidate))
        .then_some(candidate))
    }

    /// Compare the original registered slot while working on an admitted
    /// request. A new registration must never revive a previous request.
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn require_provider_prepare_admission(
        &self,
        candidate: &Arc<ProviderPrepareListener>,
    ) -> Result<()> {
        let state = self.lock_state()?;
        anyhow::ensure!(
            self.provider_prepare_still_admitted(&state, candidate),
            "the admitted Registry listener changed"
        );
        Ok(())
    }

    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    pub(crate) fn require_provider_prepare_peer(
        &self,
        identity: &crate::host_contract::ExecutionProfileIdentity,
        peer: &crate::shell_handoff::LauncherProcessIdentity,
    ) -> Result<()> {
        let state = self.lock_state()?;
        anyhow::ensure!(
            !self.shutdown_requested.load(Ordering::Acquire)
                && state.provider_prepare.as_ref().is_some_and(|candidate| {
                    candidate.proof.identity.matches(identity)
                        && candidate.matches_process(peer)
                        && state.matches_provider_slot(candidate)
                        && candidate.process_is_current()
                }),
            "Registry operation peer does not own the admitted listener"
        );
        Ok(())
    }

    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    fn provider_prepare_still_admitted(
        &self,
        state: &ApplicationProcessState,
        candidate: &Arc<ProviderPrepareListener>,
    ) -> bool {
        !self.shutdown_requested.load(Ordering::Acquire)
            && state.matches_provider_slot(candidate)
            && candidate.process_is_current()
    }

    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    fn has_provider_guardian(
        &self,
        state: &ApplicationProcessState,
        candidate: &ProviderPrepareListener,
    ) -> bool {
        self.debug_approval
            .current_guardian()
            .is_ok_and(|guardian| {
                let identity = &candidate.proof.identity;
                state.active_run_id.as_deref() == Some(guardian.run_id.as_str())
                    && guardian.http_port == candidate.port
                    && guardian.profile_id == identity.profile_id
                    && guardian.profile_revision == identity.profile_revision
                    && guardian.activation_id == identity.activation_id
                    && guardian.plan_digest == identity.plan_digest
            })
    }

    /// Start Defaultspack when this Launcher does not already own a live child.
    ///
    /// A restart already in progress is reused instead of spawning a duplicate.
    pub(crate) fn start_or_reuse(
        &self,
        metadata: DefaultspackDesktopMetadata,
        read_metadata: &MetadataReader<'_>,
    ) -> Result<()> {
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
                        state.retire_provider_prepare();
                        state.active_guardian_pid = None;
                        state.launch_metadata = None;
                        state.owned_process_groups.retain(|entry| {
                            replaced_child
                                .as_ref()
                                .map_or(true, |child| child.id() != entry.pgid)
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
                    state.retire_provider_prepare();
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

            state.retire_provider_prepare();
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
            self.spawn_and_track(metadata, "initial launch", read_metadata)?;
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

    /// Stop for startup/recovery with the existing short graceful-stop bound.
    pub(crate) fn stop(&self) -> Result<()> {
        self.stop_with_grace_period(shutdown_budget::DEFAULTSPACK_RECOVERY_GRACE)
    }

    /// Stop for Launcher exit, allowing bounded HTTP and VM capture cleanup.
    pub(crate) fn stop_for_shutdown(&self) -> Result<()> {
        self.stop_with_grace_period(shutdown_budget::DEFAULTSPACK_GRACE)
    }

    fn stop_with_grace_period(&self, grace: Duration) -> Result<()> {
        let (child, owned_process_groups, active_run_id, adopted_listener) = {
            let mut state = self.lock_state()?;
            state.stop_requested = true;
            state.retire_provider_prepare();
            let adopted_listener = state.active_guardian_pid.take().and_then(|pid| {
                state
                    .adopted_listener_port
                    .take()
                    .or_else(|| state.launch_metadata.as_ref().map(|m| m.port()))
                    .map(|port| (pid, port))
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

        // An adopted listener and owned groups verify identity independently.
        // Stop them concurrently so their grace windows do not accumulate.
        // An adopted listener is never evidence of process-group ownership.
        #[cfg(unix)]
        let group_stop_result = thread::scope(|scope| {
            let adopted_stop = adopted_listener
                .filter(|(pid, _)| Some(*pid) != child_pid)
                .map(|(pid, port)| {
                    scope.spawn(move || {
                        if let Err(error) = stop_adopted_defaultspack_listener(pid, port, grace) {
                            warn!("Failed to stop adopted Defaultspack listener {pid}: {error:#}");
                        }
                    })
                });
            let result = stop_owned_unix_process_groups(child, owned_process_groups, grace);
            if let Some(stop) = adopted_stop {
                if stop.join().is_err() {
                    warn!("Adopted Defaultspack listener stop worker panicked");
                }
            }
            result
        });

        #[cfg(not(unix))]
        let group_stop_result = {
            let _ = grace;
            let _ = owned_process_groups;
            match child {
                Some(mut child) => {
                    info!("Stopping managed Defaultspack (pid {})", child.id());
                    stop_child(&mut child)
                }
                None => Ok(()),
            }
        };

        // A guardian adopted outside the managed child is not an owned group.
        #[cfg(not(unix))]
        if let Some((pid, port)) = adopted_listener {
            if Some(pid) != child_pid {
                if let Err(error) = stop_adopted_defaultspack_listener(pid, port, grace) {
                    warn!("Failed to stop adopted Defaultspack listener {pid}: {error:#}");
                }
            }
        }

        group_stop_result?;

        if !had_live_child {
            info!("No live managed Defaultspack child remained during stop");
        }
        info!("Managed Defaultspack process groups stopped");
        Ok(())
    }

    /// Start the background monitor. It exits when Launcher shutdown begins.
    pub(crate) fn spawn_exit_monitor(manager: Arc<Self>, app: tauri::AppHandle) {
        thread::spawn(move || loop {
            if manager.shutdown_requested.load(Ordering::SeqCst) {
                break;
            }

            let deadline = Instant::now() + crate::preparation_budget::TIMEOUT;
            if let Err(error) = manager.monitor_once(&|purpose| {
                crate::dock_registration::read_launch_metadata(
                    &app,
                    &manager.config,
                    deadline,
                    purpose,
                )
            }) {
                error!("Defaultspack lifecycle monitor failed: {error:#}");
            }
            thread::sleep(DEFAULTSPACK_MONITOR_INTERVAL);
        });
    }

    fn monitor_once(&self, read_metadata: &MetadataReader<'_>) -> Result<()> {
        self.monitor_once_with(
            read_metadata,
            || self.pending_restart_port_gate(),
            |pid, port| {
                stop_adopted_defaultspack_listener(
                    pid,
                    port,
                    shutdown_budget::DEFAULTSPACK_RECOVERY_GRACE,
                )
            },
        )
    }

    fn monitor_once_with(
        &self,
        read_metadata: &MetadataReader<'_>,
        port_gate: impl FnOnce() -> Result<PendingRestartPort>,
        retire_listener: impl FnOnce(u32, u16) -> Result<()>,
    ) -> Result<()> {
        let pending_key = {
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

            let Some(metadata) = state.launch_metadata.as_ref() else {
                return Ok(());
            };
            let pending_key = application_instance_key(metadata);
            state.restart_in_progress = true;
            pending_key
        };

        // A respawn can only claim the port when it is free. Defer while an
        // adopted Launcher-owned listener still serves it, and bound the
        // churn while an unowned process squats on it. An inspection failure
        // takes the standard backoff so `restart_in_progress` never wedges.
        let port_gate = match port_gate() {
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
                let retire_port = {
                    let mut state = self.lock_state()?;
                    state.restart_in_progress = false;
                    if state.stop_requested || state.child.is_some() {
                        return Ok(());
                    }
                    if state.active_guardian_pid == Some(pid) && state.active_run_id.is_some() {
                        // A registered adopted listener is a supervised
                        // endpoint, not an obstacle: a respawn against it can
                        // only fail with EADDRINUSE, so the monitor keeps
                        // deferring until it exits on its own.
                        info!(
                            "Defaultspack restart deferred: adopted listener pid {pid} still owns the port"
                        );
                        state.next_restart_at =
                            Some(Instant::now() + DEFAULTSPACK_ADOPTED_LISTENER_RECHECK_INTERVAL);
                        return Ok(());
                    }
                    // The wrapper that owned this guardian run exited and the
                    // exit path took the registration with it, so the
                    // surviving adopted listener is an unguarded orphan that
                    // would defer every respawn forever. Retire it through
                    // the pid+port-verified path so the restart proceeds
                    // under full supervision.
                    state
                        .adopted_listener_port
                        .or_else(|| state.launch_metadata.as_ref().map(|m| m.port()))
                };
                let Some(port) = retire_port else {
                    return Ok(());
                };
                match retire_listener(pid, port) {
                    Ok(()) => {
                        info!(
                            "Retired unguarded adopted Defaultspack listener {pid}; restart can proceed"
                        );
                        let mut state = self.lock_state()?;
                        if state.active_guardian_pid == Some(pid) {
                            state.active_guardian_pid = None;
                            state.adopted_listener_port = None;
                        }
                        state.next_restart_at = Some(Instant::now());
                    }
                    Err(error) => {
                        let delay = self.record_spawn_failure()?;
                        warn!(
                            "Failed to retire unguarded adopted Defaultspack listener {pid}; retry is scheduled after {} ms: {error:#}",
                            delay.as_millis()
                        );
                    }
                }
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
        let metadata = match read_metadata(LaunchMetadataUse::Supervision) {
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
        match read_metadata(LaunchMetadataUse::Supervision) {
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
            if state.stop_requested
                || self.shutdown_requested.load(Ordering::SeqCst)
                || state.child.is_some()
            {
                state.restart_in_progress = false;
                return Ok(());
            }
            if state.launch_metadata.as_ref().is_some_and(|current| {
                !application_instance_key(current).matches(&pending_key)
                    && !application_metadata_matches(current, &metadata)
            }) {
                // A foreground launch adopted another Profile while the
                // monitor was resolving. Leave it for the next restart pass.
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

        if let Err(error) = self.spawn_and_track(metadata, "automatic restart", read_metadata) {
            error!("Failed to restart Defaultspack: {error:#}");
        }
        Ok(())
    }

    fn spawn_and_track(
        &self,
        metadata: DefaultspackDesktopMetadata,
        reason: &str,
        read_metadata: &MetadataReader<'_>,
    ) -> Result<()> {
        let run_id = managed_defaultspack_run_id();
        // The authority can rotate between the caller's resolution and this
        // spawn. Re-verify the resolved identity immediately before the Host
        // contract is written and use the fresh Registry evidence for startup.
        let metadata = match read_metadata(LaunchMetadataUse::Spawn) {
            Ok(current) if application_metadata_matches(&current, &metadata) => current,
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
        };
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
        if let Err(error) = self.drain_child_output(&mut child, pid) {
            // A child whose output cannot reach its bounded log must not run
            // without durable diagnostics: stop it like any other failed
            // launch and let the retry take the spawn backoff.
            if let Err(stop_error) = stop_child(&mut child) {
                warn!(
                    "Failed to stop Defaultspack child {pid} after its output log could not be opened: {stop_error:#}"
                );
            }
            let delay = self.record_spawn_failure()?;
            return Err(error).with_context(|| {
                format!(
                    "Defaultspack {reason} could not open defaultspack.log; retry is scheduled after {} ms",
                    delay.as_millis()
                )
            });
        }

        // The authority may also have rotated while the child was being
        // created; confirm the spawned identity once more before it is
        // registered as the guardian and allowed to own `state.child`.
        let spawned_identity_is_current = match read_metadata(LaunchMetadataUse::Supervision) {
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
                return Err(error).context(format!(
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
                if !state
                    .owned_process_groups
                    .iter()
                    .any(|entry| entry.pgid == pid)
                {
                    state
                        .owned_process_groups
                        .push(OwnedProcessGroup::record(pid));
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
        // Keep the same manager -> debug approval lock order used by spawn.
        // Shutdown or a replacement must not interleave with this transfer.
        let mut state = self.lock_state()?;
        anyhow::ensure!(
            !self.shutdown_requested.load(Ordering::Acquire),
            "Defaultspack listener registration was requested during shutdown"
        );
        if state.active_guardian_pid == Some(process_id)
            && state.active_run_id.is_some()
            && state
                .launch_metadata
                .as_ref()
                .is_some_and(|current| application_metadata_matches(current, metadata))
        {
            return Ok(());
        }
        let old_run_id = state.active_run_id.take();
        state.retire_provider_prepare();
        let run_id = old_run_id
            .clone()
            .unwrap_or_else(managed_defaultspack_run_id);
        state.active_guardian_pid = None;
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
        state.active_run_id = Some(run_id);
        state.remember_adopted_listener(process_id, metadata.port());
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
        #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
        if error.is::<crate::defaultspack_authority::ActiveLaunchIdentityChanged>() {
            return self.clear_restart_in_progress();
        }
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
        let (guardian_pid, metadata) = {
            let state = self.lock_state()?;
            if state.child.is_some() {
                return Ok(PendingRestartPort::Free);
            }
            let Some(metadata) = state.launch_metadata.clone() else {
                return Ok(PendingRestartPort::Free);
            };
            (state.active_guardian_pid, metadata)
        };
        let port = metadata.port();
        match crate::kernel_manager::detect_port_listener(port)? {
            None => Ok(PendingRestartPort::Free),
            // The recorded guardian pid may have been recycled by a foreign
            // squatter; only a listener that still identifies as the
            // Defaultspack defers the restart — anything else churns through
            // the bounded occupied-port path instead of deferring forever.
            Some(listener)
                if Some(listener.pid) == guardian_pid
                    && crate::dock_registration::identify_defaultspack_listener(
                        &listener, &metadata,
                    ) =>
            {
                Ok(PendingRestartPort::AdoptedListener(listener.pid))
            }
            Some(listener) => Ok(PendingRestartPort::Occupied {
                pid: listener.pid,
                port,
            }),
        }
    }

    /// Attach bounded, redacting drain threads to the child's piped output.
    ///
    /// `defaultspack.log` stays strictly bounded per launch — the previous
    /// generation rotates to `defaultspack.log.prev` — instead of growing
    /// without limit, and secret-shaped values are redacted before they
    /// reach durable storage. Failing to open the log aborts the launch
    /// (fail-closed) instead of silently dropping diagnostics; the pipes are
    /// always drained to EOF so the child can never wedge on output.
    fn drain_child_output(
        &self,
        child: &mut crate::python_env::PythonChild,
        pid: u32,
    ) -> Result<()> {
        let log = crate::process_utils::open_child_log(
            &self.config.log_dir.join("defaultspack.log"),
            crate::process_utils::CHILD_LOG_MAX_BYTES,
        )
        .context("failed to create defaultspack.log")?;
        if let Some(stdout) = child.stdout.take() {
            crate::process_utils::spawn_bounded_log_drain(
                stdout,
                Some(Arc::clone(&log)),
                pid,
                "stdout",
                "Defaultspack",
                true,
            );
        }
        if let Some(stderr) = child.stderr.take() {
            crate::process_utils::spawn_bounded_log_drain(
                stderr,
                Some(log),
                pid,
                "stderr",
                "Defaultspack",
                true,
            );
        }
        Ok(())
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
    owned_process_groups: Vec<OwnedProcessGroup>,
    grace: Duration,
) -> Result<()> {
    let child_group = child.as_ref().map(crate::python_env::PythonChild::id);
    let mut groups = owned_process_groups
        .into_iter()
        .filter(|entry| Some(entry.pgid) != child_group)
        .collect::<Vec<_>>();
    groups.sort_unstable_by_key(|entry| entry.pgid);
    groups.dedup_by_key(|entry| entry.pgid);

    // A wrapper can exit while its process group remains alive, and multiple
    // previously-owned groups can therefore be retained for shutdown. Their
    // grace windows are independent. Stop them concurrently so the product's
    // fixed quit budget is not multiplied by the number of owned groups.
    thread::scope(|scope| -> Result<()> {
        let child_stop = child.as_mut().map(|child| {
            info!("Stopping managed Defaultspack (pid {})", child.id());
            scope.spawn(move || stop_unix_process_group(child, "Defaultspack", grace))
        });
        let group_stops = groups
            .into_iter()
            .map(|process_group| {
                scope.spawn(move || stop_unix_process_group_id(process_group, grace))
            })
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
fn stop_adopted_defaultspack_listener(pid: u32, port: u16, grace: Duration) -> Result<()> {
    match crate::kernel_manager::detect_port_listener(port)? {
        Some(listener) if listener.pid == pid => {
            crate::kernel_manager::terminate_external_listener_with_grace(pid, port, grace)
        }
        _ => Ok(()),
    }
}

/// Whether `port` is still served by a listener running under `pid` — the
/// pid+port identity proof shared with the reclaim path. An inspection
/// failure counts as unverified rather than as ours.
#[cfg(unix)]
fn adopted_listener_owns_port(pid: u32, port: u16) -> bool {
    crate::kernel_manager::detect_port_listener(port)
        .ok()
        .flatten()
        .is_some_and(|listener| listener.pid == pid)
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
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    fn matches_provider_slot(&self, candidate: &Arc<ProviderPrepareListener>) -> bool {
        self.provider_prepare
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, candidate))
            && self.matches_provider_listener(candidate)
    }

    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    fn matches_provider_listener(&self, candidate: &ProviderPrepareListener) -> bool {
        !self.stop_requested
            && !self.restart_in_progress
            && self.provider_listener_generation != u64::MAX
            && candidate.generation == Some(self.provider_listener_generation)
            && self.active_run_id.is_some()
            && self.active_guardian_pid == Some(candidate.process.pid)
            && self
                .adopted_listener_port
                .is_none_or(|port| port == candidate.port)
            && self.launch_metadata.as_ref().is_some_and(|metadata| {
                candidate
                    .application
                    .matches(&application_instance_key(metadata))
                    && candidate.proof.catalog_revision == metadata.catalog_revision()
                    && candidate.port == metadata.port()
            })
    }

    fn retire_provider_prepare(&mut self) {
        #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
        {
            self.provider_prepare = None;
            self.provider_listener_generation = self.provider_listener_generation.saturating_add(1);
        }
    }

    fn remember_adopted_listener(&mut self, pid: u32, port: u16) {
        self.active_guardian_pid = Some(pid);
        self.adopted_listener_port = Some(port);
        // The authenticated listener can share the managed child's group or
        // lead an unrelated group. Neither fact grants ownership of its PGID.
    }

    fn record_unexpected_exit(&mut self, _status: ExitStatus) -> Duration {
        self.retire_provider_prepare();
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

fn stop_child(child: &mut crate::python_env::PythonChild) -> Result<()> {
    // Startup/replacement failures retain their short recovery bound. Normal
    // Quit supplies its larger capture-cleanup grace through the owned-group path.
    #[cfg(unix)]
    return stop_unix_process_group(
        child,
        "Defaultspack",
        shutdown_budget::DEFAULTSPACK_RECOVERY_GRACE,
    );

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
    grace: Duration,
) -> Result<()> {
    let pid = child.id();
    let owned_group = OwnedProcessGroup {
        pgid: pid,
        leader_marker: child.start_marker(),
    };

    // The pack-shell wrapper can exit before the desktop app it spawned. Wait
    // for the entire group, not only the direct child, so its listener cannot
    // survive Launcher shutdown. A reaped leader no longer proves ownership;
    // its numeric PGID may have been assigned to an unrelated process.
    let group_leader = owned_group.leader_is_current() && process_group_exists(pid);
    if group_leader {
        let _ = send_process_group_signal(pid, "-TERM");
    } else if child
        .try_wait()
        .with_context(|| format!("failed to inspect {label} process before stopping"))?
        .is_none()
    {
        let _ = send_unix_process_signal(pid, "-TERM");
    }

    let wait = wait_for_graceful_exit(grace, || -> Result<ProcessObservation> {
        let exited = if group_leader {
            if !owned_group.leader_is_current() {
                warn!("Skipping {label} process group {pid}: its leader identity was lost after SIGTERM");
                return Ok(ProcessObservation::OwnershipLost);
            }
            !process_group_exists(pid)
        } else {
            child
                .try_wait()
                .with_context(|| format!("failed to wait for {label} after SIGTERM"))?
                .is_some()
        };
        Ok(if exited {
            ProcessObservation::Exited
        } else {
            ProcessObservation::Running
        })
    })?;
    match wait {
        GracefulWait::Exited | GracefulWait::OwnershipLost => {
            let _ = child.try_wait();
            return Ok(());
        }
        GracefulWait::TimedOut => {}
    }

    let sent_kill = if group_leader {
        if !owned_group.leader_is_current() {
            warn!(
                "Skipping {label} process group {pid}: its leader identity was lost before SIGKILL"
            );
            let _ = child.try_wait();
            return Ok(());
        }
        send_process_group_signal(pid, "-KILL")
    } else {
        child
            .try_wait()
            .with_context(|| format!("failed to inspect {label} before SIGKILL"))?
            .is_some()
            || send_unix_process_signal(pid, "-KILL")
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
    if group_leader
        && owned_group.leader_is_current()
        && !wait_for_process_group_exit(pid, DEFAULTSPACK_FORCE_KILL_TIMEOUT)
    {
        return Err(anyhow!(
            "{label} process group {pid} remained live after SIGKILL"
        ));
    }
    Ok(())
}

#[cfg(unix)]
fn stop_unix_process_group_id(group: OwnedProcessGroup, grace: Duration) -> Result<()> {
    let process_group = group.pgid;
    if !process_group_exists(process_group) {
        return Ok(());
    }

    // A numeric PGID alone is never ownership evidence. Once the leader has
    // exited, its former group can disappear and the number can be reused by
    // an unrelated process. A matching port or ESRCH for the old leader does
    // not prove that the current group is the one this Launcher recorded.
    if !group.leader_is_current() {
        warn!(
            "Skipping owned Defaultspack process group {process_group}: its leader identity could not be re-verified"
        );
        return Ok(());
    }

    let _ = send_process_group_signal(process_group, "-TERM");
    let wait = wait_for_graceful_exit(grace, || -> Result<ProcessObservation> {
        if !group.leader_is_current() {
            warn!("Skipping owned Defaultspack process group {process_group}: its leader identity was lost after SIGTERM");
            return Ok(ProcessObservation::OwnershipLost);
        }
        Ok(if process_group_exists(process_group) {
            ProcessObservation::Running
        } else {
            ProcessObservation::Exited
        })
    })?;
    if wait != GracefulWait::TimedOut {
        return Ok(());
    }

    if !group.leader_is_current() {
        warn!("Skipping owned Defaultspack process group {process_group}: its leader identity was lost before SIGKILL");
        return Ok(());
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
            host_broker_contract: None,
            #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
            native_provider_prepare_endpoint: None,
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

    fn read_test_metadata(manager: &DefaultspackManager) -> Result<DefaultspackDesktopMetadata> {
        crate::dock_registration::read_defaultspack_desktop_metadata(&manager.config)
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

        manager
            .start_or_reuse(requested.clone(), &|_| {
                panic!("restart already in progress")
            })
            .unwrap();

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

        manager
            .start_or_reuse(requested.clone(), &|_| {
                panic!("restart already in progress")
            })
            .unwrap();

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
            .start_or_reuse(
                DefaultspackDesktopMetadata::test_metadata(identity.clone()),
                &|_| panic!("restart already in progress"),
            )
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

    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    #[test]
    fn registry_evidence_binds_provider_to_the_current_listener_and_generation() {
        let identity = test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-test",
            &"b".repeat(64),
        );
        let metadata = DefaultspackDesktopMetadata::test_metadata(identity.clone());
        let proof = crate::native_harness_bootstrap::VerifiedRegistryLaunch::test_proof(
            identity,
            metadata.catalog_revision(),
        );
        let mut candidate = ProviderPrepareListener::observe(proof, &metadata, std::process::id())
            .expect("fixture must use the actual test process");
        assert!(candidate.verified_source().provider_prepare.is_some());
        assert!(candidate.verified_source().model_settings.is_none());
        let mut state = DefaultspackState {
            active_guardian_pid: Some(candidate.process.pid),
            active_run_id: Some("fixture".into()),
            launch_metadata: Some(metadata),
            ..DefaultspackState::default()
        };
        assert!(!state.matches_provider_listener(&candidate));
        candidate.generation = Some(state.provider_listener_generation);
        assert!(state.matches_provider_listener(&candidate));

        state.adopted_listener_port = Some(candidate.port + 1);
        assert!(!state.matches_provider_listener(&candidate));
        state.adopted_listener_port = Some(candidate.port);
        state.stop_requested = true;
        assert!(!state.matches_provider_listener(&candidate));
        state.stop_requested = false;
        let slot = Arc::new(candidate.clone());
        state.provider_prepare = Some(slot.clone());
        assert!(state.matches_provider_slot(&slot));
        state.provider_prepare = Some(Arc::new(candidate.clone()));
        assert!(!state.matches_provider_slot(&slot));
        state.retire_provider_prepare();
        assert!(state.provider_prepare.is_none());
        assert!(!state.matches_provider_listener(&candidate));

        // Even the same PID, port, run ID and Profile need a fresh observation.
        candidate.generation = Some(state.provider_listener_generation);
        assert!(state.matches_provider_listener(&candidate));
        candidate.application.artifact_digest = Some("sha256:other".into());
        assert!(!state.matches_provider_listener(&candidate));
    }

    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    #[test]
    fn registry_evidence_replaces_model_slot_when_the_epoch_changes() {
        let identity = test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-test",
            &"b".repeat(64),
        );
        let metadata = DefaultspackDesktopMetadata::test_metadata(identity.clone());
        let mut proof = crate::native_harness_bootstrap::VerifiedRegistryLaunch::test_model_proof(
            identity,
            metadata.catalog_revision(),
        );
        let mut state = DefaultspackState {
            active_guardian_pid: Some(std::process::id()),
            active_run_id: Some("fixture".into()),
            launch_metadata: Some(metadata.clone()),
            ..DefaultspackState::default()
        };
        let observe = |proof, generation| {
            let mut candidate =
                ProviderPrepareListener::observe(proof, &metadata, std::process::id())
                    .expect("fixture must use the actual test process");
            candidate.generation = Some(generation);
            Arc::new(candidate)
        };
        let old = observe(proof.clone(), state.provider_listener_generation);
        state.provider_prepare = Some(Arc::clone(&old));
        assert!(state.matches_provider_slot(&old));
        assert!(old.verified_source().provider_prepare.is_none());
        assert!(old.verified_source().model_settings.is_some());

        proof.security_epoch += 1;
        let replacement = observe(proof, state.provider_listener_generation);
        state.provider_prepare = Some(Arc::clone(&replacement));
        assert!(!state.matches_provider_slot(&old));
        assert!(state.matches_provider_slot(&replacement));
        assert_eq!(
            replacement.verified_source().security_epoch,
            old.verified_source().security_epoch + 1
        );
        assert!(replacement.verified_source().provider_prepare.is_none());
        assert!(replacement.verified_source().model_settings.is_some());
        state.retire_provider_prepare();
        assert!(!state.matches_provider_slot(&replacement));
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
            "sleep 30 & child=$!; printf '%s' \"$child\" > {}; wait",
            pid_file.display()
        );
        let mut command = process_utils::command(SYSTEM_SHELL);
        command.args(["-c", &script]);
        crate::dock_registration::configure_defaultspack_process_group(&mut command);
        let child = command.spawn().unwrap();
        {
            let mut state = manager.lock_state().unwrap();
            state
                .owned_process_groups
                .push(OwnedProcessGroup::record(child.id()));
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
    fn direct_stop_does_not_signal_group_after_its_leader_was_reaped() {
        let pid_file = std::env::temp_dir().join(format!(
            "defaultspack-direct-stop-orphan-{}-{}.pid",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let script = format!(
            "sleep 30 & child=$!; printf '%s' \"$child\" > {}; sleep 0.2",
            pid_file.display()
        );
        let mut command = process_utils::command(SYSTEM_SHELL);
        command.args(["-c", &script]);
        crate::dock_registration::configure_defaultspack_process_group(&mut command);
        let mut child = crate::python_env::PythonChild::development(command.spawn().unwrap());
        assert!(
            child.start_marker().is_some(),
            "spawned leader marker was unavailable"
        );
        child.wait().unwrap();
        let descendant_pid = fs::read_to_string(&pid_file)
            .unwrap()
            .trim()
            .parse::<u32>()
            .unwrap();
        assert!(process_id_is_live(descendant_pid));

        stop_unix_process_group(
            &mut child,
            "Defaultspack",
            shutdown_budget::DEFAULTSPACK_GRACE,
        )
        .unwrap();
        let descendant_alive = process_id_is_live(descendant_pid);
        let _ = send_unix_process_signal(descendant_pid, "-KILL");
        fs::remove_file(pid_file).ok();
        assert!(
            descendant_alive,
            "reaped leader's former group was signalled"
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_child_that_does_not_lead_its_group_is_stopped_by_pid() {
        let mut command = process_utils::command(SYSTEM_SHELL);
        command.args(["-c", "sleep 30"]);
        let mut child = crate::python_env::PythonChild::development(command.spawn().unwrap());
        let pid = child.id();
        let group = OwnedProcessGroup {
            pgid: pid,
            leader_marker: child.start_marker(),
        };
        assert!(!group.leader_is_current());
        stop_unix_process_group(
            &mut child,
            "Defaultspack",
            shutdown_budget::DEFAULTSPACK_GRACE,
        )
        .unwrap();
        assert!(child.try_wait().unwrap().is_some());
    }

    #[cfg(unix)]
    #[test]
    fn launcher_shutdown_allows_cleanup_after_the_recovery_grace() {
        use std::io::{BufRead, BufReader, Read};

        let manager = test_manager();
        let mut command = process_utils::command(SYSTEM_SHELL);
        command.args([
            "-c",
            "trap 'sleep 2.2; printf cleaned; exit 0' TERM; printf 'ready\\n'; while :; do :; done",
        ]);
        command.stdout(std::process::Stdio::piped());
        crate::dock_registration::configure_defaultspack_process_group(&mut command);
        let mut child = command.spawn().unwrap();
        let mut output = BufReader::new(child.stdout.take().unwrap());
        let mut ready = String::new();
        output.read_line(&mut ready).unwrap();
        assert_eq!(ready, "ready\n");
        {
            let mut state = manager.lock_state().unwrap();
            state
                .owned_process_groups
                .push(OwnedProcessGroup::record(child.id()));
            state.child = Some(crate::python_env::PythonChild::development(child));
        }

        let started = Instant::now();
        manager.stop_for_shutdown().unwrap();
        assert!(started.elapsed() >= shutdown_budget::DEFAULTSPACK_RECOVERY_GRACE);
        assert!(started.elapsed() < Duration::from_secs(10));
        let mut completion = String::new();
        output.read_to_string(&mut completion).unwrap();
        assert_eq!(completion, "cleaned");
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
            state
                .owned_process_groups
                .push(OwnedProcessGroup::record(process_group));
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
            state.owned_process_groups.extend([
                OwnedProcessGroup::record(group_a),
                OwnedProcessGroup::record(group_b),
            ]);
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
    fn adopted_listener_and_stale_group_identity_do_not_signal_reused_id() {
        let manager = test_manager();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();

        // This live group stands in for a foreign group that acquired the
        // numeric PID/PGID of an earlier, now-dead adopted guardian. It does
        // not own the listener port, so quit must leave it running.
        let mut command = process_utils::command(SYSTEM_SHELL);
        command.args(["-c", "exec sleep 30"]);
        crate::dock_registration::configure_defaultspack_process_group(&mut command);
        let mut foreign_group = command.spawn().unwrap();
        let reused_id = foreign_group.id();
        assert_eq!(unsafe { libc::getpgid(reused_id as i32) }, reused_id as i32);
        let foreign_marker = crate::process_utils::process_start_marker(reused_id).unwrap();
        {
            let mut state = manager.lock_state().unwrap();
            state.remember_adopted_listener(reused_id, port);
            assert!(state.owned_process_groups.is_empty());
            // An older managed group once had this numeric PGID. Its saved
            // leader marker must not authorize a signal to the new group.
            state.owned_process_groups.push(OwnedProcessGroup {
                pgid: reused_id,
                leader_marker: Some(foreign_marker.wrapping_add(1)),
            });
        }

        manager.stop().unwrap();
        let survived = foreign_group.try_wait().unwrap().is_none();
        let _ = foreign_group.kill();
        let _ = foreign_group.wait();
        assert!(survived, "quit signalled a foreign process group");
    }

    #[cfg(unix)]
    #[test]
    fn explicit_stop_does_not_signal_group_after_leader_identity_is_lost() {
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
            state
                .owned_process_groups
                .push(OwnedProcessGroup::record(process_group));
        }

        manager.stop().unwrap();

        let descendant_alive = process_id_is_live(descendant_pid);
        // The test knows this exact descendant pid; production has no such
        // evidence after the leader exits and must leave its PGID alone.
        let _ = send_unix_process_signal(descendant_pid, "-KILL");
        fs::remove_file(pid_file).ok();
        assert!(
            descendant_alive,
            "unverified process group {process_group} was signalled"
        );
    }

    fn pending_restart_metadata() -> DefaultspackDesktopMetadata {
        let reservation = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = reservation.local_addr().unwrap().port();
        drop(reservation);
        DefaultspackDesktopMetadata::test_metadata_at_port(
            test_execution_identity(
                "profile-a",
                &"a".repeat(64),
                "profile-a-active",
                &"b".repeat(64),
            ),
            port,
        )
    }

    #[test]
    fn monitor_once_retires_listener_after_wrapper_exit_removes_guardian() {
        let manager = test_manager();
        let listener =
            std::cell::RefCell::new(Some(std::net::TcpListener::bind("127.0.0.1:0").unwrap()));
        let port = listener
            .borrow()
            .as_ref()
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let listener_pid = std::process::id();
        let run_id = "wrapper-exit-run-12345678";
        let token_file = std::env::temp_dir().join(format!(
            "tobkiri-wrapper-exit-token-{}-{}",
            listener_pid,
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::write(&token_file, "test-token").unwrap();
        let metadata = crate::dock_registration::DefaultspackDesktopMetadata::test_metadata_at_port(
            test_execution_identity(
                "profile-a",
                &"a".repeat(64),
                "profile-a-active",
                &"b".repeat(64),
            ),
            port,
        );
        manager
            .debug_approval
            .register_guardian(
                run_id.into(),
                listener_pid,
                "test-defaultspack".into(),
                std::env::temp_dir(),
                port,
                token_file.clone(),
                metadata.execution_identity().clone(),
            )
            .unwrap();
        assert!(manager.debug_approval.current_guardian().is_ok());

        // A separate test-harness process stands in for the pack-shell
        // wrapper. The TCP listener remains open in this process after it
        // exits, as the desktop application's child listener can in practice.
        let mut wrapper = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "__defaultspack_wrapper_fixture_no_test__"])
            .spawn()
            .unwrap();
        assert!(wrapper.wait().unwrap().success());
        {
            let mut state = manager.lock_state().unwrap();
            state.child = Some(crate::python_env::PythonChild::development(wrapper));
            state.launch_metadata = Some(metadata);
            state.active_run_id = Some(run_id.into());
            state.remember_adopted_listener(listener_pid, port);
        }

        manager
            .monitor_once(&|_| read_test_metadata(&manager))
            .unwrap();
        assert!(listener.borrow().is_some());
        assert!(manager.debug_approval.current_guardian().is_err());
        {
            let mut state = manager.lock_state().unwrap();
            assert!(state.child.is_none());
            assert!(state.active_run_id.is_none());
            assert_eq!(state.active_guardian_pid, Some(listener_pid));
            state.next_restart_at = Some(Instant::now());
        }

        let retired = std::cell::Cell::new(false);
        manager
            .monitor_once_with(
                &|_| panic!("an occupied port must defer authority resolution"),
                || {
                    assert!(listener.borrow().is_some());
                    Ok(PendingRestartPort::AdoptedListener(listener_pid))
                },
                |pid, observed_port| {
                    assert_eq!((pid, observed_port), (listener_pid, port));
                    listener.borrow_mut().take();
                    retired.set(true);
                    Ok(())
                },
            )
            .unwrap();
        assert!(retired.get());
        {
            let state = manager.lock_state().unwrap();
            assert!(!state.restart_in_progress);
            assert!(state.active_guardian_pid.is_none());
            assert!(state.adopted_listener_port.is_none());
            assert!(state
                .next_restart_at
                .is_some_and(|deadline| deadline <= Instant::now()));
        }

        // The next pass proceeds to authority resolution instead of being
        // deferred by the same orphan forever. This fixture has no signed
        // catalog, so resolution must enter its bounded failure path.
        manager
            .monitor_once(&|_| read_test_metadata(&manager))
            .unwrap();
        let state = manager.lock_state().unwrap();
        assert_eq!(state.consecutive_resolve_failures, 1);
        assert!(state.next_restart_at.is_some());
        drop(state);
        fs::remove_file(token_file).unwrap();
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
        manager
            .monitor_once(&|_| read_test_metadata(&manager))
            .unwrap();

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

        manager
            .monitor_once(&|_| read_test_metadata(&manager))
            .unwrap();

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
            .spawn_and_track(metadata, "test launch", &|purpose| {
                assert!(matches!(purpose, LaunchMetadataUse::Spawn));
                read_test_metadata(&manager)
            })
            .unwrap_err();

        assert!(format!("{error:#}").contains("could not re-verify"));
        let state = manager.lock_state().unwrap();
        assert!(!state.restart_in_progress);
        assert_eq!(state.consecutive_resolve_failures, 1);
        assert!(state.next_restart_at.is_some());
    }

    #[test]
    fn restart_resolution_does_not_replace_metadata_adopted_by_a_concurrent_launch() {
        let manager = test_manager();
        let pending = pending_restart_metadata();
        let adopted = DefaultspackDesktopMetadata::test_metadata(test_execution_identity(
            "profile-a",
            &"a".repeat(64),
            "profile-a-rotated",
            &"b".repeat(64),
        ));
        manager.lock_state().unwrap().launch_metadata = Some(pending.clone());
        let reads = std::cell::Cell::new(0);
        manager
            .monitor_once_with(
                &|_| {
                    reads.set(reads.get() + 1);
                    if reads.get() == 2 {
                        manager.start_or_reuse(adopted.clone(), &|_| {
                            panic!("foreground launch must reuse the in-flight restart")
                        })?;
                    }
                    Ok(pending.clone())
                },
                || Ok(PendingRestartPort::Free),
                |_, _| panic!("free port has no listener to retire"),
            )
            .unwrap();
        assert_eq!(reads.get(), 2);
        let state = manager.lock_state().unwrap();
        assert!(!state.restart_in_progress);
        assert!(state.child.is_none());
        assert_eq!(state.consecutive_resolve_failures, 0);
        assert_eq!(state.launch_metadata.as_ref(), Some(&adopted));
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

    fn test_manager_with_log_dir(log_dir: PathBuf) -> DefaultspackManager {
        let mut config = test_config();
        config.log_dir = log_dir;
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

    fn unique_test_dir(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "defaultspack-manager-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    #[cfg(unix)]
    fn spawn_shell_child(script: &str) -> crate::python_env::PythonChild {
        let mut command = process_utils::command(SYSTEM_SHELL);
        command
            .args(["-c", script])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());
        crate::python_env::PythonChild::development(command.spawn().unwrap())
    }

    #[cfg(unix)]
    #[test]
    fn drain_child_output_persists_bounded_redacted_output_with_rotation() {
        let dir = unique_test_dir("drain");
        let manager = test_manager_with_log_dir(dir.clone());
        let log_path = dir.join("defaultspack.log");
        let prev_path = dir.join("defaultspack.log.prev");
        fs::create_dir_all(&dir).unwrap();
        fs::write(&log_path, "prior generation").unwrap();
        fs::write(&prev_path, "stale generation").unwrap();

        let mut child = spawn_shell_child(
            "printf 'token=tok123 plain-stdout\n'; printf 'password=hunter2 plain-stderr\n' >&2",
        );
        let pid = child.id();

        manager.drain_child_output(&mut child, pid).unwrap();

        let contents = (0..80)
            .find_map(|_| {
                let contents = fs::read_to_string(&log_path).unwrap_or_default();
                if contents.contains("plain-stdout") && contents.contains("plain-stderr") {
                    Some(contents)
                } else {
                    thread::sleep(Duration::from_millis(25));
                    None
                }
            })
            .expect("drain did not persist child output");

        assert!(contents.contains("[stdout pid="));
        assert!(contents.contains("[stderr pid="));
        // Secret-shaped values are redacted before reaching durable storage.
        assert!(
            !contents.contains("tok123"),
            "leaked stdout token: {contents}"
        );
        assert!(
            !contents.contains("hunter2"),
            "leaked stderr password: {contents}"
        );
        assert!(contents.contains("[REDACTED]"));
        // The previous generation rotated to .prev, and the stale .prev was
        // dropped so at most two bounded generations persist.
        assert_eq!(fs::read_to_string(&prev_path).unwrap(), "prior generation");
        fs::remove_dir_all(&dir).ok();
    }

    #[cfg(unix)]
    #[test]
    fn drain_child_output_stays_bounded_when_child_exceeds_the_cap() {
        let dir = unique_test_dir("drain-cap");
        let manager = test_manager_with_log_dir(dir.clone());
        let log_path = dir.join("defaultspack.log");

        // Emit well over the 8MiB persistence cap plus a marker at the end.
        let mut child =
            spawn_shell_child("head -c 9000000 /dev/zero | tr '\\0' 'x'; printf 'tail-marker'");
        let pid = child.id();

        manager.drain_child_output(&mut child, pid).unwrap();

        let contents = (0..200)
            .find_map(|_| {
                let contents = fs::read_to_string(&log_path).unwrap_or_default();
                if contents.contains("output truncated") {
                    Some(contents)
                } else {
                    thread::sleep(Duration::from_millis(25));
                    None
                }
            })
            .expect("drain did not mark a truncated log");

        // The pipe is still fully drained so the child can exit, but the
        // persisted bytes stop at the shared cap plus one truncation marker.
        let file_size = fs::metadata(&log_path).unwrap().len();
        assert!(
            file_size <= crate::process_utils::CHILD_LOG_MAX_BYTES + 128,
            "defaultspack.log persisted {file_size} bytes past its cap"
        );
        assert_eq!(contents.matches("output truncated").count(), 1);
        fs::remove_dir_all(&dir).ok();
    }

    #[cfg(unix)]
    #[test]
    fn drain_child_output_fails_closed_when_the_log_cannot_be_opened() {
        let root = unique_test_dir("drain-fail");
        fs::create_dir_all(&root).unwrap();
        // A regular file where the log directory must be created makes the
        // open fail, exercising the fail-closed boundary.
        let blocker = root.join("blocker");
        fs::write(&blocker, "not a directory").unwrap();
        let manager = test_manager_with_log_dir(blocker.join("logs"));

        let mut child = spawn_shell_child("exit 0");

        let error = manager.drain_child_output(&mut child, 0).unwrap_err();

        assert!(
            format!("{error:#}").contains("defaultspack.log"),
            "unexpected error: {error:#}"
        );
        fs::remove_dir_all(&root).ok();
    }
}
