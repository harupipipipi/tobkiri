//! Kernel process lifecycle management.
//!
//! Responsibilities:
//! - Start the Python Kernel (`python -m app`) inside the venv.
//! - Stop it gracefully (SIGTERM -> timeout -> SIGKILL on Unix, kill on Windows).
//! - Detect exit-code 42 to signal "please restart me".
//! - Auto-restart on unexpected exit (max 3 times).

use std::fs;
#[cfg(unix)]
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use log::{error, info, warn};

use crate::config::AppConfig;
use crate::process_utils;

/// Special exit code: the Kernel requests a restart.
const RESTART_EXIT_CODE: i32 = 42;

/// Maximum consecutive non-42 restarts before giving up.
const MAX_AUTO_RESTARTS: u32 = 3;

/// Maximum consecutive `start()` failures while a restart is still owed
/// before the exit monitor abandons the supervised restart.
pub(crate) const MAX_KERNEL_RESTART_START_ATTEMPTS: u32 = 5;

/// Initial delay between failed restart `start()` attempts.
const KERNEL_RESTART_START_INITIAL_BACKOFF: Duration = Duration::from_millis(500);

/// Cap for the delay between failed restart `start()` attempts.
const KERNEL_RESTART_START_MAX_BACKOFF: Duration = Duration::from_secs(30);

/// Bounded exponential backoff for a failed restart `start()` attempt.
pub(crate) fn kernel_restart_start_backoff(consecutive_failures: u32) -> Duration {
    let exponent = consecutive_failures.saturating_sub(1).min(6);
    let multiplier = 1_u32 << exponent;
    KERNEL_RESTART_START_INITIAL_BACKOFF
        .checked_mul(multiplier)
        .unwrap_or(KERNEL_RESTART_START_MAX_BACKOFF)
        .min(KERNEL_RESTART_START_MAX_BACKOFF)
}

/// Seconds to wait after SIGTERM before sending SIGKILL.
const KILL_TIMEOUT_SECS: u64 = 5;

fn python_runtime_env_vars() -> [(&'static str, &'static str); 4] {
    [
        ("PYTHONUTF8", "1"),
        ("PYTHONIOENCODING", "utf-8"),
        ("PYTHONUNBUFFERED", "1"),
        ("PYTHONDONTWRITEBYTECODE", "1"),
    ]
}

fn kernel_working_dir(config: &AppConfig) -> &Path {
    if config.is_dev_workspace() {
        &config.rumi_home
    } else {
        config
            .user_data_dir
            .parent()
            .unwrap_or(&config.user_data_dir)
    }
}

fn require_development_venv(config: &AppConfig) -> Result<()> {
    if !config.is_dev_workspace() {
        return Ok(());
    }
    let venv_python = config.venv_python();
    if !venv_python.exists() {
        bail!(
            "venv Python not found at {} -- run environment setup first",
            venv_python.display()
        );
    }
    Ok(())
}

/// Resolve the durable active Application authority, if one has been
/// completely committed.  A Host-contract file is never used as the source
/// of this decision: it is only a projection of the independently verified
/// Profile authority.
fn verified_active_application_authority(
    config: &AppConfig,
) -> Result<Option<crate::defaultspack_authority::ApplicationAuthority>> {
    if !crate::defaultspack_authority::has_verified_active_profile(config)
        .context("failed to inspect durable active Profile authority")?
    {
        return Ok(None);
    }
    match crate::defaultspack_authority::resolve(config) {
        Ok(authority) => Ok(Some(authority)),
        Err(error) if requires_setup_reconfirmation(&error) => {
            // The Python Host must verify and reconfirm the successor before
            // publishing any active execution identity or contributions.
            Ok(None)
        }
        Err(error) => Err(error).context("failed to resolve durable active Application authority"),
    }
}

fn requires_setup_reconfirmation(error: &anyhow::Error) -> bool {
    error
        .downcast_ref::<crate::defaultspack_authority::ShellReconfirmationRequired>()
        .is_some()
        || error
            .downcast_ref::<crate::defaultspack_authority::ProfileReresolutionRequired>()
            .is_some()
}

