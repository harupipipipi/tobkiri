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
use std::path::Path;
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
}

impl ListenerIdentity {
    fn description(self) -> &'static str {
        match self {
            Self::WorkingDirectory => "matched the configured RUMI_HOME working directory",
            Self::EntrypointPath => "matched the configured Kernel entrypoint path",
            Self::VenvPython => "matched the configured venv Python path",
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
}

impl KernelManager {
    pub fn new(config: &AppConfig, panel_bootstrap_secret: String) -> Self {
        Self {
            child: None,
            config: config.clone(),
            panel_bootstrap_secret,
            last_exit_code: None,
            restart_count: 0,
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

        fs::create_dir_all(&self.config.log_dir)?;
        let log_file = fs::File::create(self.config.log_dir.join("kernel.log"))
            .context("failed to create kernel.log")?;
        let log_stderr = log_file
            .try_clone()
            .context("failed to clone log file handle")?;

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
        let host_contract_path = crate::host_contract::write_contract(
            &self.config,
            crate::host_contract::DEFAULT_PROFILE_ID,
            [(
                "panel_bootstrap_secret",
                self.panel_bootstrap_secret.clone(),
            )],
        )?;

        let child = crate::python_env::spawn_python_role(
            &self.config,
            crate::python_env::PythonRole::Kernel,
            crate::python_env::RoleArguments::default(),
            |command| {
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
                    .stdout(Stdio::from(log_file))
                    .stderr(Stdio::from(log_stderr));
                Ok(())
            },
        )
        .context("failed to verify and spawn Kernel process")?;

        info!("Kernel started (pid {})", child.id());
        self.child = Some(child);
        self.last_exit_code = None;
        Ok(())
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
    /// Returns `true` if the caller should call `start()` again.
    pub fn wait_and_handle_restart(&mut self) -> Result<bool> {
        if let Some(child) = self.child.as_mut() {
            let status = child.wait().context("failed to wait on Kernel")?;
            let code = status.code().unwrap_or(-1);
            self.last_exit_code = Some(code);
            self.child = None;
        }

        match self.last_exit_code.take() {
            Some(RESTART_EXIT_CODE) => {
                info!("Kernel exited with code 42 -- restart requested");
                self.restart_count = 0;
                Ok(true)
            }
            Some(0) => {
                info!("Kernel exited normally (code 0)");
                Ok(false)
            }
            Some(code) => {
                self.restart_count += 1;
                if self.restart_count <= MAX_AUTO_RESTARTS {
                    warn!(
                        "Kernel exited with code {code} -- auto-restart {}/{}",
                        self.restart_count, MAX_AUTO_RESTARTS
                    );
                    Ok(true)
                } else {
                    error!(
                        "Kernel exited with code {code} -- max restarts ({}) exceeded, giving up",
                        MAX_AUTO_RESTARTS
                    );
                    Ok(false)
                }
            }
            None => Ok(false),
        }
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
        use std::thread;
        use std::time::Duration;

        let pid = child.id() as i32;
        let _ = process_utils::command("kill")
            .args(["-TERM", &pid.to_string()])
            .status();

        for _ in 0..KILL_TIMEOUT_SECS {
            thread::sleep(Duration::from_secs(1));
            if let Ok(Some(_)) = child.try_wait() {
                return Ok(());
            }
        }

        warn!("Kernel did not exit after SIGTERM, sending SIGKILL");
        child.kill().ok();
        child.wait().ok();
        Ok(())
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
    let output = match process_utils::command("lsof")
        .args(["-nP", &format!("-iTCP:{port}"), "-sTCP:LISTEN", "-Fpc"])
        .output()
    {
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
    let output = process_utils::command("netstat")
        .args(["-ano", "-p", "tcp"])
        .output()
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
    let output = process_utils::command("ps")
        .args(["-p", &pid.to_string(), "-o", "command="])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!text.is_empty()).then_some(text)
}

#[cfg(unix)]
fn unix_process_cwd(pid: u32) -> Option<String> {
    let output = process_utils::command("lsof")
        .args(["-a", "-p", &pid.to_string(), "-d", "cwd", "-Fn"])
        .output()
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
    let output = process_utils::command("tasklist")
        .args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"])
        .output()
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

    None
}

fn is_python_app_command(command: &str) -> bool {
    let command = normalize_for_match(command);
    command.contains("python") && (command.contains("app.py") || command.contains("-m app"))
}

fn command_mentions_path(command: &str, path: &Path) -> bool {
    normalize_for_match(command).contains(&normalize_path_for_match(path))
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

pub(crate) fn terminate_external_listener(pid: u32, port: u16) -> Result<()> {
    #[cfg(unix)]
    {
        let pid_str = pid.to_string();
        let _ = process_utils::command("kill")
            .args(["-TERM", &pid_str])
            .status();
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
        let _ = process_utils::command("kill")
            .args(["-KILL", &expected_pid.to_string()])
            .status();
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

    #[test]
    fn stop_without_start_is_ok() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        assert!(km.stop().is_ok());
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
}
