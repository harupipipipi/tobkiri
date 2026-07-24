//! Lifecycle supervision for the local Defaultspack process.
//!
//! The launcher owns only processes it starts itself. An already-running,
//! authenticated Defaultspack listener is deliberately reused by
//! `dock_registration` and is never adopted or terminated here.

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::process::{Child, ExitStatus};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Context, Result};
use log::{error, info, warn};

use crate::config::AppConfig;
use crate::dock_registration::{spawn_defaultspack_local_server, DefaultspackDesktopMetadata};
use crate::process_utils;

const DEFAULTSPACK_MONITOR_INTERVAL: Duration = Duration::from_millis(250);
const DEFAULTSPACK_RESTART_INITIAL_BACKOFF: Duration = Duration::from_millis(250);
const DEFAULTSPACK_RESTART_MAX_BACKOFF: Duration = Duration::from_secs(5);
const DEFAULTSPACK_STABLE_RUN_WINDOW: Duration = Duration::from_secs(30);
const DEFAULTSPACK_STOP_TIMEOUT: Duration = Duration::from_secs(5);

/// Tracks the Defaultspack child started by this Launcher instance.
pub(crate) struct DefaultspackManager {
    config: AppConfig,
    shutdown_requested: Arc<AtomicBool>,
    state: Mutex<DefaultspackState>,
}

struct DefaultspackState {
    child: Option<Child>,
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
}

impl Default for DefaultspackState {
    fn default() -> Self {
        Self {
            child: None,
            owned_process_groups: Vec::new(),
            launch_metadata: None,
            restart_in_progress: false,
            stop_requested: false,
            consecutive_failures: 0,
            next_restart_at: None,
            started_at: None,
        }
    }
}

impl DefaultspackManager {
    pub(crate) fn new(config: AppConfig, shutdown_requested: Arc<AtomicBool>) -> Self {
        Self {
            config,
            shutdown_requested,
            state: Mutex::new(DefaultspackState::default()),
        }
    }

    /// Start Defaultspack when this Launcher does not already own a live child.
    ///
    /// A restart already in progress is reused instead of spawning a duplicate.
    pub(crate) fn start_or_reuse(&self, metadata: DefaultspackDesktopMetadata) -> Result<()> {
        let should_spawn = {
            let mut state = self.lock_state()?;
            if self.shutdown_requested.load(Ordering::SeqCst) {
                return Err(anyhow!("Defaultspack launch was requested during shutdown"));
            }

            state.stop_requested = false;
            if let Some(child) = state.child.as_mut() {
                match child
                    .try_wait()
                    .context("failed to inspect managed Defaultspack process")?
                {
                    None => {
                        info!(
                            "Defaultspack already running under Launcher supervision (pid {})",
                            child.id()
                        );
                        return Ok(());
                    }
                    Some(status) => {
                        warn!(
                            "Managed Defaultspack exited before reuse (status {status}); starting a replacement"
                        );
                        state.child = None;
                        state.record_unexpected_exit(status);
                    }
                }
            }

            if state.restart_in_progress {
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
                    state.record_unexpected_exit(status);
                }
            }
        }

        Ok(!state.stop_requested && (state.restart_in_progress || state.launch_metadata.is_some()))
    }

    /// Stop the managed child and disable all automatic restart paths.
    pub(crate) fn stop(&self) -> Result<()> {
        let (child, owned_process_groups) = {
            let mut state = self.lock_state()?;
            state.stop_requested = true;
            state.launch_metadata = None;
            state.next_restart_at = None;
            state.restart_in_progress = false;
            state.consecutive_failures = 0;
            state.started_at = None;
            (
                state.child.take(),
                std::mem::take(&mut state.owned_process_groups),
            )
        };

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
        let mut child = match spawn_defaultspack_local_server(&self.config, &metadata) {
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
                state.child = Some(child);
                state.launch_metadata = Some(metadata);
                state.next_restart_at = None;
                state.started_at = Some(Instant::now());
                info!("Defaultspack {reason} started (pid {pid})");
                return Ok(());
            }
        };

        if should_stop_child {
            info!("Discarding duplicate Defaultspack process (pid {pid})");
            stop_child(&mut child)?;
        }
        Ok(())
    }

    fn record_spawn_failure(&self) -> Result<Duration> {
        let mut state = self.lock_state()?;
        state.restart_in_progress = false;
        Ok(state.record_restart_failure())
    }

    fn drain_child_output(&self, child: &mut Child, pid: u32) {
        let log_path = self.config.log_dir.join("defaultspack.log");
        if let Some(stdout) = child.stdout.take() {
            spawn_output_drain(stdout, log_path.clone(), pid, "stdout");
        }
        if let Some(stderr) = child.stderr.take() {
            spawn_output_drain(stderr, log_path, pid, "stderr");
        }
    }

    fn lock_state(&self) -> Result<std::sync::MutexGuard<'_, DefaultspackState>> {
        self.state
            .lock()
            .map_err(|error| anyhow!("Defaultspack manager lock poisoned: {error}"))
    }
}

impl DefaultspackState {
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

fn stop_child(child: &mut Child) -> Result<()> {
    #[cfg(unix)]
    return stop_unix_process_group(child);

    #[cfg(not(unix))]
    stop_non_unix_child(child)
}

#[cfg(unix)]
fn stop_unix_process_group(child: &mut Child) -> Result<()> {
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
fn stop_non_unix_child(child: &mut Child) -> Result<()> {
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
    let sent = match process_utils::command("kill")
        .args([signal, &process_group])
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
    send_process_group_signal(process_group, "-0")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

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
        let manager = DefaultspackManager::new(test_config(), Arc::new(AtomicBool::new(false)));
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
        let mut command = process_utils::command("sh");
        command.args(["-c", &script]);
        crate::dock_registration::configure_defaultspack_process_group(&mut command);
        let child = command.spawn().unwrap();
        {
            let mut state = manager.lock_state().unwrap();
            state.owned_process_groups.push(child.id());
            state.child = Some(child);
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
        let descendant_alive = process_utils::command("kill")
            .args(["-0", &descendant_pid.to_string()])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .unwrap()
            .success();
        fs::remove_file(pid_file).ok();
        assert!(
            !descendant_alive,
            "Defaultspack descendant {descendant_pid} survived process-group shutdown"
        );
    }

    #[cfg(unix)]
    #[test]
    fn explicit_stop_terminates_an_orphaned_owned_process_group() {
        let manager = DefaultspackManager::new(test_config(), Arc::new(AtomicBool::new(false)));
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
        let mut command = process_utils::command("sh");
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

        let descendant_alive = process_utils::command("kill")
            .args(["-0", &descendant_pid.to_string()])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .unwrap()
            .success();
        fs::remove_file(pid_file).ok();
        assert!(
            !descendant_alive,
            "orphaned Defaultspack descendant {descendant_pid} survived shutdown"
        );
    }
}