/// Publish contributions only from the independently verified active Application.
pub(crate) fn write_kernel_host_contract(
    config: &AppConfig,
    bootstrap_secret: &str,
) -> Result<PathBuf> {
    match verified_active_application_authority(config)? {
        Some(authority) => {
            let identity = authority
                .execution_identity()
                .context("durable active Application execution identity is invalid")?;
            let contributions =
                crate::host_contract_contributions::collect_for_verified_application(&authority)
                    .context("failed to collect verified active Host contract contributions")?;
            crate::host_contract::write_contract(
                config,
                &identity,
                [
                    ("panel_bootstrap_secret", bootstrap_secret.to_owned()),
                    (
                        "system_pack_descriptors",
                        contributions.system_pack_descriptors,
                    ),
                    (
                        "update_target_descriptors",
                        contributions.update_target_descriptors,
                    ),
                ],
            )
            .context("failed to publish the durable active Host contract")
        }
        None => crate::host_contract::write_bootstrap_contract(
            config,
            [("panel_bootstrap_secret", bootstrap_secret.to_owned())],
        )
        .context("failed to publish the bootstrap-only Host contract"),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PortListener {
    pub(crate) pid: u32,
    pub(crate) command: String,
    pub(crate) cwd: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ListenerIdentity {
    WorkingDirectory,
    EntrypointPath,
    VenvPython,
    SealedEnvironment,
}

impl ListenerIdentity {
    fn description(self) -> &'static str {
        match self {
            Self::WorkingDirectory => "matched the configured RUMI_HOME working directory",
            Self::EntrypointPath => "matched the configured Kernel entrypoint path",
            Self::VenvPython => "matched the configured venv Python path",
            Self::SealedEnvironment => "matched the sealed Python environment root",
        }
    }
}

impl PortListener {
    pub(crate) fn summary(&self) -> String {
        match self.cwd.as_deref() {
            Some(cwd) if !cwd.is_empty() => format!("{} (cwd={cwd})", self.command),
            _ => self.command.clone(),
        }
    }
}

/// Manages a single Kernel child process.
pub struct KernelManager {
    child: Option<crate::python_env::PythonChild>,
    config: AppConfig,
    panel_bootstrap_secret: String,
    /// Stores the exit code from the most recent child exit.
    last_exit_code: Option<i32>,
    /// Counter for consecutive non-42 restarts.
    restart_count: u32,
    /// Set when a supervised exit requested a restart that has not yet
    /// produced a running Kernel. It survives transient `start()` failures so
    /// the exit monitor cannot lose restart eligibility by consuming the
    /// exit status before the Kernel is running again.
    restart_owed: bool,
    /// Consecutive `start()` failures while `restart_owed` was set.
    restart_start_failures: u32,
    /// Monotonically increasing successful-start generation used to fence
    /// background work from a Kernel process that has since restarted.
    launch_generation: u64,
}

impl KernelManager {
    pub fn new(config: &AppConfig, panel_bootstrap_secret: String) -> Self {
        Self {
            child: None,
            config: config.clone(),
            panel_bootstrap_secret,
            last_exit_code: None,
            restart_count: 0,
            restart_owed: false,
            restart_start_failures: 0,
            launch_generation: 0,
        }
    }

    /// Start the Kernel process.
    ///
    /// Runs the sealed runtime module with isolated Python. Bundled builds use
    /// the writable app data root as cwd and an explicit verified resource
    /// root, preventing ambient imports and writes inside the app bundle.
    /// Stdout and stderr are redirected to `{log_dir}/kernel.log`.
    pub fn start(&mut self) -> Result<()> {
        if self.is_running() {
            info!("Kernel already running, skipping start");
            self.restart_owed = false;
            self.restart_start_failures = 0;
            return Ok(());
        }

        if let Some(message) = self.recover_port_conflict()? {
            warn!("{message}");
        }

        require_development_venv(&self.config)?;
        if !self.config.rumi_home.exists() {
            bail!(
                "Kernel directory not found: {}",
                self.config.rumi_home.display()
            );
        }
        // Packaged outer-runtime and sealed-environment verification is
        // intentionally centralized in `spawn_packaged_role`. It binds the
        // full outer manifest to the sealed snapshot immediately before
        // execution; hashing `app_dir` here would repeat that work without
        // improving the fail-closed launch boundary.

        // Kernel output is piped through bounded, redacting drain threads so
        // kernel.log stays strictly bounded per launch (previous launch
        // rotates to kernel.log.prev) instead of growing without limit, and
        // secret-shaped values cannot be persisted verbatim. Failing to open
        // the log aborts the launch (fail-closed), matching the previous
        // File::create behavior.
        let kernel_log_path = self.config.log_dir.join("kernel.log");
        let kernel_log = crate::process_utils::open_child_log(
            &kernel_log_path,
            crate::process_utils::CHILD_LOG_MAX_BYTES,
        )
        .context("failed to create kernel.log")?;

        let working_dir = kernel_working_dir(&self.config);
        fs::create_dir_all(working_dir)?;
        info!(
            "Starting Kernel from {} (cwd={})",
            if self.config.is_dev_workspace() {
                self.config.venv_python().display().to_string()
            } else {
                "build-bound sealed Python snapshot".to_string()
            },
            working_dir.display()
        );

        let dev_environment = cfg!(debug_assertions) || self.config.is_dev_workspace();
        let host_contract_path =
            write_kernel_host_contract(&self.config, &self.panel_bootstrap_secret)?;
        let next_launch_generation = self.next_launch_generation()?;

        let kernel_log_for_spawn = std::sync::Arc::clone(&kernel_log);
        let child = crate::python_env::spawn_python_role(
            &self.config,
            crate::python_env::PythonRole::Kernel,
            crate::python_env::RoleArguments::default(),
            |command| {
                // The bounded drain attaches at spawn time inside
                // `spawn_python_role`, so bootstrap output is consumed before
                // the sealed-environment attestation wait instead of wedging
                // a >pipe-buffer emitter behind it.
                command.attach_output_log(kernel_log_for_spawn, "Kernel");
                if self.config.is_dev_workspace() {
                    command.env("RUMI_APP_DIR", &self.config.app_dir);
                }
                command
                    .current_dir(working_dir)
                    .env_remove("PYTHONPATH")
                    .env("RUMI_HOME", &self.config.rumi_home)
                    .env("RUMI_USER_DATA", &self.config.user_data_dir)
                    .env(
                        "RUMI_DEFAULTSPACK_SECRETS_DIR",
                        self.config.user_data_dir.join("secrets"),
                    )
                    .env(
                        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
                        self.config
                            .user_data_dir
                            .join("defaultspack")
                            .join("shared")
                            .join("frontend_settings.json"),
                    )
                    .env(
                        "RUMI_DEFAULTSPACK_COMMAND_STATE_DIR",
                        self.config
                            .user_data_dir
                            .join("defaultspack")
                            .join("shared"),
                    )
                    .env("RUMI_LOG_DIR", &self.config.log_dir)
                    .env("RUMI_PORT", self.config.kernel_port.to_string())
                    .env(crate::host_contract::CONTRACT_ENV, &host_contract_path)
                    .env(
                        "RUMI_VIEWER_HOST_BROKER_CONNECTION",
                        self.config.host_broker_connection_path(),
                    )
                    .env("RUMI_MACOS_PERMISSION_HOST", "tobkiri_launcher")
                    .envs(python_runtime_env_vars())
                    .env(
                        "RUMI_ENVIRONMENT",
                        if dev_environment {
                            "development"
                        } else {
                            "production"
                        },
                    )
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped());
                // Own process group so shutdown can terminate the Kernel and
                // any runtime descendants together (mirrors Defaultspack).
                #[cfg(unix)]
                command.new_process_group();
                Ok(())
            },
        )
        .context("failed to verify and spawn Kernel process")?;

        info!("Kernel started (pid {})", child.id());
        self.child = Some(child);
        self.launch_generation = next_launch_generation;
        self.last_exit_code = None;
        self.restart_owed = false;
        self.restart_start_failures = 0;
        Ok(())
    }

    /// Return whether a supervised exit requested a restart that has not yet
    /// produced a running Kernel.
    pub(crate) fn restart_owed(&self) -> bool {
        self.restart_owed
    }

    /// Record a failed `start()` while a restart is owed and return the
    /// consecutive failure count for the monitor's bounded retry budget.
    pub(crate) fn record_restart_start_failure(&mut self) -> u32 {
        self.restart_start_failures = self.restart_start_failures.saturating_add(1);
        self.restart_start_failures
    }

    /// Abandon a restart whose `start()` attempts exhausted the monitor's
    /// retry budget.
    pub(crate) fn abandon_restart_owed(&mut self) {
        self.restart_owed = false;
        self.restart_start_failures = 0;
    }

    fn next_launch_generation(&self) -> Result<u64> {
        self.launch_generation
            .checked_add(1)
            .context("Kernel launch generation overflow")
    }

    /// Return the start generation captured by asynchronous Launcher work.
    pub(crate) fn launch_generation(&self) -> u64 {
        self.launch_generation
    }

    /// Return whether a captured generation still names this Kernel.  The
    /// zero generation is reserved for an authenticated Kernel that predates
    /// this Launcher process and therefore has no managed child handle.
    pub(crate) fn is_current_launch_generation(&mut self, generation: u64) -> bool {
        self.launch_generation == generation && (generation == 0 || self.is_running())
    }

    pub fn current_pid(&self) -> Option<u32> {
        self.child.as_ref().map(|child| child.id())
    }

    pub fn recover_port_conflict(&mut self) -> Result<Option<String>> {
        let port = self.config.kernel_port;
        let Some(listener) = detect_port_listener(port)? else {
            return Ok(None);
        };

        if Some(listener.pid) == self.current_pid() {
            return Ok(None);
        }

        let Some(identity) = identify_owned_listener(&listener, &self.config) else {
            bail!(
                "port {port} is already in use by pid {} ({})",
                listener.pid,
                listener.summary(),
            );
        };

        warn!(
            "Detected stale Rumi listener on port {port}: pid {} ({}; {})",
            listener.pid,
            listener.summary(),
            identity.description(),
        );

        if self.child.is_some() {
            self.stop().ok();
        }

        terminate_external_listener(listener.pid, port)?;
        self.restart_count = 0;

        Ok(Some(format!(
            "Recovered stale Rumi listener on port {port} from pid {} ({})",
            listener.pid,
            identity.description(),
        )))
    }

    /// Stop the Kernel process.
    pub fn stop(&mut self) -> Result<()> {
        let child = match self.child.as_mut() {
            Some(c) => c,
            None => {
                info!("No Kernel process to stop");
                return Ok(());
            }
        };

        info!("Stopping Kernel (pid {}) ...", child.id());

        #[cfg(unix)]
        {
            Self::unix_stop(child)?;
        }

        #[cfg(not(unix))]
        {
            child.kill().ok();
            child.wait().ok();
        }

        self.child = None;
        info!("Kernel stopped");
        Ok(())
    }

    /// Stop then start. Resets the restart counter.
    pub fn restart(&mut self) -> Result<()> {
        self.stop()?;
        self.restart_count = 0;
        self.start()
    }

    /// Consume the last exit status and decide whether to auto-restart.
    ///
    /// Returns `true` if the caller should call `start()` again. The decision
    /// is retained as `restart_owed` until `start()` produces a running
    /// Kernel, so a transient `start()` failure cannot consume it.
    pub fn wait_and_handle_restart(&mut self) -> Result<bool> {
        if let Some(child) = self.child.as_mut() {
            let status = child.wait().context("failed to wait on Kernel")?;
            let code = status.code().unwrap_or(-1);
            self.last_exit_code = Some(code);
            self.child = None;
        }

        let restart = match self.last_exit_code.take() {
            Some(RESTART_EXIT_CODE) => {
                info!("Kernel exited with code 42 -- restart requested");
                self.restart_count = 0;
                true
            }
            Some(0) => {
                info!("Kernel exited normally (code 0)");
                false
            }
            Some(code) => {
                self.restart_count += 1;
                if self.restart_count <= MAX_AUTO_RESTARTS {
                    warn!(
                        "Kernel exited with code {code} -- auto-restart {}/{}",
                        self.restart_count, MAX_AUTO_RESTARTS
                    );
                    true
                } else {
                    error!(
                        "Kernel exited with code {code} -- max restarts ({}) exceeded, giving up",
                        MAX_AUTO_RESTARTS
                    );
                    false
                }
            }
            // No new exit status: keep reporting the still-owed restart so a
            // failed `start()` cannot consume restart eligibility.
            None => self.restart_owed,
        };
        self.restart_owed = restart;
        Ok(restart)
    }

    /// Returns `true` if the child process exists and has not yet exited.
    pub fn is_running(&mut self) -> bool {
        match self.child.as_mut() {
            Some(child) => match child.try_wait() {
                Ok(Some(status)) => {
                    self.last_exit_code = status.code();
                    self.child = None;
                    false
                }
                Ok(None) => true,
                Err(e) => {
                    error!("try_wait error: {e}");
                    false
                }
            },
            None => false,
        }
    }

    #[cfg(unix)]
    fn unix_stop(child: &mut crate::python_env::PythonChild) -> Result<()> {
        // The Kernel leads its own process group so runtime descendants exit
        // with it; children spawned without a group are still reached by the
        // routine's direct-pid fallback. Shares the zombie-aware group stop
        // used for Defaultspack.
        crate::defaultspack_manager::stop_unix_process_group(child, "Kernel")
    }
}

pub(crate) fn detect_port_listener(port: u16) -> Result<Option<PortListener>> {
    #[cfg(unix)]
    {
        detect_port_listener_unix(port)
    }

    #[cfg(windows)]
    {
        detect_port_listener_windows(port)
    }

    #[cfg(not(any(unix, windows)))]
    {
        let _ = port;
        Ok(None)
    }
}

#[cfg(unix)]
fn detect_port_listener_unix(port: u16) -> Result<Option<PortListener>> {
    let mut command = process_utils::command("lsof");
    command.args(["-nP", &format!("-iTCP:{port}"), "-sTCP:LISTEN", "-Fpc"]);
    let output = match process_utils::bounded_output(
        &mut command,
        process_utils::INSPECTION_COMMAND_TIMEOUT,
    ) {
        Ok(output) => output,
        Err(error) if error.kind() == ErrorKind::NotFound => {
            warn!("`lsof` is not available; port-conflict recovery is disabled");
            return Ok(None);
        }
        Err(error) => return Err(error).context("failed to run lsof"),
    };

    if !output.status.success() {
        if output.status.code() == Some(1) {
            return Ok(None);
        }
        let stderr = String::from_utf8_lossy(&output.stderr);
        bail!("lsof exited with {}: {}", output.status, stderr.trim());
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut pid = None;
    let mut short_name = None;

    for line in stdout.lines() {
        if let Some(rest) = line.strip_prefix('p') {
            pid = rest.trim().parse::<u32>().ok();
        } else if let Some(rest) = line.strip_prefix('c') {
            short_name = Some(rest.trim().to_string());
        }
    }

    let Some(pid) = pid else {
        return Ok(None);
    };

    let command = unix_process_command(pid)
        .or(short_name)
        .unwrap_or_else(|| "unknown".to_string());
    let cwd = unix_process_cwd(pid);

    Ok(Some(PortListener { pid, command, cwd }))
}

#[cfg(windows)]
fn detect_port_listener_windows(port: u16) -> Result<Option<PortListener>> {
    let mut command = process_utils::command("netstat");
    command.args(["-ano", "-p", "tcp"]);
    let output =
        process_utils::bounded_output(&mut command, process_utils::INSPECTION_COMMAND_TIMEOUT)
            .context("failed to run netstat")?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        bail!("netstat exited with {}: {}", output.status, stderr.trim());
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    for line in stdout.lines() {
        let columns: Vec<&str> = line.split_whitespace().collect();
        if columns.len() < 5 {
            continue;
        }
        let local = columns[1];
        let state = columns[3];
        let pid = columns[4];
        if state != "LISTENING" {
            continue;
        }
        if !(local.ends_with(&format!(":{port}")) || local.ends_with(&format!("]:{port}"))) {
            continue;
        }
        if let Ok(pid) = pid.parse::<u32>() {
            let command = windows_process_command(pid).unwrap_or_else(|| "unknown".to_string());
            return Ok(Some(PortListener {
                pid,
                command,
                cwd: None,
            }));
        }
    }

    Ok(None)
}

#[cfg(unix)]
fn unix_process_command(pid: u32) -> Option<String> {
    let mut command = process_utils::command("ps");
    command.args(["-p", &pid.to_string(), "-o", "command="]);
    let output =
        process_utils::bounded_output(&mut command, process_utils::INSPECTION_COMMAND_TIMEOUT)
            .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!text.is_empty()).then_some(text)
}

#[cfg(unix)]
fn unix_process_cwd(pid: u32) -> Option<String> {
    let mut command = process_utils::command("lsof");
    command.args(["-a", "-p", &pid.to_string(), "-d", "cwd", "-Fn"]);
    let output =
        process_utils::bounded_output(&mut command, process_utils::INSPECTION_COMMAND_TIMEOUT)
            .ok()?;
    if !output.status.success() {
        return None;
    }

    for line in String::from_utf8_lossy(&output.stdout).lines() {
        if let Some(rest) = line.strip_prefix('n') {
            let cwd = rest.trim().to_string();
            if !cwd.is_empty() {
                return Some(cwd);
            }
        }
    }

    None
}

#[cfg(windows)]
fn windows_process_command(pid: u32) -> Option<String> {
    let mut command = process_utils::command("tasklist");
    command.args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"]);
    let output =
        process_utils::bounded_output(&mut command, process_utils::INSPECTION_COMMAND_TIMEOUT)
            .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!text.is_empty() && !text.starts_with("INFO:")).then_some(text)
}

fn identify_owned_listener(
    listener: &PortListener,
    config: &AppConfig,
) -> Option<ListenerIdentity> {
    if !is_python_app_command(&listener.command) {
        return None;
    }

    if listener
        .cwd
        .as_deref()
        .is_some_and(|cwd| observed_path_matches(cwd, &config.rumi_home))
    {
        return Some(ListenerIdentity::WorkingDirectory);
    }

    let entrypoint = config.rumi_home.join("app.py");
    if command_mentions_path(&listener.command, &entrypoint)
        || command_mentions_path(&listener.command, &config.rumi_home)
    {
        return Some(ListenerIdentity::EntrypointPath);
    }

    if command_mentions_path(&listener.command, &config.venv_python()) {
        return Some(ListenerIdentity::VenvPython);
    }

    if sealed_kernel_listener(&listener) {
        return Some(ListenerIdentity::SealedEnvironment);
    }

    None
}

fn is_python_app_command(command: &str) -> bool {
    let command = normalize_for_match(command);
    command.contains("python")
        && (command.contains("app.py")
            || command.contains("-m app")
            || is_sealed_kernel_command(&command))
}

/// Whether a normalized command line is the sealed Kernel launch
/// (`python3 -I -B -m tobkiri_sealed.bootstrap --role typed ...`). Other
/// sealed roles (defaultspack, host_helper) are deliberately rejected so an
/// unrelated Launcher child is never reaped as a stale Kernel.
fn is_sealed_kernel_command(normalized_command: &str) -> bool {
    crate::sealed_python_protocol::sealed_bootstrap_role(normalized_command)
        == Some(crate::sealed_python_protocol::ROLE_TYPED)
}

/// A sealed Kernel runs from the verified environment root named by its own
/// `--environment-root` argument (on macOS that root is the private
/// `.tobkiri-sealed-python-*` snapshot copy). The snapshot name embeds the
/// launcher pid that created it: that owner must be dead for the listener
/// to count as stale, or a healthy kernel belonging to a concurrently
/// running install would be reclaimed while it still serves.
fn sealed_kernel_listener(listener: &PortListener) -> bool {
    let command = normalize_for_match(&listener.command);
    if !is_sealed_kernel_command(&command) {
        return false;
    }
    let Some(cwd) = listener.cwd.as_deref() else {
        return false;
    };
    let cwd = normalize_for_match(cwd);
    let Some(env_root) = crate::sealed_python_protocol::sealed_environment_root(&command) else {
        return false;
    };
    let env_root = normalize_for_match(env_root);
    let Some(leaf) = env_root.rsplit('/').next() else {
        return false;
    };
    if !crate::sealed_python::is_sealed_snapshot_dir_name(leaf) {
        return false;
    }
    if !sealed_snapshot_owner_dead(leaf) {
        return false;
    }
    cwd == env_root
        || cwd
            .rsplit('/')
            .next()
            .is_some_and(crate::sealed_python::is_sealed_snapshot_dir_name)
}

/// Whether the launcher pid stamped into a snapshot directory name is
/// provably dead. `EPERM` counts as alive — a process that cannot be
/// probed cannot be proven stale. Non-Unix platforms cannot establish
/// owner liveness, so the snapshot evidence is never enough there.
fn sealed_snapshot_owner_dead(snapshot_leaf: &str) -> bool {
    let Some(owner_pid) = crate::sealed_python::sealed_snapshot_owner_pid(snapshot_leaf) else {
        return false;
    };
    #[cfg(unix)]
    {
        let alive = unsafe { libc::kill(owner_pid, 0) } == 0
            || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM);
        !alive
    }
    #[cfg(not(unix))]
    {
        let _ = owner_pid;
        false
    }
}

fn command_mentions_path(command: &str, path: &Path) -> bool {
    let command = normalize_for_match(command);
    let path = normalize_path_for_match(path);
    if path.is_empty() {
        return false;
    }
    // The path must end at a segment boundary or the end of the argument —
    // a plain substring also matches `…/rumi_runtime_extra/…` or
    // `…/app.py.bak`, which would misidentify a foreign process as ours.
    command.match_indices(&path).any(|(start, _)| {
        let head_ok = start == 0 || command[..start].ends_with(['/', ' ', '"', '\'']);
        let tail = &command[start + path.len()..];
        head_ok && (tail.is_empty() || tail.starts_with(['/', ' ', '"', '\'']))
    })
}

fn observed_path_matches(observed: &str, expected: &Path) -> bool {
    normalize_for_match(observed) == normalize_path_for_match(expected)
}

fn normalize_path_for_match(path: &Path) -> String {
    normalize_for_match(&path.to_string_lossy())
}

fn normalize_for_match(value: &str) -> String {
    let normalized = value.trim().replace('\\', "/");
    let normalized = normalized.trim_end_matches('/').to_string();

    #[cfg(windows)]
    {
        normalized.to_ascii_lowercase()
    }

    #[cfg(not(windows))]
    {
        normalized
    }
}

/// Signal the listener's whole process group when it leads one so sealed
/// descendants cannot keep the port; signal the process directly otherwise.
/// The group-existence check keeps the common non-leader case quiet instead
/// of logging a failed group signal first.
#[cfg(unix)]
fn signal_listener_process_tree(pid: u32, signal: &str) {
    if crate::defaultspack_manager::process_group_exists(pid) {
        let _ = crate::defaultspack_manager::send_process_group_signal(pid, signal);
    } else {
        let _ = crate::defaultspack_manager::send_unix_process_signal(pid, signal);
    }
}

pub(crate) fn terminate_external_listener(pid: u32, port: u16) -> Result<()> {
    #[cfg(unix)]
    {
        // Re-verify the occupant immediately before signaling: the caller's
        // identification work spans several subprocesses, so the recorded
        // pid may have exited and been recycled by an innocent process.
        match detect_port_listener(port)? {
            Some(listener) if listener.pid == pid => {}
            Some(listener) => bail!(
                "port {port} listener changed to pid {} before it could be signaled",
                listener.pid
            ),
            None => return Ok(()),
        }
        signal_listener_process_tree(pid, "-TERM");
        wait_for_port_to_clear(port, pid, Duration::from_secs(KILL_TIMEOUT_SECS))?;
        Ok(())
    }

    #[cfg(windows)]
    {
        let status = process_utils::command("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .status()
            .context("failed to run taskkill")?;
        if !status.success() {
            bail!("taskkill exited with {status}");
        }
        wait_for_port_to_clear(port, pid, Duration::from_secs(KILL_TIMEOUT_SECS))?;
        return Ok(());
    }

    #[cfg(not(any(unix, windows)))]
    {
        let _ = (pid, port);
        bail!("port-conflict recovery is not supported on this platform");
    }
}

fn wait_for_port_to_clear(port: u16, expected_pid: u32, timeout: Duration) -> Result<()> {
    let deadline = Instant::now() + timeout;

    loop {
        match detect_port_listener(port)? {
            None => return Ok(()),
            Some(listener) if listener.pid != expected_pid => {
                bail!(
                    "port {port} is now occupied by pid {} ({})",
                    listener.pid,
                    listener.summary(),
                );
            }
            Some(_) if Instant::now() >= deadline => break,
            Some(_) => thread::sleep(Duration::from_millis(250)),
        }
    }

    #[cfg(unix)]
    {
        warn!("Port {port} is still occupied after SIGTERM; sending SIGKILL to pid {expected_pid}");
        // Same pid+port fence as the initial signal: the loop's last
        // detection is up to 250ms stale.
        match detect_port_listener(port)? {
            Some(listener) if listener.pid == expected_pid => {}
            Some(listener) => bail!(
                "port {port} listener changed to pid {} before SIGKILL",
                listener.pid
            ),
            None => return Ok(()),
        }
        signal_listener_process_tree(expected_pid, "-KILL");
        let kill_deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < kill_deadline {
            match detect_port_listener(port)? {
                None => return Ok(()),
                Some(listener) if listener.pid != expected_pid => {
                    bail!(
                        "port {port} is now occupied by pid {} ({})",
                        listener.pid,
                        listener.summary(),
                    );
                }
                Some(_) => thread::sleep(Duration::from_millis(200)),
            }
        }
    }

    bail!("port {port} remained occupied by pid {expected_pid}")
}

impl Drop for KernelManager {
    fn drop(&mut self) {
        if self.is_running() {
            if let Err(e) = self.stop() {
                error!("Failed to stop Kernel during drop: {e}");
            }
        }
    }
}

/// Reap a stale Launcher-owned Kernel listener on `port` when its identity
/// can be proven from its argv and working directory.
///
/// Returns `true` when the port was already free or was recovered, and
/// `false` when the occupant is not provably Launcher-owned. This is the
/// port-resolution counterpart of `KernelManager::recover_port_conflict`:
/// without it a still-initializing previous Kernel silently pushes the new
/// session onto a shifted port and two Kernels end up owning the same state
/// directories.
pub(crate) fn reclaim_stale_kernel_port(config: &AppConfig, port: u16) -> Result<bool> {
    let Some(listener) = detect_port_listener(port)? else {
        // An empty detection is not proof the port is free — a missing or
        // timed-out lsof must not bind a port another process still owns.
        return Ok(crate::is_loopback_port_available(port));
    };
    let Some(identity) = identify_owned_listener(&listener, config) else {
        return Ok(false);
    };
    warn!(
        "Detected stale Rumi listener on port {port}: pid {} ({}; {})",
        listener.pid,
        listener.summary(),
        identity.description(),
    );
    terminate_external_listener(listener.pid, port)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn test_config() -> AppConfig {
        AppConfig::detect_for_tauri(
            PathBuf::from("/tmp/test_resource"),
            PathBuf::from("/tmp/test_appdata"),
        )
        .unwrap()
    }

    fn temporary_packaged_config(label: &str) -> (PathBuf, AppConfig) {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri-{label}-{unique}"));
        let resource_dir = root.join("resources");
        fs::create_dir_all(resource_dir.join("app")).unwrap();
        let config = AppConfig::detect_for_tauri(resource_dir, root.join("appdata")).unwrap();
        assert!(!config.is_dev_workspace());
        (root, config)
    }

    #[test]
    fn is_running_default_false() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        assert!(!km.is_running());
    }

    #[cfg(unix)]
    #[test]
    fn panel_reauthorization_preserves_a_kernel_with_slow_health_readiness() {
        use std::io::{Read, Write};
        use std::net::TcpListener;
        use std::sync::{mpsc, Arc, Mutex};
        use std::time::{Duration, Instant};

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let config = AppConfig {
            app_dir: PathBuf::new(),
            rumi_home: PathBuf::new(),
            python_dir: PathBuf::new(),
            uv_path: PathBuf::new(),
            venv_dir: PathBuf::new(),
            user_data_dir: PathBuf::new(),
            log_dir: PathBuf::new(),
            kernel_port: listener.local_addr().unwrap().port(),
            dev_workspace_root: None,
        };
        let (stop_server, stop_requested) = mpsc::channel();
        let server = std::thread::spawn(move || {
            let mut first_request = None;
            while matches!(stop_requested.try_recv(), Err(mpsc::TryRecvError::Empty)) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        // Accepted sockets may inherit nonblocking mode. Read
                        // the complete request before closing the connection;
                        // unread request bytes can turn the response into a reset.
                        stream.set_nonblocking(false).unwrap();
                        stream
                            .set_read_timeout(Some(Duration::from_secs(1)))
                            .unwrap();
                        stream
                            .set_write_timeout(Some(Duration::from_secs(1)))
                            .unwrap();
                        let mut request = Vec::new();
                        let mut byte = [0];
                        while !request.ends_with(b"\r\n\r\n") && request.len() < 8192 {
                            if stream.read_exact(&mut byte).is_err() {
                                break;
                            }
                            request.push(byte[0]);
                        }
                        if !request.ends_with(b"\r\n\r\n") {
                            continue;
                        }
                        let started = first_request.get_or_insert_with(Instant::now);
                        let ready = started.elapsed() >= Duration::from_secs(7);
                        let status = if ready { "200 OK" } else { "503 Unavailable" };
                        let _ = write!(
                            stream,
                            "HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        );
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(10));
                    }
                    Err(error) => panic!("health fixture failed: {error}"),
                }
            }
        });
        let child = std::process::Command::new("/bin/sleep")
            .arg("120")
            .spawn()
            .unwrap();
        let pid = child.id();
        let mut manager = KernelManager::new(&config, "test-bootstrap".into());
        manager.child = Some(crate::python_env::PythonChild::development(child));
        let manager = Arc::new(Mutex::new(manager));

        let result = crate::ensure_kernel_ready_for_panel_auth(&config, &manager);
        stop_server.send(()).unwrap();
        let mut kernel = manager.lock().unwrap();
        let retained_pid = kernel.child.as_ref().map(|child| child.id());
        let still_running = kernel.is_running();
        kernel.stop().unwrap();
        server.join().unwrap();
        result.unwrap();
        assert_eq!(retained_pid, Some(pid));
        assert!(still_running);
    }

    #[test]
    fn stop_without_start_is_ok() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        assert!(km.stop().is_ok());
    }

    #[cfg(unix)]
    #[test]
    fn stop_force_kills_a_term_ignoring_kernel_within_quit_budget() {
        let (root, config) = temporary_packaged_config("kernel-term-ignore");
        let ready_file = std::env::temp_dir().join(format!(
            "tobkiri-kernel-term-ignore-{}-{}.ready",
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
        let child = process_utils::command("/bin/sh")
            .args(["-c", &script])
            .spawn()
            .unwrap();
        let mut kernel = KernelManager::new(&config, "test-bootstrap".into());
        kernel.child = Some(crate::python_env::PythonChild::development(child));
        assert!((0..40).any(|_| {
            if ready_file.exists() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(25));
            false
        }));

        let started = Instant::now();
        kernel.stop().unwrap();
        fs::remove_file(ready_file).ok();

        assert!(
            started.elapsed() < Duration::from_secs(3),
            "forced kernel shutdown exceeded its share of the quit budget"
        );
        assert!(kernel.child.is_none());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn wait_and_handle_restart_no_child() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        let result = km.wait_and_handle_restart().unwrap();
        assert!(!result);
    }

    #[test]
    fn restart_exit_code_requests_restart_without_child() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        km.last_exit_code = Some(RESTART_EXIT_CODE);

        let result = km.wait_and_handle_restart().unwrap();

        assert!(result);
    }

    #[test]
    fn successful_start_generation_rejects_stale_guardian_work() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());

        assert_eq!(km.launch_generation(), 0);
        assert!(km.is_current_launch_generation(0));

        let first_generation = km.next_launch_generation().unwrap();
        km.launch_generation = first_generation;
        let restarted_generation = km.next_launch_generation().unwrap();
        km.launch_generation = restarted_generation;

        assert_eq!(first_generation, 1);
        assert_eq!(restarted_generation, 2);
        assert_ne!(first_generation, restarted_generation);
        assert!(!km.is_current_launch_generation(first_generation));
    }

    #[test]
    fn clean_exit_does_not_request_restart_without_child() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        km.last_exit_code = Some(0);

        let result = km.wait_and_handle_restart().unwrap();

        assert!(!result);
    }

    #[test]
    fn python_runtime_env_forces_utf8_output() {
        let envs = python_runtime_env_vars();

        assert!(envs.contains(&("PYTHONUTF8", "1")));
        assert!(envs.contains(&("PYTHONIOENCODING", "utf-8")));
        assert!(envs.contains(&("PYTHONUNBUFFERED", "1")));
        assert!(envs.contains(&("PYTHONDONTWRITEBYTECODE", "1")));
    }

    #[test]
    fn only_typed_reconfirmation_states_fall_back_to_bootstrap() {
        let shell = anyhow::Error::new(crate::defaultspack_authority::ShellReconfirmationRequired);
        let profile =
            anyhow::Error::new(crate::defaultspack_authority::ProfileReresolutionRequired);
        let malformed = anyhow::anyhow!("active Profile pointer is malformed");

        assert!(requires_setup_reconfirmation(&shell));
        assert!(requires_setup_reconfirmation(&profile));
        assert!(!requires_setup_reconfirmation(&malformed));
    }

    #[test]
    fn bundled_kernel_uses_writable_app_data_as_working_directory() {
        let config = test_config();

        assert_eq!(kernel_working_dir(&config), Path::new("/tmp/test_appdata"));
    }

    #[test]
    fn bundled_kernel_does_not_require_legacy_writable_venv() {
        let config = test_config();

        assert!(!config.is_dev_workspace());
        assert!(!config.venv_python().exists());
        require_development_venv(&config).unwrap();
    }

    #[test]
    fn packaged_kernel_defers_outer_verification_to_authoritative_role_spawn() {
        let (root, mut config) = temporary_packaged_config("kernel-spawn-authority");
        config.kernel_port = 0;
        fs::write(
            config
                .app_dir
                .join(crate::runtime_resource_integrity::MANIFEST_NAME),
            b"not a resource manifest",
        )
        .unwrap();

        let error = KernelManager::new(&config, "test-bootstrap".into())
            .start()
            .unwrap_err()
            .to_string();

        // A preflight `runtime_resource_integrity::verify` would fail on the
        // malformed outer manifest before role spawn. The only failure path is
        // now the authoritative packaged role spawn, which still fails closed.
        assert!(error.contains("failed to verify and spawn Kernel process"));
        assert!(!error.contains("packaged runtime integrity verification failed"));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn no_active_profile_publishes_only_a_distinct_bootstrap_contract() {
        let (root, config) = temporary_packaged_config("kernel-bootstrap-contract");

        write_kernel_host_contract(&config, "bootstrap-secret").unwrap();

        let contract: serde_json::Value = serde_json::from_slice(
            &fs::read(crate::host_contract::contract_path(&config)).unwrap(),
        )
        .unwrap();
        assert_eq!(contract["profile_id"], "defaults");
        assert_ne!(
            contract["profile_revision"],
            serde_json::Value::String(format!("sha256:{}", "0".repeat(64))),
            "bootstrap must not use the former all-zero digest"
        );
        assert_ne!(contract["profile_revision"], contract["plan_digest"]);
        assert_eq!(
            contract["values"]["panel_bootstrap_secret"],
            "bootstrap-secret"
        );
        assert!(contract["values"].get("system_pack_descriptors").is_none());
        assert!(crate::host_contract::read_identity(&config).is_none());
        assert!(crate::host_contract::read_value(&config, "panel_bootstrap_secret").is_none());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn malformed_active_pointer_fails_closed_instead_of_falling_back_to_bootstrap() {
        let (root, config) = temporary_packaged_config("kernel-corrupt-active-contract");
        let profiles = config.user_data_dir.join("profiles");
        fs::create_dir_all(&profiles).unwrap();
        fs::write(profiles.join("active.json"), br#"{"not":"an authority"}"#).unwrap();

        let error = write_kernel_host_contract(&config, "bootstrap-secret").unwrap_err();

        assert!(format!("{error:#}").contains("active Profile pointer"));
        assert!(
            !crate::host_contract::contract_path(&config).exists(),
            "a corrupt active authority must not be replaced with a bootstrap contract"
        );
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn exact_rumi_home_cwd_is_recoverable() {
        let config = test_config();
        let listener = PortListener {
            pid: 100,
            command: "/opt/homebrew/bin/python3 -m app".into(),
            cwd: Some(config.rumi_home.to_string_lossy().into_owned()),
        };

        assert_eq!(
            identify_owned_listener(&listener, &config),
            Some(ListenerIdentity::WorkingDirectory),
        );
    }

    #[test]
    fn exact_venv_python_command_is_recoverable() {
        let config = test_config();
        let listener = PortListener {
            pid: 101,
            command: format!("{} -m app", config.venv_python().display()),
            cwd: None,
        };

        assert_eq!(
            identify_owned_listener(&listener, &config),
            Some(ListenerIdentity::VenvPython),
        );
    }

    #[test]
    fn exact_entrypoint_path_is_recoverable() {
        let config = test_config();
        let listener = PortListener {
            pid: 102,
            command: format!(
                "/usr/bin/python3 {}",
                config.rumi_home.join("app.py").display()
            ),
            cwd: None,
        };

        assert_eq!(
            identify_owned_listener(&listener, &config),
            Some(ListenerIdentity::EntrypointPath),
        );
    }

    #[test]
    fn does_not_flag_foreign_python_process_from_other_rumi_checkout() {
        let config = test_config();
        let listener = PortListener {
            pid: 103,
            command: "/usr/bin/python3 -m app".into(),
            cwd: Some("/Users/haru/dev/rumi-playground/tobkiri_runtime".into()),
        };

        assert_eq!(identify_owned_listener(&listener, &config), None);
    }

    #[test]
    fn does_not_flag_non_python_process_even_if_path_mentions_rumi_home() {
        let config = test_config();
        let listener = PortListener {
            pid: 104,
            command: format!(
                "/usr/bin/node {}",
                config.rumi_home.join("app.js").display()
            ),
            cwd: Some(config.rumi_home.to_string_lossy().into_owned()),
        };

        assert_eq!(identify_owned_listener(&listener, &config), None);
    }

    #[test]
    fn sealed_kernel_argv_is_recoverable_from_its_environment_root() {
        let config = test_config();
        // The stamped owner must be dead for the listener to be stale; a
        // guaranteed-unowned pid keeps the test deterministic.
        let root = format!(
            "/private/tmp/.tobkiri-sealed-python-999999999-{}",
            "a".repeat(64)
        );
        let listener = PortListener {
            pid: 105,
            command: format!(
                "python3 -I -B -m tobkiri_sealed.bootstrap --role typed --nonce {} --environment-root {}",
                "b".repeat(64),
                root
            ),
            cwd: Some(root),
        };

        assert_eq!(
            identify_owned_listener(&listener, &config),
            Some(ListenerIdentity::SealedEnvironment),
        );
    }

    #[test]
    fn sealed_non_kernel_roles_are_never_flagged_as_stale_kernels() {
        let config = test_config();
        for role in ["defaultspack", "host_helper"] {
            let listener = PortListener {
                pid: 106,
                command: format!(
                    "python3 -I -B -m tobkiri_sealed.bootstrap --role {role} --nonce {}",
                    "b".repeat(64)
                ),
                cwd: Some(
                    "/private/tmp/.tobkiri-sealed-python-4321-".to_string() + &"a".repeat(64),
                ),
            };

            assert_eq!(
                identify_owned_listener(&listener, &config),
                None,
                "sealed role {role} must not be reaped as a stale Kernel"
            );
        }
    }

    #[test]
    fn restart_start_backoff_is_bounded() {
        assert_eq!(kernel_restart_start_backoff(1), Duration::from_millis(500));
        assert_eq!(kernel_restart_start_backoff(2), Duration::from_secs(1));
        assert_eq!(kernel_restart_start_backoff(4), Duration::from_secs(4));
        assert_eq!(kernel_restart_start_backoff(7), Duration::from_secs(30));
        assert_eq!(
            kernel_restart_start_backoff(u32::MAX),
            Duration::from_secs(30)
        );
    }

    #[test]
    fn restart_debt_survives_a_consumed_exit_status() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());

        km.last_exit_code = Some(RESTART_EXIT_CODE);
        assert!(km.wait_and_handle_restart().unwrap());
        assert!(km.restart_owed());

        // A failed start attempt leaves no child and no new exit status, but
        // the monitor must still observe the owed restart.
        assert!(km.wait_and_handle_restart().unwrap());
        assert!(km.restart_owed());
    }

    #[test]
    fn clean_exit_clears_the_restart_debt() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        km.restart_owed = true;
        km.last_exit_code = Some(0);

        assert!(!km.wait_and_handle_restart().unwrap());
        assert!(!km.restart_owed());
    }

    #[test]
    fn restart_start_failures_feed_a_bounded_retry_budget() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        km.restart_owed = true;

        for expected in 1..MAX_KERNEL_RESTART_START_ATTEMPTS {
            assert_eq!(km.record_restart_start_failure(), expected);
            assert!(km.restart_owed());
        }

        km.abandon_restart_owed();
        assert!(!km.restart_owed());
        assert_eq!(km.restart_start_failures, 0);
        assert!(!km.wait_and_handle_restart().unwrap());
    }

    #[test]
    fn failed_start_preserves_restart_eligibility() {
        let (root, mut config) = temporary_packaged_config("kernel-restart-debt");
        config.kernel_port = 0;
        fs::write(
            config
                .app_dir
                .join(crate::runtime_resource_integrity::MANIFEST_NAME),
            b"not a resource manifest",
        )
        .unwrap();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        km.restart_owed = true;

        let error = km.start().unwrap_err().to_string();

        assert!(error.contains("failed to verify and spawn Kernel process"));
        assert!(
            km.restart_owed(),
            "a failed start must not consume restart eligibility"
        );
        fs::remove_dir_all(root).ok();
    }
}
