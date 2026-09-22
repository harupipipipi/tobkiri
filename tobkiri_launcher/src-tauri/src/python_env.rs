//! Python launch authority and development-only bootstrap.
//!
//! Packaged builds use the build-bound sealed environment in
//! `sealed_python`; they never execute `uv`. External provisioning is a
//! segregated development facility and is rejected before spawn on macOS and
//! unless a delegated cgroup-v2/systemd boundary exists on Linux. Windows
//! provisioning runs suspended inside a kill-on-close Job Object.
//!
//! Development bootstrap flow:
//! 1. Ensure a trusted `uv` binary is available (bundled/dev/PATH only)
//! 2. `uv python install 3.13.13`      (into a temp dir, then rename)
//! 3. `uv venv`                         (create virtual-environment)
//! 4. `uv pip install -r requirements.txt`
//!
//! Each step is idempotent — if the artefact already exists the step is
//! skipped.

use std::collections::HashSet;
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
#[cfg(windows)]
use std::process::Child;
use std::process::Command;
#[cfg(windows)]
use std::process::{ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use hmac::{Hmac, Mac};
use log::{info, warn};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::config::AppConfig;
use crate::process_utils;

#[cfg(unix)]
use std::os::unix::{fs::MetadataExt, io::AsRawFd};
#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(windows)]
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_LOCK_VIOLATION, HANDLE};
#[cfg(windows)]
use windows_sys::Win32::Storage::FileSystem::{
    GetFileInformationByHandle, LockFileEx, UnlockFileEx, BY_HANDLE_FILE_INFORMATION,
    LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY,
};
#[cfg(windows)]
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
};
#[cfg(windows)]
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
#[cfg(windows)]
use windows_sys::Win32::System::Threading::{
    OpenThread, ResumeThread, CREATE_SUSPENDED, THREAD_SUSPEND_RESUME,
};
#[cfg(windows)]
use windows_sys::Win32::System::IO::OVERLAPPED;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Pinned CPython patch version. Avoid resolving a mutable latest patch at startup.
const PYTHON_VERSION: &str = "3.13.13";
const PYTHON_MINOR: &str = "3.13";
const PROVISION_TIMEOUT_SECS: u64 = 15 * 60;
const PROVISION_LOCK_WAIT_SECS: u64 = 15 * 60;
const LONG_OPERATION_NOTICE_SECS: u64 = 60;
const CHILD_POLL_INTERVAL: Duration = Duration::from_millis(100);
#[cfg(windows)]
const MAX_CAPTURED_COMMAND_OUTPUT: usize = 256 * 1024;
const RUNTIME_MARKER: &str = ".tobkiri-python-runtime";
const RUNTIME_MARKER_CONTENT: &str = "tobkiri-python-runtime-v2\nversion=3.13.13\n";
const TRUST_DIRECTORY: &str = ".python_install_trust";
const TRUST_KEY_FILE: &str = "authority.key";
const RUNTIME_TRUST_FILE: &str = "runtime.v2.json";
const VENV_TRUST_FILE: &str = "venv.v2.json";
const REQUIREMENTS_SNAPSHOT_FILE: &str = "requirements.locked.txt";
const TRUST_SCHEMA: &str = "tobkiri.python-install-trust.v2";
const MAX_TREE_ENTRIES: usize = 100_000;

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct TreeEntryIdentity {
    path: String,
    kind: String,
    size: u64,
    sha256: String,
    link_target: String,
    mode: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct TreeIdentity {
    digest: String,
    entries: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct InstallTrustPayload {
    schema: String,
    kind: String,
    python_version: String,
    tree: TreeIdentity,
    runtime_install_identity: String,
    requirements_sha256: String,
    uv_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct SignedInstallTrust {
    payload: InstallTrustPayload,
    hmac_sha256: String,
}

#[derive(Debug)]
struct RuntimeTrust {
    install_identity: String,
    root_guard: File,
}

/// Stable error categories for the startup splash and diagnostics.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PythonProvisioningCode {
    LockBusy,
    ChildSpawn,
    ChildFailed,
    NetworkUnavailable,
    Timeout,
    InvalidArtifact,
    PathTampered,
    CleanupFailed,
    DiagnosticsUnavailable,
    ContainmentUnavailable,
}

impl PythonProvisioningCode {
    fn as_str(self) -> &'static str {
        match self {
            Self::LockBusy => "PYTHON_PROVISION_LOCK_BUSY",
            Self::ChildSpawn => "PYTHON_PROVISION_CHILD_SPAWN",
            Self::ChildFailed => "PYTHON_PROVISION_CHILD_FAILED",
            Self::NetworkUnavailable => "PYTHON_PROVISION_NETWORK_UNAVAILABLE",
            Self::Timeout => "PYTHON_PROVISION_TIMEOUT",
            Self::InvalidArtifact => "PYTHON_PROVISION_INVALID_ARTIFACT",
            Self::PathTampered => "PYTHON_PROVISION_PATH_TAMPERED",
            Self::CleanupFailed => "PYTHON_PROVISION_CLEANUP_FAILED",
            Self::DiagnosticsUnavailable => "PYTHON_PROVISION_DIAGNOSTICS_UNAVAILABLE",
            Self::ContainmentUnavailable => "PYTHON_PROVISION_CONTAINMENT_UNAVAILABLE",
        }
    }
}

/// Typed, user-actionable failure from the packaged Python bootstrap.
#[derive(Debug, Clone)]
pub struct PythonProvisioningError {
    code: PythonProvisioningCode,
    message: String,
}

impl PythonProvisioningError {
    fn new(code: PythonProvisioningCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    /// Return the stable machine-readable failure category.
    pub fn code(&self) -> PythonProvisioningCode {
        self.code
    }
}

impl fmt::Display for PythonProvisioningError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "[{}] {}", self.code.as_str(), self.message)
    }
}

impl std::error::Error for PythonProvisioningError {}

fn typed_error(code: PythonProvisioningCode, message: impl Into<String>) -> anyhow::Error {
    PythonProvisioningError::new(code, message).into()
}

#[derive(Clone, Copy)]
struct ProvisionOptions {
    deadline: Instant,
    timeout_secs: u64,
    lock_wait: Duration,
    long_operation_notice: Duration,
}

impl ProvisionOptions {
    fn production() -> Self {
        Self {
            deadline: Instant::now() + Duration::from_secs(PROVISION_TIMEOUT_SECS),
            timeout_secs: PROVISION_TIMEOUT_SECS,
            lock_wait: Duration::from_secs(PROVISION_LOCK_WAIT_SECS),
            long_operation_notice: Duration::from_secs(LONG_OPERATION_NOTICE_SECS),
        }
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Ensure that a working Python venv with all dependencies is present.
///
/// Steps (each is idempotent):
/// 1. Ensure uv binary        → bundled/dev/PATH only
/// 2. uv python install 3.13.13 → `config.python_dir`
/// 3. uv venv                 → `config.venv_dir`
/// 4. uv pip install           → into the venv
pub fn ensure_python_env(config: &AppConfig) -> Result<()> {
    ensure_python_env_with_progress(config, |message| info!("{message}"))
}

/// Ensure the Python environment while sending long-running setup progress to
/// the caller's splash or other UI surface. Packaged verification is
/// deliberately deferred to `spawn_packaged_role`: that is the authoritative
/// fail-closed boundary which verifies the sealed environment and, on macOS,
/// creates the snapshot retained by the child. Verifying here would construct
/// and immediately destroy a second snapshot before the real launch.
pub fn ensure_python_env_with_progress<F>(config: &AppConfig, progress: F) -> Result<()>
where
    F: Fn(&str),
{
    if !config.is_dev_workspace() {
        // `spawn_packaged_role` is the single authority for the outer-runtime
        // and sealed-environment verification immediately before execution.
        // Do not add a packaged preflight here: it duplicates the full hash
        // and macOS snapshot construction without shrinking the launch race.
        progress("Packaged Python will be verified immediately before the runtime starts...");
        return Ok(());
    }
    let options = ProvisionOptions::production();
    let lock_path = provision_lock_path(config);
    let lock = acquire_provision_lock(&lock_path, options.lock_wait)?;
    let trust_key = load_or_create_trust_key(config)
        .context("Python install trust authority is unavailable")?;
    let log_path = provision_log_path(config)?;

    let result = (|| {
        progress("Checking the bundled Python runtime...");
        ensure_uv(config).context("uv setup failed")?;
        let uv = trusted_uv_path(config)?;
        let cache_dir = ensure_uv_cache_dir(config)?;

        let requirements_sha256 = locked_requirements_identity(config)?;
        let runtime = ensure_python(
            config, &uv, &cache_dir, &log_path, &trust_key, options, &progress,
        )
        .context("Python runtime provisioning failed")?;
        let venv_ready = ensure_venv(
            config,
            &uv,
            &cache_dir,
            &log_path,
            &trust_key,
            &runtime,
            &requirements_sha256,
            options,
            &progress,
        )
        .context("venv creation failed")?;
        install_requirements(
            config,
            &uv,
            &cache_dir,
            &log_path,
            &trust_key,
            &runtime,
            &requirements_sha256,
            venv_ready,
            options,
            &progress,
        )
        .context("pip install failed")?;
        Ok(())
    })();

    let release_result = lock.release();
    match (result, release_result) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(error), Ok(())) => Err(error),
        (Ok(()), Err(error)) => Err(error),
        (Err(error), Err(release_error)) => Err(anyhow::anyhow!(
            "{error}; also failed to release the Python provisioning lock: {release_error}"
        )),
    }
}

pub use crate::sealed_python::{PythonChild, PythonRole, RoleArguments, RoleCommand};

/// Spawn one fixed Python role. Packaged mode accepts only the build-bound
/// sealed environment; development mode is explicitly segregated and may use
/// the externally provisioned developer venv.
pub fn spawn_python_role<F>(
    config: &AppConfig,
    role: PythonRole,
    role_arguments: RoleArguments,
    configure: F,
) -> Result<PythonChild>
where
    F: FnOnce(&mut RoleCommand<'_>) -> Result<()>,
{
    if !config.is_dev_workspace() {
        return crate::sealed_python::spawn_packaged_role(config, role, role_arguments, configure);
    }
    let mut command = process_utils::isolated_python(config.venv_python());
    match role {
        PythonRole::Kernel => {
            command.args(["-m", "app"]);
        }
        PythonRole::Defaultspack => {
            command
                .arg(
                    config
                        .app_dir
                        .join("ecosystem/defaultspack/defaultspack/desktop_app.py"),
                )
                .args(role_arguments.into_values());
        }
        PythonRole::HostHelper => {
            command.arg(
                config
                    .app_dir
                    .join("core_runtime/host_broker/computer_host_helper.py"),
            );
        }
    }
    configure(&mut RoleCommand::new(&mut command))?;
    command
        .spawn()
        .map(PythonChild::development)
        .context("failed to spawn development Python role")
}

// ---------------------------------------------------------------------------
// Step 1 — uv binary
// ---------------------------------------------------------------------------

fn trusted_uv_path(config: &AppConfig) -> Result<PathBuf> {
    config.trusted_uv_path().ok_or_else(|| {
        typed_error(
            PythonProvisioningCode::ChildSpawn,
            format!(
                "no trusted uv binary found; bundle {} with the app, set RUMI_UV_PATH to a user-managed uv binary, or install uv on PATH, then Retry",
                config.bundled_uv_path().display()
            ),
        )
    })
}

fn ensure_uv(config: &AppConfig) -> Result<()> {
    let uv = trusted_uv_path(config)?;
    info!("Using trusted uv at {}", uv.display());
    Ok(())
}

fn provision_lock_path(config: &AppConfig) -> PathBuf {
    config.python_dir.with_file_name("_python_provision.lock")
}

fn provision_log_path(config: &AppConfig) -> Result<PathBuf> {
    provision_state_root(config)?;
    ensure_real_directory(&config.log_dir, "Python provisioning log directory")?;
    fs::create_dir_all(&config.log_dir).map_err(|error| {
        typed_error(
            PythonProvisioningCode::DiagnosticsUnavailable,
            format!(
                "cannot create Python provisioning log directory {}: {error}; retry the launcher",
                config.log_dir.display()
            ),
        )
    })?;
    restrict_private_directory(&config.log_dir)?;
    let path = config.log_dir.join("python-provision.log");
    reject_link(&path, "Python provisioning log")?;
    open_append_nofollow(&path)
        .map(|_| path.clone())
        .map_err(|error| {
            typed_error(
                PythonProvisioningCode::DiagnosticsUnavailable,
                format!(
                    "cannot write Python provisioning diagnostics at {}: {error}; retry the launcher",
                    path.display()
                ),
            )
        })
}

fn ensure_uv_cache_dir(config: &AppConfig) -> Result<PathBuf> {
    let parent = config.python_dir.parent().ok_or_else(|| {
        typed_error(
            PythonProvisioningCode::PathTampered,
            format!(
                "Python runtime path {} has no parent",
                config.python_dir.display()
            ),
        )
    })?;
    ensure_real_directory(parent, "Python runtime state directory")?;
    fs::create_dir_all(parent).map_err(|error| {
        typed_error(
            PythonProvisioningCode::DiagnosticsUnavailable,
            format!(
                "cannot create Python runtime state directory {}: {error}; retry the launcher",
                parent.display()
            ),
        )
    })?;

    let cache_dir = parent.join("_uv_cache");
    reject_link(&cache_dir, "uv cache directory")?;
    if path_exists_or_reparse_point(&cache_dir) && !cache_dir.is_dir() {
        return Err(typed_error(
            PythonProvisioningCode::PathTampered,
            format!(
                "uv cache path {} is not a directory; remove it and retry",
                cache_dir.display()
            ),
        ));
    }
    fs::create_dir_all(&cache_dir).map_err(|error| {
        typed_error(
            PythonProvisioningCode::DiagnosticsUnavailable,
            format!(
                "cannot create uv cache directory {}: {error}; retry the launcher",
                cache_dir.display()
            ),
        )
    })?;
    restrict_private_directory(&cache_dir)?;
    let canonical_parent = fs::canonicalize(parent)?;
    let canonical_cache = fs::canonicalize(&cache_dir)?;
    if canonical_cache.parent() != Some(canonical_parent.as_path()) {
        return Err(typed_error(
            PythonProvisioningCode::PathTampered,
            format!("uv cache {} escaped its state root", cache_dir.display()),
        ));
    }
    Ok(cache_dir)
}

fn provision_state_root(config: &AppConfig) -> Result<PathBuf> {
    let root = config.python_dir.parent().ok_or_else(|| {
        typed_error(
            PythonProvisioningCode::PathTampered,
            "Python runtime has no state root",
        )
    })?;
    for (label, path) in [
        ("Python venv", &config.venv_dir),
        ("Python logs", &config.log_dir),
    ] {
        if path.parent() != Some(root) {
            return Err(typed_error(
                PythonProvisioningCode::PathTampered,
                format!("{label} {} escapes the Python state root", path.display()),
            ));
        }
    }
    ensure_real_directory(root, "Python state root")?;
    fs::create_dir_all(root)?;
    let canonical = fs::canonicalize(root).map_err(|error| {
        typed_error(
            PythonProvisioningCode::PathTampered,
            format!(
                "cannot resolve Python state root {}: {error}",
                root.display()
            ),
        )
    })?;
    for path in [&config.python_dir, &config.venv_dir, &config.log_dir] {
        if let Some(parent) = path.parent() {
            if fs::canonicalize(parent).ok().as_deref() != Some(canonical.as_path()) {
                return Err(typed_error(
                    PythonProvisioningCode::PathTampered,
                    format!(
                        "Python state path {} changed ownership scope",
                        path.display()
                    ),
                ));
            }
        }
    }
    Ok(canonical)
}

fn trust_directory(config: &AppConfig) -> Result<PathBuf> {
    let directory = provision_state_root(config)?.join(TRUST_DIRECTORY);
    reject_link(&directory, "Python trust directory")?;
    fs::create_dir_all(&directory)?;
    restrict_private_directory(&directory)?;
    Ok(directory)
}

#[cfg(unix)]
fn restrict_private_directory(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() {
        bail!(
            "private Python state may not be a symlink: {}",
            path.display()
        );
    }
    if metadata.permissions().mode() & 0o077 != 0 {
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

#[cfg(not(unix))]
fn restrict_private_directory(path: &Path) -> Result<()> {
    reject_link(path, "private Python state")
}

fn open_regular_nofollow(path: &Path) -> io::Result<File> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        return OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        return OpenOptions::new()
            .read(true)
            .share_mode(1)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path);
    }
    #[cfg(not(any(unix, windows)))]
    {
        OpenOptions::new().read(true).open(path)
    }
}

fn open_directory_nofollow(path: &Path) -> io::Result<File> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        return OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_DIRECTORY)
            .open(path);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        return OpenOptions::new()
            .read(true)
            .share_mode(1)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path);
    }
    #[cfg(not(any(unix, windows)))]
    {
        OpenOptions::new().read(true).open(path)
    }
}

fn guarded_directory(path: &Path) -> Result<File> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata_is_reparse_point(&metadata)
    {
        bail!("Python identity root is linked or not a directory");
    }
    let guard = open_directory_nofollow(path)?;
    verify_directory_guard(path, &guard)?;
    Ok(guard)
}

fn verify_directory_guard(path: &Path, guard: &File) -> Result<()> {
    if lock_file_identity(guard)? != lock_path_identity(path)? {
        bail!("Python identity root was replaced during verification");
    }
    Ok(())
}

fn open_append_nofollow(path: &Path) -> io::Result<File> {
    reject_link(path, "append-only Python state")
        .map_err(|error| io::Error::new(io::ErrorKind::PermissionDenied, error.to_string()))?;
    #[cfg(unix)]
    let file = {
        use std::os::unix::fs::OpenOptionsExt;
        OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)?
    };
    #[cfg(windows)]
    let file = {
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        OpenOptions::new()
            .create(true)
            .append(true)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)?
    };
    #[cfg(not(any(unix, windows)))]
    let file = OpenOptions::new().create(true).append(true).open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata_is_reparse_point(&metadata) {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "append-only Python state is not a regular file",
        ));
    }
    if lock_file_identity(&file)? != lock_path_identity(path)? {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "append-only Python state was replaced during open",
        ));
    }
    Ok(file)
}

fn read_regular_nofollow(path: &Path) -> Result<Vec<u8>> {
    let before = fs::symlink_metadata(path)?;
    if !before.is_file() || before.file_type().is_symlink() || metadata_is_reparse_point(&before) {
        bail!("refusing non-regular Python trust input {}", path.display());
    }
    let mut file = open_regular_nofollow(path)?;
    let opened = file.metadata()?;
    if !opened.is_file() || metadata_is_reparse_point(&opened) {
        bail!(
            "Python trust input changed while opening {}",
            path.display()
        );
    }
    if lock_file_identity(&file)? != lock_path_identity(path)? {
        bail!(
            "Python trust input was replaced while opening {}",
            path.display()
        );
    }
    if file_link_count(&file)? != 1 {
        bail!("Python trust input is hardlinked: {}", path.display());
    }
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    if lock_file_identity(&file)? != lock_path_identity(path)? {
        bail!(
            "Python trust input was replaced while reading {}",
            path.display()
        );
    }
    Ok(bytes)
}

fn sha256_bytes(bytes: &[u8]) -> String {
    format!("sha256:{}", hex::encode(Sha256::digest(bytes)))
}

fn sha256_regular_file(path: &Path) -> Result<String> {
    Ok(sha256_bytes(&read_regular_nofollow(path)?))
}

fn file_link_count(file: &File) -> io::Result<u64> {
    #[cfg(unix)]
    {
        return Ok(file.metadata()?.nlink());
    }
    #[cfg(windows)]
    {
        let mut information = BY_HANDLE_FILE_INFORMATION::default();
        let result =
            unsafe { GetFileInformationByHandle(file.as_raw_handle() as HANDLE, &mut information) };
        if result == 0 {
            return Err(io::Error::last_os_error());
        }
        return Ok(u64::from(information.nNumberOfLinks));
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = file;
        Ok(1)
    }
}

fn portable_tree_path(path: &Path) -> Result<String> {
    let text = path
        .to_str()
        .ok_or_else(|| anyhow::anyhow!("Python tree contains a non-UTF-8 path"))?
        .replace('\\', "/");
    if text.is_empty()
        || text.starts_with('/')
        || text
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
    {
        bail!("Python tree contains an unsafe relative path {text:?}");
    }
    Ok(text)
}

fn tree_mode(metadata: &fs::Metadata) -> u32 {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.permissions().mode() & 0o777
    }
    #[cfg(not(unix))]
    {
        let _ = metadata;
        0
    }
}

fn collect_tree_entries(
    root: &Path,
    current: &Path,
    entries: &mut Vec<TreeEntryIdentity>,
    visited_directories: &mut HashSet<PathBuf>,
) -> Result<()> {
    let canonical_current = fs::canonicalize(current)?;
    let canonical_root = fs::canonicalize(root)?;
    if !canonical_current.starts_with(&canonical_root)
        || !visited_directories.insert(canonical_current)
    {
        bail!(
            "Python tree directory escaped or repeated at {}",
            current.display()
        );
    }
    let mut children = fs::read_dir(current)?.collect::<io::Result<Vec<_>>>()?;
    children.sort_by_key(fs::DirEntry::file_name);
    for child in children {
        if entries.len() >= MAX_TREE_ENTRIES {
            bail!("Python tree exceeds {MAX_TREE_ENTRIES} entries");
        }
        let path = child.path();
        let relative = path.strip_prefix(root)?;
        let relative_text = portable_tree_path(relative)?;
        let metadata = fs::symlink_metadata(&path)?;
        if metadata_is_reparse_point(&metadata) {
            bail!("Python tree contains a reparse point at {}", path.display());
        }
        if metadata.file_type().is_symlink() {
            let target = fs::read_link(&path)?;
            let canonical_target = fs::canonicalize(&path)?;
            if !canonical_target.starts_with(&canonical_root) {
                bail!("Python tree symlink escapes at {}", path.display());
            }
            entries.push(TreeEntryIdentity {
                path: relative_text,
                kind: "symlink".into(),
                size: 0,
                sha256: String::new(),
                link_target: target.to_string_lossy().replace('\\', "/"),
                mode: tree_mode(&metadata),
            });
        } else if metadata.is_dir() {
            if tree_mode(&metadata) & 0o022 != 0 {
                bail!(
                    "Python tree directory is group/world writable: {}",
                    path.display()
                );
            }
            entries.push(TreeEntryIdentity {
                path: relative_text,
                kind: "directory".into(),
                size: 0,
                sha256: String::new(),
                link_target: String::new(),
                mode: tree_mode(&metadata),
            });
            collect_tree_entries(root, &path, entries, visited_directories)?;
        } else if metadata.is_file() {
            let file = open_regular_nofollow(&path)?;
            if file_link_count(&file)? != 1 {
                bail!(
                    "Python tree contains a hardlinked file at {}",
                    path.display()
                );
            }
            if tree_mode(&metadata) & 0o022 != 0 {
                bail!(
                    "Python tree file is group/world writable: {}",
                    path.display()
                );
            }
            entries.push(TreeEntryIdentity {
                path: relative_text,
                kind: "file".into(),
                size: metadata.len(),
                sha256: sha256_regular_file(&path)?,
                link_target: String::new(),
                mode: tree_mode(&metadata),
            });
        } else {
            bail!(
                "Python tree contains an unsupported entry at {}",
                path.display()
            );
        }
    }
    Ok(())
}

fn tree_identity(root: &Path) -> Result<TreeIdentity> {
    ensure_real_directory(root, "Python identity tree")?;
    if !root.is_dir() {
        bail!("Python identity tree is missing: {}", root.display());
    }
    let root_metadata = fs::symlink_metadata(root)?;
    if tree_mode(&root_metadata) & 0o022 != 0 {
        bail!(
            "Python identity tree root is group/world writable: {}",
            root.display()
        );
    }
    let mut entries = vec![TreeEntryIdentity {
        path: ".".into(),
        kind: "directory".into(),
        size: 0,
        sha256: String::new(),
        link_target: String::new(),
        mode: tree_mode(&root_metadata),
    }];
    collect_tree_entries(root, root, &mut entries, &mut HashSet::new())?;
    let encoded = serde_json::to_vec(&entries)?;
    Ok(TreeIdentity {
        digest: sha256_bytes(&encoded),
        entries: entries.len(),
    })
}

fn stable_tree_identity(root: &Path) -> Result<TreeIdentity> {
    let guard = guarded_directory(root)?;
    let first = tree_identity(root)?;
    let second = tree_identity(root)?;
    verify_directory_guard(root, &guard)?;
    if first != second {
        bail!("Python tree changed while its identity was captured");
    }
    Ok(second)
}

fn secure_create_private(path: &Path) -> io::Result<File> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        return OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path);
    }
    #[cfg(not(unix))]
    {
        OpenOptions::new().write(true).create_new(true).open(path)
    }
}

fn atomic_write_private(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow::anyhow!("private path has no parent"))?;
    restrict_private_directory(parent)?;
    reject_link(path, "Python trust record")?;
    let temporary = parent.join(format!(
        ".{}.{}-{}.tmp",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("trust"),
        std::process::id(),
        unix_timestamp_nanos()
    ));
    let mut file = secure_create_private(&temporary)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    drop(file);
    fs::rename(&temporary, path)?;
    sync_directory(parent)?;
    Ok(())
}

fn load_or_create_trust_key(config: &AppConfig) -> Result<Vec<u8>> {
    let path = trust_directory(config)?.join(TRUST_KEY_FILE);
    if path_exists_or_reparse_point(&path) {
        return load_trust_key(&path);
    }
    let mut key = vec![0_u8; 32];
    rand::thread_rng().fill_bytes(&mut key);
    let mut file = secure_create_private(&path)?;
    file.write_all(&key)?;
    file.sync_all()?;
    Ok(key)
}

fn load_trust_key(path: &Path) -> Result<Vec<u8>> {
    let bytes = read_regular_nofollow(path)?;
    if bytes.len() != 32 {
        bail!("Python trust authority key is malformed");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if fs::symlink_metadata(path)?.permissions().mode() & 0o077 != 0 {
            bail!("Python trust authority key permissions are unsafe");
        }
    }
    Ok(bytes)
}

fn sign_payload(key: &[u8], payload: &InstallTrustPayload) -> Result<String> {
    let encoded = serde_json::to_vec(payload)?;
    let mut mac = HmacSha256::new_from_slice(key)
        .map_err(|_| anyhow::anyhow!("invalid Python trust authority key"))?;
    mac.update(&encoded);
    Ok(hex::encode(mac.finalize().into_bytes()))
}

fn write_signed_trust(path: &Path, key: &[u8], payload: InstallTrustPayload) -> Result<()> {
    let record = SignedInstallTrust {
        hmac_sha256: sign_payload(key, &payload)?,
        payload,
    };
    atomic_write_private(path, &serde_json::to_vec(&record)?)
}

fn read_signed_trust(path: &Path, key: &[u8]) -> Result<InstallTrustPayload> {
    let record: SignedInstallTrust = serde_json::from_slice(&read_regular_nofollow(path)?)?;
    if record.payload.schema != TRUST_SCHEMA {
        bail!("Python trust record schema is invalid");
    }
    let expected = sign_payload(key, &record.payload)?;
    let expected = hex::decode(expected)?;
    let supplied = hex::decode(&record.hmac_sha256)?;
    if expected.len() != supplied.len()
        || !expected
            .iter()
            .zip(supplied.iter())
            .fold(0_u8, |difference, (left, right)| {
                difference | (left ^ right)
            })
            .eq(&0)
    {
        bail!("Python trust record authentication failed");
    }
    Ok(record.payload)
}

fn runtime_trust_path(config: &AppConfig) -> Result<PathBuf> {
    Ok(trust_directory(config)?.join(RUNTIME_TRUST_FILE))
}

fn venv_trust_path(config: &AppConfig) -> Result<PathBuf> {
    Ok(trust_directory(config)?.join(VENV_TRUST_FILE))
}

fn runtime_install_identity(payload: &InstallTrustPayload) -> Result<String> {
    Ok(sha256_bytes(&serde_json::to_vec(payload)?))
}

fn seal_runtime(config: &AppConfig, root: &Path, uv: &Path, key: &[u8]) -> Result<RuntimeTrust> {
    let root_guard = guarded_directory(root)?;
    let tree = stable_tree_identity(root)?;
    verify_directory_guard(root, &root_guard)?;
    let mut payload = InstallTrustPayload {
        schema: TRUST_SCHEMA.into(),
        kind: "cpython-runtime".into(),
        python_version: PYTHON_VERSION.into(),
        tree: tree.clone(),
        runtime_install_identity: String::new(),
        requirements_sha256: String::new(),
        uv_sha256: sha256_regular_file(uv)?,
    };
    payload.runtime_install_identity = runtime_install_identity(&payload)?;
    write_signed_trust(&runtime_trust_path(config)?, key, payload.clone())?;
    Ok(RuntimeTrust {
        install_identity: payload.runtime_install_identity,
        root_guard,
    })
}

fn verify_runtime_trust(
    config: &AppConfig,
    root: &Path,
    uv: &Path,
    key: &[u8],
) -> Result<RuntimeTrust> {
    let payload = read_signed_trust(&runtime_trust_path(config)?, key)?;
    if payload.kind != "cpython-runtime"
        || payload.python_version != PYTHON_VERSION
        || !payload.requirements_sha256.is_empty()
        || payload.uv_sha256 != sha256_regular_file(uv)?
        || payload.runtime_install_identity
            != runtime_install_identity(&InstallTrustPayload {
                runtime_install_identity: String::new(),
                ..payload.clone()
            })?
    {
        bail!("Python runtime trust binding is invalid");
    }
    let root_guard = guarded_directory(root)?;
    let tree = stable_tree_identity(root)?;
    verify_directory_guard(root, &root_guard)?;
    if tree != payload.tree {
        bail!("Python runtime tree identity changed");
    }
    Ok(RuntimeTrust {
        install_identity: payload.runtime_install_identity,
        root_guard,
    })
}

fn locked_requirements_identity(config: &AppConfig) -> Result<String> {
    let source = config.requirements_txt();
    let bytes = read_regular_nofollow(&source)?;
    let snapshot = locked_requirements_path(config)?;
    atomic_write_private(&snapshot, &bytes)?;
    validate_hashed_requirements(&snapshot)?;
    sha256_regular_file(&snapshot)
}

fn locked_requirements_path(config: &AppConfig) -> Result<PathBuf> {
    Ok(trust_directory(config)?.join(REQUIREMENTS_SNAPSHOT_FILE))
}

fn venv_payload(
    tree: TreeIdentity,
    runtime: &RuntimeTrust,
    requirements_sha256: &str,
    uv_sha256: String,
) -> InstallTrustPayload {
    InstallTrustPayload {
        schema: TRUST_SCHEMA.into(),
        kind: "python-venv".into(),
        python_version: PYTHON_VERSION.into(),
        tree,
        runtime_install_identity: runtime.install_identity.clone(),
        requirements_sha256: requirements_sha256.into(),
        uv_sha256,
    }
}

fn seal_venv(
    config: &AppConfig,
    uv: &Path,
    key: &[u8],
    runtime: &RuntimeTrust,
    requirements_sha256: &str,
) -> Result<()> {
    let payload = venv_payload(
        stable_tree_identity(&config.venv_dir)?,
        runtime,
        requirements_sha256,
        sha256_regular_file(uv)?,
    );
    write_signed_trust(&venv_trust_path(config)?, key, payload)
}

fn verify_venv_trust(
    config: &AppConfig,
    uv: &Path,
    key: &[u8],
    runtime: &RuntimeTrust,
    requirements_sha256: &str,
) -> Result<File> {
    let payload = read_signed_trust(&venv_trust_path(config)?, key)?;
    if payload.kind != "python-venv"
        || payload.python_version != PYTHON_VERSION
        || payload.runtime_install_identity != runtime.install_identity
        || payload.requirements_sha256 != requirements_sha256
        || payload.uv_sha256 != sha256_regular_file(uv)?
    {
        bail!("Python venv trust binding is invalid");
    }
    let root_guard = guarded_directory(&config.venv_dir)?;
    if stable_tree_identity(&config.venv_dir)? != payload.tree {
        bail!("Python venv tree identity changed");
    }
    verify_directory_guard(&config.venv_dir, &root_guard)?;
    Ok(root_guard)
}

#[cfg(unix)]
fn set_owner_writable(path: &Path, writable: bool) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() {
        return Ok(());
    }
    let mode = metadata.permissions().mode();
    let updated = if writable {
        mode | if metadata.is_dir() { 0o700 } else { 0o600 }
    } else {
        mode & !0o222
    };
    fs::set_permissions(path, fs::Permissions::from_mode(updated))
}

#[cfg(not(unix))]
fn set_owner_writable(path: &Path, writable: bool) -> io::Result<()> {
    let mut permissions = fs::symlink_metadata(path)?.permissions();
    permissions.set_readonly(!writable);
    fs::set_permissions(path, permissions)
}

fn set_tree_writable_state(root: &Path, writable: bool) -> Result<()> {
    let metadata = fs::symlink_metadata(root)?;
    if metadata.file_type().is_symlink() || metadata_is_reparse_point(&metadata) {
        bail!("refusing to change permissions through a linked Python tree");
    }
    if metadata.is_dir() {
        for entry in fs::read_dir(root)? {
            let path = entry?.path();
            let child = fs::symlink_metadata(&path)?;
            if child.file_type().is_symlink() {
                continue;
            }
            if metadata_is_reparse_point(&child) {
                bail!("refusing reparse point in Python tree permissions");
            }
            if child.is_dir() {
                set_tree_writable_state(&path, writable)?;
            } else {
                set_owner_writable(&path, writable)?;
            }
        }
    }
    set_owner_writable(root, writable)?;
    Ok(())
}

fn remove_trust_record(path: &Path) -> Result<()> {
    if !path_exists_or_reparse_point(path) {
        return Ok(());
    }
    reject_link(path, "Python trust record")?;
    fs::remove_file(path)?;
    Ok(())
}

fn quarantine_owned_tree(config: &AppConfig, path: &Path, label: &str) -> Result<()> {
    if !path_exists_or_reparse_point(path) {
        return Ok(());
    }
    let state_root = provision_state_root(config)?;
    if path.parent() != Some(state_root.as_path())
        && path
            .parent()
            .and_then(|parent| fs::canonicalize(parent).ok())
            .as_deref()
            != Some(state_root.as_path())
    {
        bail!("refusing to quarantine {label} outside Python state root");
    }
    reject_link(path, label)?;
    let quarantine = state_root.join(format!(
        ".quarantine-{}-{}-{}",
        label.replace(' ', "-"),
        std::process::id(),
        unix_timestamp_nanos()
    ));
    fs::rename(path, &quarantine)
        .with_context(|| format!("failed to quarantine {label} at {}", path.display()))?;
    set_tree_writable_state(&quarantine, true)?;
    remove_path_or_reparse_point(&quarantine)?;
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct LockFileIdentity {
    #[cfg(unix)]
    device: u64,
    #[cfg(unix)]
    inode: u64,
    #[cfg(windows)]
    volume_serial: u32,
    #[cfg(windows)]
    file_index: u64,
}

fn lock_file_identity(file: &File) -> io::Result<LockFileIdentity> {
    #[cfg(unix)]
    {
        let metadata = file.metadata()?;
        return Ok(LockFileIdentity {
            device: metadata.dev(),
            inode: metadata.ino(),
        });
    }

    #[cfg(windows)]
    {
        let mut information = BY_HANDLE_FILE_INFORMATION::default();
        let result =
            unsafe { GetFileInformationByHandle(file.as_raw_handle() as HANDLE, &mut information) };
        if result == 0 {
            return Err(io::Error::last_os_error());
        }
        return Ok(LockFileIdentity {
            volume_serial: information.dwVolumeSerialNumber,
            file_index: (u64::from(information.nFileIndexHigh) << 32)
                | u64::from(information.nFileIndexLow),
        });
    }

    #[cfg(not(any(unix, windows)))]
    {
        let _ = file;
        Ok(LockFileIdentity {})
    }
}

fn lock_path_identity(path: &Path) -> io::Result<LockFileIdentity> {
    #[cfg(unix)]
    {
        let metadata = fs::metadata(path)?;
        return Ok(LockFileIdentity {
            device: metadata.dev(),
            inode: metadata.ino(),
        });
    }

    #[cfg(windows)]
    {
        return lock_file_identity(&OpenOptions::new().read(true).open(path)?);
    }

    #[cfg(not(any(unix, windows)))]
    {
        Ok(LockFileIdentity {})
    }
}

struct ProvisionLock {
    path: PathBuf,
    file: File,
    released: bool,
}

impl ProvisionLock {
    fn release(mut self) -> Result<()> {
        self.release_inner()?;
        self.released = true;
        Ok(())
    }

    fn release_inner(&self) -> Result<()> {
        unlock_exclusive(&self.file).map_err(|error| {
            typed_error(
                PythonProvisioningCode::CleanupFailed,
                format!(
                    "cannot release Python provisioning lock {}: {error}; retry the launcher",
                    self.path.display()
                ),
            )
        })
    }
}

impl Drop for ProvisionLock {
    fn drop(&mut self) {
        if self.released {
            return;
        }
        if let Err(error) = self.release_inner() {
            warn!("Python provisioning lock cleanup failed: {error}");
        }
    }
}

fn acquire_provision_lock(path: &Path, wait: Duration) -> Result<ProvisionLock> {
    let parent = path.parent().ok_or_else(|| {
        typed_error(
            PythonProvisioningCode::PathTampered,
            format!("Python provisioning lock {} has no parent", path.display()),
        )
    })?;
    ensure_real_directory(parent, "Python provisioning lock parent")?;
    fs::create_dir_all(parent).map_err(|error| {
        typed_error(
            PythonProvisioningCode::DiagnosticsUnavailable,
            format!(
                "cannot create Python provisioning lock parent {}: {error}; retry the launcher",
                parent.display()
            ),
        )
    })?;

    let started = Instant::now();
    let token = format!(
        "pid={}\nstarted_nanos={}\n",
        std::process::id(),
        unix_timestamp_nanos()
    );

    loop {
        reject_link(path, "Python provisioning lock")?;
        let (mut file, created) = match OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(path)
        {
            Ok(file) => (file, true),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                match OpenOptions::new().read(true).write(true).open(path) {
                    Ok(file) => (file, false),
                    Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                    Err(error) => {
                        return Err(typed_error(
                            PythonProvisioningCode::DiagnosticsUnavailable,
                            format!(
                                "cannot inspect Python provisioning lock {}: {error}; retry the launcher",
                                path.display()
                            ),
                        ));
                    }
                }
            }
            Err(error) => {
                return Err(typed_error(
                    PythonProvisioningCode::DiagnosticsUnavailable,
                    format!(
                        "cannot acquire Python provisioning lock {}: {error}; retry the launcher",
                        path.display()
                    ),
                ));
            }
        };

        match try_lock_exclusive(&file) {
            Ok(true) => {}
            Ok(false) => {
                drop(file);
                if started.elapsed() >= wait {
                    return Err(typed_error(
                        PythonProvisioningCode::LockBusy,
                        format!(
                            "another Launcher is provisioning Python at {}; wait for it to finish, then Retry",
                            path.display()
                        ),
                    ));
                }
                thread::sleep(CHILD_POLL_INTERVAL);
                continue;
            }
            Err(error) => {
                return Err(typed_error(
                    PythonProvisioningCode::DiagnosticsUnavailable,
                    format!(
                        "cannot lock Python provisioning file {}: {error}; retry the launcher",
                        path.display()
                    ),
                ));
            }
        }

        let observed = read_lock_contents(&file).map_err(|error| {
            typed_error(
                PythonProvisioningCode::DiagnosticsUnavailable,
                format!(
                    "cannot read Python provisioning lock {}: {error}; retry the launcher",
                    path.display()
                ),
            )
        })?;
        if !created
            && !lock_reclaim_identity_matches(path, &file, &observed).map_err(|error| {
                typed_error(
                    PythonProvisioningCode::DiagnosticsUnavailable,
                    format!(
                    "cannot verify stale Python provisioning lock {}: {error}; retry the launcher",
                    path.display()
                ),
                )
            })?
        {
            unlock_exclusive(&file).map_err(|error| {
                typed_error(
                    PythonProvisioningCode::CleanupFailed,
                    format!(
                        "cannot release replaced Python provisioning lock {}: {error}; retry the launcher",
                        path.display()
                    ),
                )
            })?;
            drop(file);
            continue;
        }

        write_lock_contents(&mut file, &token).map_err(|error| {
            typed_error(
                PythonProvisioningCode::CleanupFailed,
                format!(
                    "cannot initialize Python provisioning lock {}: {error}; retry the launcher",
                    path.display()
                ),
            )
        })?;
        return Ok(ProvisionLock {
            path: path.to_path_buf(),
            file,
            released: false,
        });
    }
}

fn read_lock_contents(file: &File) -> io::Result<String> {
    let mut reader = file.try_clone()?;
    reader.seek(SeekFrom::Start(0))?;
    let mut contents = String::new();
    reader.read_to_string(&mut contents)?;
    Ok(contents)
}

fn write_lock_contents(file: &mut File, contents: &str) -> io::Result<()> {
    file.set_len(0)?;
    file.seek(SeekFrom::Start(0))?;
    file.write_all(contents.as_bytes())?;
    file.sync_all()
}

fn lock_reclaim_identity_matches(path: &Path, file: &File, observed: &str) -> io::Result<bool> {
    let path_metadata = fs::symlink_metadata(path)?;
    if path_metadata.file_type().is_symlink() || metadata_is_reparse_point(&path_metadata) {
        return Ok(false);
    }
    if lock_file_identity(file)? != lock_path_identity(path)? {
        return Ok(false);
    }
    Ok(fs::read_to_string(path)? == observed)
}

fn try_lock_exclusive(file: &File) -> io::Result<bool> {
    #[cfg(unix)]
    {
        let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        if result == 0 {
            return Ok(true);
        }
        let error = io::Error::last_os_error();
        if matches!(
            error.raw_os_error(),
            Some(code) if code == libc::EAGAIN || code == libc::EWOULDBLOCK
        ) {
            return Ok(false);
        }
        return Err(error);
    }

    #[cfg(windows)]
    {
        let mut overlapped = OVERLAPPED::default();
        let result = unsafe {
            LockFileEx(
                file.as_raw_handle() as HANDLE,
                LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                0,
                1,
                0,
                &mut overlapped,
            )
        };
        if result != 0 {
            return Ok(true);
        }
        let error = unsafe { GetLastError() };
        if error == ERROR_LOCK_VIOLATION {
            return Ok(false);
        }
        return Err(io::Error::from_raw_os_error(error as i32));
    }

    #[cfg(not(any(unix, windows)))]
    {
        let _ = file;
        Ok(true)
    }
}

fn unlock_exclusive(file: &File) -> io::Result<()> {
    #[cfg(unix)]
    {
        let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) };
        if result == 0 {
            return Ok(());
        }
        return Err(io::Error::last_os_error());
    }

    #[cfg(windows)]
    {
        let mut overlapped = OVERLAPPED::default();
        let result =
            unsafe { UnlockFileEx(file.as_raw_handle() as HANDLE, 0, 1, 0, &mut overlapped) };
        if result != 0 {
            return Ok(());
        }
        return Err(io::Error::from_raw_os_error(
            unsafe { GetLastError() } as i32
        ));
    }

    #[cfg(not(any(unix, windows)))]
    {
        let _ = file;
        Ok(())
    }
}

fn unix_timestamp_nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_nanos())
}

fn reject_link(path: &Path, label: &str) -> Result<()> {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return Ok(());
    };
    if metadata.file_type().is_symlink() || metadata_is_reparse_point(&metadata) {
        return Err(typed_error(
            PythonProvisioningCode::PathTampered,
            format!(
                "{label} {} is a symlink or reparse point; remove it manually and Retry",
                path.display()
            ),
        ));
    }
    Ok(())
}

fn ensure_real_directory(path: &Path, label: &str) -> Result<()> {
    reject_link(path, label)?;
    if path_exists_or_reparse_point(path) && !path.is_dir() {
        return Err(typed_error(
            PythonProvisioningCode::PathTampered,
            format!(
                "{label} {} is not a directory; remove it and Retry",
                path.display()
            ),
        ));
    }
    Ok(())
}

#[derive(Debug)]
#[cfg_attr(not(windows), allow(dead_code))]
struct CapturedOutput {
    text: String,
    truncated: bool,
}

#[cfg(windows)]
fn read_capped(mut reader: impl Read) -> io::Result<CapturedOutput> {
    let mut captured = Vec::new();
    let mut buffer = [0_u8; 8192];
    let mut truncated = false;
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        let remaining = MAX_CAPTURED_COMMAND_OUTPUT.saturating_sub(captured.len());
        if remaining > 0 {
            captured.extend_from_slice(&buffer[..count.min(remaining)]);
        }
        if count > remaining {
            truncated = true;
        }
    }
    Ok(CapturedOutput {
        text: String::from_utf8_lossy(&captured).into_owned(),
        truncated,
    })
}

#[cfg(windows)]
struct WindowsJob {
    handle: HANDLE,
}

#[cfg(windows)]
impl WindowsJob {
    fn new() -> io::Result<Self> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(io::Error::from_raw_os_error(
                unsafe { GetLastError() } as i32
            ));
        }

        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const std::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if configured == 0 {
            let error = io::Error::from_raw_os_error(unsafe { GetLastError() } as i32);
            unsafe {
                CloseHandle(handle);
            }
            return Err(error);
        }
        Ok(Self { handle })
    }

    fn assign(&self, child: &Child) -> io::Result<()> {
        let assigned =
            unsafe { AssignProcessToJobObject(self.handle, child.as_raw_handle() as HANDLE) };
        if assigned == 0 {
            return Err(io::Error::from_raw_os_error(
                unsafe { GetLastError() } as i32
            ));
        }
        Ok(())
    }

    fn resume(&self, child: &Child) -> io::Result<()> {
        resume_suspended_child(child)
    }

    fn terminate(&self) -> io::Result<()> {
        let terminated = unsafe { TerminateJobObject(self.handle, 1) };
        if terminated == 0 {
            return Err(io::Error::from_raw_os_error(
                unsafe { GetLastError() } as i32
            ));
        }
        Ok(())
    }
}

#[cfg(windows)]
fn resume_suspended_child(child: &Child) -> io::Result<()> {
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;

    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    let result = (|| {
        let mut entry = THREADENTRY32 {
            dwSize: std::mem::size_of::<THREADENTRY32>() as u32,
            ..THREADENTRY32::default()
        };
        let mut available = unsafe { Thread32First(snapshot, &mut entry) } != 0;
        while available {
            if entry.th32OwnerProcessID == child.id() {
                let thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
                if thread.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let resumed = unsafe { ResumeThread(thread) };
                unsafe { CloseHandle(thread) };
                if resumed == u32::MAX {
                    return Err(io::Error::last_os_error());
                }
                return Ok(());
            }
            available = unsafe { Thread32Next(snapshot, &mut entry) } != 0;
        }
        Err(io::Error::new(
            io::ErrorKind::NotFound,
            "suspended child thread was not found",
        ))
    })();
    unsafe { CloseHandle(snapshot) };
    result
}

#[cfg(windows)]
impl Drop for WindowsJob {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.handle);
        }
    }
}

#[cfg(windows)]
fn append_command_log(
    log_path: &Path,
    step: &str,
    command_summary: &str,
    status: Option<ExitStatus>,
    stdout: &CapturedOutput,
    stderr: &CapturedOutput,
) -> Result<()> {
    let mut file = open_append_nofollow(log_path).map_err(|error| {
        typed_error(
            PythonProvisioningCode::DiagnosticsUnavailable,
            format!(
                "cannot append Python provisioning diagnostics at {}: {error}; Retry",
                log_path.display()
            ),
        )
    })?;
    writeln!(file, "===== {step} =====").map_err(|error| {
        typed_error(
            PythonProvisioningCode::DiagnosticsUnavailable,
            format!("cannot write Python provisioning diagnostics: {error}; Retry"),
        )
    })?;
    writeln!(file, "command={command_summary}").map_err(|error| {
        typed_error(
            PythonProvisioningCode::DiagnosticsUnavailable,
            format!("cannot write Python provisioning diagnostics: {error}; Retry"),
        )
    })?;
    writeln!(file, "status={status:?}").map_err(|error| {
        typed_error(
            PythonProvisioningCode::DiagnosticsUnavailable,
            format!("cannot write Python provisioning diagnostics: {error}; Retry"),
        )
    })?;
    if stdout.truncated || stderr.truncated {
        writeln!(file, "output_truncated=true").map_err(|error| {
            typed_error(
                PythonProvisioningCode::DiagnosticsUnavailable,
                format!("cannot write Python provisioning diagnostics: {error}; Retry"),
            )
        })?;
    }
    if !stdout.text.is_empty() {
        writeln!(file, "stdout:\n{}", stdout.text).map_err(|error| {
            typed_error(
                PythonProvisioningCode::DiagnosticsUnavailable,
                format!("cannot write Python provisioning diagnostics: {error}; Retry"),
            )
        })?;
    }
    if !stderr.text.is_empty() {
        writeln!(file, "stderr:\n{}", stderr.text).map_err(|error| {
            typed_error(
                PythonProvisioningCode::DiagnosticsUnavailable,
                format!("cannot write Python provisioning diagnostics: {error}; Retry"),
            )
        })?;
    }
    file.flush().map_err(|error| {
        typed_error(
            PythonProvisioningCode::DiagnosticsUnavailable,
            format!("cannot flush Python provisioning diagnostics: {error}; Retry"),
        )
    })?;
    Ok(())
}

#[cfg(windows)]
fn diagnostic_tail(output: &CapturedOutput) -> String {
    output
        .text
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .next_back()
        .unwrap_or("no diagnostic output")
        .chars()
        .take(240)
        .collect()
}

#[cfg(windows)]
fn is_network_failure(output: &CapturedOutput) -> bool {
    let text = output.text.to_ascii_lowercase();
    [
        "network",
        "offline",
        "download",
        "connect",
        "dns",
        "timed out",
        "timeout",
        "tls",
        "proxy",
    ]
    .iter()
    .any(|needle| text.contains(needle))
}

#[cfg(not(windows))]
fn run_command_until<F>(
    _command: Command,
    step: &str,
    _initial_progress: &str,
    _long_progress: &str,
    _options: ProvisionOptions,
    log_path: &Path,
    _progress: &F,
) -> Result<CapturedOutput>
where
    F: Fn(&str) + ?Sized,
{
    let platform = if cfg!(target_os = "macos") {
        "macOS packaged launches never permit external provisioning"
    } else {
        "Linux external provisioning requires a verified delegated cgroup-v2/systemd service"
    };
    Err(typed_error(
        PythonProvisioningCode::ContainmentUnavailable,
        format!(
            "{step} was rejected before spawn: {platform}. Diagnostics: {}",
            log_path.display()
        ),
    ))
}

#[cfg(windows)]
fn run_command_until<F>(
    mut command: Command,
    step: &str,
    initial_progress: &str,
    long_progress: &str,
    options: ProvisionOptions,
    log_path: &Path,
    progress: &F,
) -> Result<CapturedOutput>
where
    F: Fn(&str) + ?Sized,
{
    use std::os::windows::{fs::OpenOptionsExt, process::CommandExt};

    progress(initial_progress);
    let diagnostic_path = log_path.with_file_name(format!(
        ".python-command-{}-{}.log",
        std::process::id(),
        unix_timestamp_nanos()
    ));
    let diagnostic = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .share_mode(1)
        .custom_flags(0x0020_0000)
        .open(&diagnostic_path)
        .map_err(|error| {
            typed_error(
                PythonProvisioningCode::DiagnosticsUnavailable,
                format!("{step} could not create a private diagnostic log: {error}"),
            )
        })?;
    let diagnostic_stderr = diagnostic.try_clone()?;
    let command_summary = format!("{command:?}");
    let job = WindowsJob::new().map_err(|error| {
        typed_error(
            PythonProvisioningCode::ContainmentUnavailable,
            format!("{step} could not create a Windows Job Object: {error}"),
        )
    })?;
    command
        .creation_flags(CREATE_SUSPENDED)
        .stdout(Stdio::from(diagnostic.try_clone()?))
        .stderr(Stdio::from(diagnostic_stderr));
    let mut child = command.spawn().map_err(|error| {
        typed_error(
            PythonProvisioningCode::ChildSpawn,
            format!("{step} could not start suspended: {error}"),
        )
    })?;
    if let Err(error) = job.assign(&child).and_then(|()| job.resume(&child)) {
        let _ = job.terminate();
        let _ = child.kill();
        let _ = child.wait();
        return Err(typed_error(
            PythonProvisioningCode::ContainmentUnavailable,
            format!("{step} could not enter the Windows Job Object: {error}"),
        ));
    }

    let started = Instant::now();
    let mut long_notice_sent = false;
    let status = loop {
        if diagnostic.metadata()?.len() > MAX_CAPTURED_COMMAND_OUTPUT as u64 {
            job.terminate()?;
            let _ = child.wait();
            return Err(typed_error(
                PythonProvisioningCode::DiagnosticsUnavailable,
                format!("{step} exceeded the bounded diagnostic-log size"),
            ));
        }
        if let Some(status) = child.try_wait()? {
            break Some(status);
        }
        if !long_notice_sent && started.elapsed() >= options.long_operation_notice {
            progress(long_progress);
            long_notice_sent = true;
        }
        if Instant::now() >= options.deadline {
            job.terminate()?;
            let _ = child.wait();
            break None;
        }
        thread::sleep(CHILD_POLL_INTERVAL);
    };
    let mut diagnostic_reader = diagnostic.try_clone()?;
    diagnostic_reader.seek(SeekFrom::Start(0))?;
    let output = read_capped(&mut diagnostic_reader)?;
    append_command_log(
        log_path,
        step,
        &command_summary,
        status,
        &output,
        &CapturedOutput {
            text: String::new(),
            truncated: output.truncated,
        },
    )?;
    drop(diagnostic);
    let _ = fs::remove_file(&diagnostic_path);
    let Some(status) = status else {
        return Err(typed_error(
            PythonProvisioningCode::Timeout,
            format!(
                "{step} exceeded the bounded {} second deadline",
                options.timeout_secs
            ),
        ));
    };
    if !status.success() {
        let code = if is_network_failure(&output) {
            PythonProvisioningCode::NetworkUnavailable
        } else {
            PythonProvisioningCode::ChildFailed
        };
        return Err(typed_error(
            code,
            format!("{step} exited with {status}: {}", diagnostic_tail(&output)),
        ));
    }
    Ok(output)
}

fn uv_command(uv: &Path, cache_dir: &Path) -> Command {
    let mut command = process_utils::command(uv);
    command
        .arg("--no-config")
        .env("UV_CACHE_DIR", cache_dir)
        .env("UV_NO_MANAGED_PYTHON", "0");
    command
}

// ---------------------------------------------------------------------------
// Step 2 — Python via `uv python install`
// ---------------------------------------------------------------------------

fn ensure_python<F>(
    config: &AppConfig,
    uv: &Path,
    cache_dir: &Path,
    log_path: &Path,
    trust_key: &[u8],
    options: ProvisionOptions,
    progress: &F,
) -> Result<RuntimeTrust>
where
    F: Fn(&str) + ?Sized,
{
    let python_bin = config.python_bin();
    let tmp_dir = config.python_dir.with_file_name("_python_tmp");
    reject_link(&config.python_dir, "Python runtime directory")?;
    reject_link(&tmp_dir, "Python runtime staging directory")?;

    if path_exists_or_reparse_point(&config.python_dir)
        && fs::symlink_metadata(config.python_dir.join(RUNTIME_MARKER)).is_ok()
    {
        match verify_runtime_trust(config, &config.python_dir, uv, trust_key) {
            Ok(runtime) => {
                verify_python_install(
                    &config.python_dir,
                    &runtime.root_guard,
                    true,
                    options,
                    log_path,
                    progress,
                )?;
                cleanup_staging_root(&tmp_dir)?;
                info!(
                    "Python already present and cryptographically verified at {}",
                    python_bin.display()
                );
                return Ok(runtime);
            }
            Err(error) => {
                warn!(
                    "Quarantining untrusted existing Python runtime at {}: {error:#}",
                    config.python_dir.display()
                );
                quarantine_owned_tree(config, &config.python_dir, "python-runtime")?;
                remove_trust_record(&runtime_trust_path(config)?)?;
            }
        }
    }

    if path_exists_or_reparse_point(&config.python_dir) {
        info!(
            "Removing incomplete Python directory before verified replacement: {}",
            config.python_dir.display()
        );
        quarantine_owned_tree(config, &config.python_dir, "python-runtime")?;
        remove_trust_record(&runtime_trust_path(config)?)?;
    }

    cleanup_staging_root(&tmp_dir)?;
    fs::create_dir_all(&tmp_dir).map_err(|error| {
        typed_error(
            PythonProvisioningCode::CleanupFailed,
            format!(
                "cannot create Python staging directory {}: {error}; Retry",
                tmp_dir.display()
            ),
        )
    })?;
    let attempt_dir = tmp_dir.join(format!(
        "attempt-{}-{}",
        std::process::id(),
        unix_timestamp_nanos()
    ));
    fs::create_dir(&attempt_dir).map_err(|error| {
        typed_error(
            PythonProvisioningCode::CleanupFailed,
            format!(
                "cannot create Python install attempt {}: {error}; Retry",
                attempt_dir.display()
            ),
        )
    })?;

    let result = (|| {
        let mut command = uv_command(uv, cache_dir);
        command.args([
            "python",
            "install",
            PYTHON_VERSION,
            "--install-dir",
            &attempt_dir.to_string_lossy(),
        ]);
        run_command_until(
            command,
            "uv python install",
            "Downloading Python 3.13.13 (the first start may take several minutes)...",
            "Python download is still running; keep Tobkiri Launcher open while it finishes...",
            options,
            log_path,
            progress,
        )?;
        let extracted = find_installed_python_dir(&attempt_dir)?;
        write_runtime_marker(&extracted)?;
        sync_directory(&extracted)?;
        set_tree_writable_state(&extracted, false)?;
        let _sealed = seal_runtime(config, &extracted, uv, trust_key)?;
        let staged = verify_runtime_trust(config, &extracted, uv, trust_key)?;
        verify_python_install(
            &extracted,
            &staged.root_guard,
            true,
            options,
            log_path,
            progress,
        )?;
        set_tree_writable_state(&extracted, true)?;

        reject_link(&config.python_dir, "Python runtime directory")?;
        if path_exists_or_reparse_point(&config.python_dir) {
            return Err(typed_error(
                PythonProvisioningCode::PathTampered,
                format!(
                    "Python runtime destination {} appeared during provisioning; close other Launcher processes and Retry",
                    config.python_dir.display()
                ),
            ));
        }
        fs::rename(&extracted, &config.python_dir).map_err(|error| {
            typed_error(
                PythonProvisioningCode::CleanupFailed,
                format!(
                    "cannot atomically promote verified Python runtime to {}: {error}; Retry",
                    config.python_dir.display()
                ),
            )
        })?;
        sync_directory(config.python_dir.parent().unwrap_or(Path::new(".")))?;
        set_tree_writable_state(&config.python_dir, false)?;
        seal_runtime(config, &config.python_dir, uv, trust_key)?;
        let runtime = verify_runtime_trust(config, &config.python_dir, uv, trust_key)?;
        verify_python_install(
            &config.python_dir,
            &runtime.root_guard,
            true,
            options,
            log_path,
            progress,
        )?;
        info!(
            "Python installed and verified at {}",
            config.python_dir.display()
        );
        Ok(runtime)
    })();

    let cleanup_result = cleanup_staging_root(&tmp_dir);
    match (result, cleanup_result) {
        (Ok(runtime), Ok(())) => Ok(runtime),
        (Err(error), Ok(())) => Err(error),
        (Ok(_), Err(error)) => Err(error),
        (Err(error), Err(cleanup_error)) => Err(anyhow::anyhow!(
            "{error}; Python staging cleanup also failed: {cleanup_error}"
        )),
    }
}

fn cleanup_staging_root(path: &Path) -> Result<()> {
    if !path_exists_or_reparse_point(path) {
        return Ok(());
    }
    reject_link(path, "Python runtime staging directory")?;
    set_tree_writable_state(path, true)?;
    remove_path_or_reparse_point(path).map_err(|error| {
        typed_error(
            PythonProvisioningCode::CleanupFailed,
            format!(
                "cannot clean Python staging directory {}: {error}; Retry",
                path.display()
            ),
        )
    })?;
    if path_exists_or_reparse_point(path) {
        return Err(typed_error(
            PythonProvisioningCode::CleanupFailed,
            format!(
                "Python staging directory {} remains after cleanup; Retry",
                path.display()
            ),
        ));
    }
    Ok(())
}

fn write_runtime_marker(root: &Path) -> Result<()> {
    let marker = root.join(RUNTIME_MARKER);
    reject_link(&marker, "Python runtime marker")?;
    if path_exists_or_reparse_point(&marker) {
        return Err(typed_error(
            PythonProvisioningCode::PathTampered,
            format!(
                "verified Python runtime already contains an unexpected marker at {}; Retry",
                marker.display()
            ),
        ));
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&marker)
        .map_err(|error| {
            typed_error(
                PythonProvisioningCode::CleanupFailed,
                format!(
                    "cannot write Python runtime marker {}: {error}; Retry",
                    marker.display()
                ),
            )
        })?;
    file.write_all(RUNTIME_MARKER_CONTENT.as_bytes())
        .and_then(|_| file.sync_all())
        .map_err(|error| {
            typed_error(
                PythonProvisioningCode::CleanupFailed,
                format!(
                    "cannot persist Python runtime marker {}: {error}; Retry",
                    marker.display()
                ),
            )
        })?;
    Ok(())
}

fn sync_directory(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        File::open(path)
            .and_then(|file| file.sync_all())
            .map_err(|error| {
                typed_error(
                    PythonProvisioningCode::CleanupFailed,
                    format!(
                        "cannot persist Python runtime directory {}: {error}; Retry",
                        path.display()
                    ),
                )
            })?;
    }
    #[cfg(not(unix))]
    {
        let _ = path;
    }
    Ok(())
}

fn verify_python_install<F>(
    root: &Path,
    root_guard: &File,
    require_marker: bool,
    options: ProvisionOptions,
    log_path: &Path,
    progress: &F,
) -> Result<()>
where
    F: Fn(&str) + ?Sized,
{
    verify_directory_guard(root, root_guard)?;
    ensure_real_directory(root, "Python runtime")?;
    let marker = root.join(RUNTIME_MARKER);
    reject_link(&marker, "Python runtime marker")?;
    if require_marker {
        let content = read_regular_nofollow(&marker).map_err(|error| {
            typed_error(
                PythonProvisioningCode::InvalidArtifact,
                format!(
                    "Python runtime marker {} is unreadable ({error}); remove the incomplete runtime and Retry",
                    marker.display()
                ),
            )
        })?;
        if content != RUNTIME_MARKER_CONTENT.as_bytes() {
            return Err(typed_error(
                PythonProvisioningCode::PathTampered,
                format!(
                    "Python runtime marker {} does not match Python {PYTHON_VERSION}; remove the tampered runtime and Retry",
                    marker.display()
                ),
            ));
        }
    }

    let python_bin = python_bin_under(root);
    let metadata = fs::symlink_metadata(&python_bin).map_err(|error| {
        typed_error(
            PythonProvisioningCode::InvalidArtifact,
            format!(
                "verified Python runtime is missing {} ({error}); Retry",
                python_bin.display()
            ),
        )
    })?;
    if metadata.file_type().is_symlink() || metadata_is_reparse_point(&metadata) {
        let canonical_root = fs::canonicalize(root).map_err(|error| {
            typed_error(
                PythonProvisioningCode::PathTampered,
                format!(
                    "cannot resolve Python runtime {} safely ({error}); remove it and Retry",
                    root.display()
                ),
            )
        })?;
        let canonical_bin = fs::canonicalize(&python_bin).map_err(|error| {
            typed_error(
                PythonProvisioningCode::PathTampered,
                format!(
                    "cannot resolve Python executable {} safely ({error}); remove it and Retry",
                    python_bin.display()
                ),
            )
        })?;
        if !canonical_bin.starts_with(&canonical_root) {
            return Err(typed_error(
                PythonProvisioningCode::PathTampered,
                format!(
                    "Python executable {} escapes the runtime directory; remove it and Retry",
                    python_bin.display()
                ),
            ));
        }
    }
    if !metadata.is_file() && !metadata.file_type().is_symlink() {
        return Err(typed_error(
            PythonProvisioningCode::InvalidArtifact,
            format!(
                "Python executable {} is not a regular file; Retry",
                python_bin.display()
            ),
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o111 == 0 {
            return Err(typed_error(
                PythonProvisioningCode::InvalidArtifact,
                format!(
                    "Python executable {} is not executable; Retry",
                    python_bin.display()
                ),
            ));
        }
    }

    let mut command = process_utils::isolated_python(&python_bin);
    command.args([
        "-c",
        "import sys; print(sys.version_info.major, sys.version_info.minor, sys.version_info.micro)",
    ]);
    verify_directory_guard(root, root_guard)?;
    let output = run_command_until(
        command,
        "Python runtime validation",
        "Verifying the downloaded Python runtime...",
        "Python runtime verification is still running; keep Tobkiri Launcher open...",
        options,
        log_path,
        progress,
    )?;
    verify_directory_guard(root, root_guard)?;
    if !output
        .text
        .lines()
        .any(|line| line.trim() == PYTHON_VERSION)
        && !output.text.contains("3 13 13")
        && !output.text.contains("3, 13, 13")
    {
        return Err(typed_error(
            PythonProvisioningCode::InvalidArtifact,
            format!(
                "Python executable {} did not report Python {PYTHON_VERSION}; remove it and Retry. Diagnostics: {}",
                python_bin.display(),
                log_path.display()
            ),
        ));
    }
    Ok(())
}

fn path_exists_or_reparse_point(path: &Path) -> bool {
    path.exists() || fs::symlink_metadata(path).is_ok()
}

fn remove_path_or_reparse_point(path: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("failed to inspect {}", path.display()))?;
    if metadata_is_removable_link(path, &metadata) {
        if metadata_is_directory_like(&metadata) {
            remove_reparse_dir(path)
        } else {
            fs::remove_file(path)
        }
    } else if metadata.is_dir() {
        remove_dir_all_reparse_safe(path)
    } else {
        fs::remove_file(path)
    }
    .with_context(|| format!("failed to remove {}", path.display()))
}

fn remove_dir_all_reparse_safe(path: &Path) -> io::Result<()> {
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let child_path = entry.path();
        let child_metadata = fs::symlink_metadata(&child_path)?;
        if metadata_is_removable_link(&child_path, &child_metadata) {
            if metadata_is_directory_like(&child_metadata) {
                remove_reparse_dir(&child_path)?;
            } else {
                fs::remove_file(&child_path)?;
            }
        } else if child_metadata.is_dir() {
            remove_dir_all_reparse_safe(&child_path)?;
        } else {
            fs::remove_file(&child_path)?;
        }
    }
    fs::remove_dir(path)
}

fn remove_reparse_dir(path: &Path) -> io::Result<()> {
    match fs::remove_dir(path) {
        Ok(()) => Ok(()),
        Err(first_error) => {
            #[cfg(windows)]
            {
                let status = process_utils::command("cmd")
                    .args(["/C", "rmdir", &path.to_string_lossy()])
                    .status();
                if status.is_ok_and(|status| status.success()) {
                    return Ok(());
                }
            }
            Err(first_error)
        }
    }
}

fn metadata_is_removable_link(path: &Path, metadata: &fs::Metadata) -> bool {
    metadata.file_type().is_symlink()
        || metadata_is_reparse_point(metadata)
        || (metadata.is_dir() && !path.exists())
}

fn metadata_is_directory_like(metadata: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x10;
        metadata.file_attributes() & FILE_ATTRIBUTE_DIRECTORY != 0
    }

    #[cfg(not(windows))]
    {
        metadata.is_dir()
    }
}

fn metadata_is_reparse_point(metadata: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
        metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
    }

    #[cfg(not(windows))]
    {
        let _ = metadata;
        false
    }
}

fn python_bin_under(root: &Path) -> PathBuf {
    if cfg!(target_os = "windows") {
        root.join("python.exe")
    } else {
        root.join("bin").join("python3")
    }
}

fn venv_python_under(root: &Path) -> PathBuf {
    if cfg!(target_os = "windows") {
        root.join("Scripts").join("python.exe")
    } else {
        root.join("bin").join("python3")
    }
}

fn verify_venv_python(config: &AppConfig, root: &Path) -> Result<()> {
    ensure_real_directory(root, "Python venv")?;
    let python_bin = venv_python_under(root);
    let metadata = fs::symlink_metadata(&python_bin).map_err(|error| {
        typed_error(
            PythonProvisioningCode::InvalidArtifact,
            format!(
                "Python venv is missing {} ({error}); Retry",
                python_bin.display()
            ),
        )
    })?;
    if metadata.file_type().is_symlink() || metadata_is_reparse_point(&metadata) {
        let canonical_bin = fs::canonicalize(&python_bin).map_err(|error| {
            typed_error(
                PythonProvisioningCode::PathTampered,
                format!(
                    "cannot resolve venv Python executable {} safely ({error}); remove it and Retry",
                    python_bin.display()
                ),
            )
        })?;
        let canonical_root = fs::canonicalize(root).map_err(|error| {
            typed_error(
                PythonProvisioningCode::PathTampered,
                format!(
                    "cannot resolve Python venv {} safely ({error}); remove it and Retry",
                    root.display()
                ),
            )
        })?;
        let canonical_runtime = fs::canonicalize(&config.python_dir).map_err(|error| {
            typed_error(
                PythonProvisioningCode::InvalidArtifact,
                format!(
                    "cannot resolve verified Python runtime {} ({error}); Retry",
                    config.python_dir.display()
                ),
            )
        })?;
        if !canonical_bin.starts_with(&canonical_root)
            && !canonical_bin.starts_with(&canonical_runtime)
        {
            return Err(typed_error(
                PythonProvisioningCode::PathTampered,
                format!(
                    "venv Python executable {} escapes the managed runtime; remove the venv and Retry",
                    python_bin.display()
                ),
            ));
        }
    } else if !metadata.is_file() {
        return Err(typed_error(
            PythonProvisioningCode::InvalidArtifact,
            format!(
                "Python venv executable {} is not a regular file; Retry",
                python_bin.display()
            ),
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o111 == 0 {
            return Err(typed_error(
                PythonProvisioningCode::InvalidArtifact,
                format!(
                    "Python venv executable {} is not executable; Retry",
                    python_bin.display()
                ),
            ));
        }
    }
    Ok(())
}

fn find_installed_python_dir(tmp_dir: &Path) -> Result<PathBuf> {
    let prefix = format!("cpython-{PYTHON_MINOR}");
    let versioned_prefix = format!("cpython-{PYTHON_MINOR}.");
    let mut candidates: Vec<PathBuf> = fs::read_dir(tmp_dir)
        .map_err(|error| {
            typed_error(
                PythonProvisioningCode::InvalidArtifact,
                format!(
                    "cannot inspect uv's Python install output at {} ({error}); Retry",
                    tmp_dir.display()
                ),
            )
        })?
        .filter_map(|entry| entry.ok())
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().into_owned();
            if !name.starts_with(&prefix) {
                return None;
            }
            let path = entry.path();
            python_bin_under(&path).exists().then_some(path)
        })
        .collect();

    candidates.sort_by(|left, right| {
        let left_name = left
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("");
        let right_name = right
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("");
        let left_versioned = left_name.starts_with(&versioned_prefix);
        let right_versioned = right_name.starts_with(&versioned_prefix);
        right_versioned
            .cmp(&left_versioned)
            .then_with(|| right_name.cmp(left_name))
    });

    if let Some(path) = candidates.into_iter().next() {
        return Ok(path);
    }

    let contents: Vec<String> = fs::read_dir(tmp_dir)
        .map_err(|error| {
            typed_error(
                PythonProvisioningCode::InvalidArtifact,
                format!(
                    "cannot inspect uv's Python install output at {} ({error}); Retry",
                    tmp_dir.display()
                ),
            )
        })?
        .filter_map(|entry| {
            entry
                .ok()
                .map(|entry| entry.file_name().to_string_lossy().into_owned())
        })
        .collect();
    // The caller owns staging cleanup so cleanup failures are never hidden.
    return Err(typed_error(
        PythonProvisioningCode::InvalidArtifact,
        format!(
            "uv python install succeeded but no usable directory matching \
         `{prefix}*` was found in the install dir.\n\
         Contents of {}: {:?}\n\
         This may indicate a change in uv's directory naming scheme; Retry",
            tmp_dir.display(),
            contents,
        ),
    ));
}

// ---------------------------------------------------------------------------
// Step 3 — venv
// ---------------------------------------------------------------------------

fn ensure_venv<F>(
    config: &AppConfig,
    uv: &Path,
    cache_dir: &Path,
    log_path: &Path,
    trust_key: &[u8],
    runtime: &RuntimeTrust,
    requirements_sha256: &str,
    options: ProvisionOptions,
    progress: &F,
) -> Result<bool>
where
    F: Fn(&str) + ?Sized,
{
    let venv_python = config.venv_python();
    reject_link(&config.venv_dir, "Python venv directory")?;
    if venv_python.exists() {
        match verify_venv_trust(config, uv, trust_key, runtime, requirements_sha256) {
            Ok(_) => {
                verify_venv_python(config, &config.venv_dir)?;
                info!(
                    "venv already present and cryptographically verified at {}",
                    config.venv_dir.display()
                );
                return Ok(true);
            }
            Err(error) => {
                warn!(
                    "Quarantining untrusted existing Python venv at {}: {error:#}",
                    config.venv_dir.display()
                );
                quarantine_owned_tree(config, &config.venv_dir, "python-venv")?;
                remove_trust_record(&venv_trust_path(config)?)?;
            }
        }
    }

    if path_exists_or_reparse_point(&config.venv_dir) {
        info!(
            "venv directory exists but {} is missing; recreating the venv",
            venv_python.display()
        );
        quarantine_owned_tree(config, &config.venv_dir, "python-venv")?;
        remove_trust_record(&venv_trust_path(config)?)?;
    }

    let tmp_dir = config.venv_dir.with_file_name("_venv_tmp");
    cleanup_staging_root(&tmp_dir)?;
    fs::create_dir_all(&tmp_dir).map_err(|error| {
        typed_error(
            PythonProvisioningCode::CleanupFailed,
            format!(
                "cannot create venv staging directory {}: {error}; Retry",
                tmp_dir.display()
            ),
        )
    })?;
    let python_bin = config.python_bin();
    let result = (|| {
        verify_directory_guard(&config.python_dir, &runtime.root_guard)?;
        let mut command = uv_command(uv, cache_dir);
        command.args([
            "venv",
            "--python",
            &python_bin.to_string_lossy(),
            &tmp_dir.to_string_lossy(),
        ]);
        run_command_until(
            command,
            "uv venv",
            "Creating the Python environment...",
            "Python environment creation is still running; keep Tobkiri Launcher open...",
            options,
            log_path,
            progress,
        )?;
        verify_directory_guard(&config.python_dir, &runtime.root_guard)?;
        verify_venv_python(config, &tmp_dir).with_context(|| {
            format!(
                "uv created an invalid venv; diagnostics: {}",
                log_path.display()
            )
        })?;
        reject_link(&config.venv_dir, "Python venv directory")?;
        if path_exists_or_reparse_point(&config.venv_dir) {
            return Err(typed_error(
                PythonProvisioningCode::PathTampered,
                format!(
                    "Python venv destination {} appeared during provisioning; close other Launcher processes and Retry",
                    config.venv_dir.display()
                ),
            ));
        }
        fs::rename(&tmp_dir, &config.venv_dir).map_err(|error| {
            typed_error(
                PythonProvisioningCode::CleanupFailed,
                format!(
                    "cannot atomically promote venv to {}: {error}; Retry",
                    config.venv_dir.display()
                ),
            )
        })?;
        sync_directory(config.venv_dir.parent().unwrap_or(Path::new(".")))?;
        stable_tree_identity(&config.venv_dir)?;
        info!("venv created at {}", config.venv_dir.display());
        Ok(false)
    })();

    let cleanup_result = cleanup_staging_root(&tmp_dir);
    match (result, cleanup_result) {
        (Ok(ready), Ok(())) => Ok(ready),
        (Err(error), Ok(())) => Err(error),
        (Ok(_), Err(error)) => Err(error),
        (Err(error), Err(cleanup_error)) => Err(anyhow::anyhow!(
            "{error}; venv staging cleanup also failed: {cleanup_error}"
        )),
    }
}

// ---------------------------------------------------------------------------
// Step 4 — requirements
// ---------------------------------------------------------------------------

fn validate_hashed_requirements(req_path: &Path) -> Result<()> {
    let contents = fs::read_to_string(req_path)
        .with_context(|| format!("failed to read {}", req_path.display()))?;

    for (line_number, logical_line) in logical_requirement_lines(&contents)? {
        let trimmed = strip_inline_comment(&logical_line).trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let tokens: Vec<&str> = trimmed.split_whitespace().collect();
        if tokens.is_empty() {
            continue;
        }
        if tokens[0].starts_with("--") {
            if tokens.as_slice() == ["--only-binary", ":all:"]
                || tokens.as_slice() == ["--only-binary=:all:"]
            {
                continue;
            }
            bail!(
                "{}:{} contains unsupported pip option {trimmed:?}; automatic installation only permits --only-binary :all:",
                req_path.display(),
                line_number
            );
        }

        validate_requirement_tokens(req_path, line_number, &tokens)?;
    }

    Ok(())
}

fn logical_requirement_lines(contents: &str) -> Result<Vec<(usize, String)>> {
    let mut result = Vec::new();
    let mut current = String::new();
    let mut start_line = 0usize;

    for (index, line) in contents.lines().enumerate() {
        let line_number = index + 1;
        let trimmed = line.trim();
        if current.is_empty() && (trimmed.is_empty() || trimmed.starts_with('#')) {
            continue;
        }

        if current.is_empty() {
            start_line = line_number;
        } else {
            current.push(' ');
        }

        let continued = trimmed.ends_with('\\');
        let segment = if continued {
            trimmed.trim_end_matches('\\').trim_end()
        } else {
            trimmed
        };
        current.push_str(segment);

        if !continued {
            result.push((start_line, current.trim().to_string()));
            current.clear();
        }
    }

    if !current.trim().is_empty() {
        bail!(
            "requirements.txt:{} has an unterminated line continuation",
            start_line
        );
    }

    Ok(result)
}

fn strip_inline_comment(line: &str) -> &str {
    line.find(" #").map_or(line, |index| &line[..index])
}

fn validate_requirement_tokens(req_path: &Path, line_number: usize, tokens: &[&str]) -> Result<()> {
    let package = tokens.first().copied().unwrap_or_default();
    if !is_exact_package_pin(package) {
        bail!(
            "{}:{} must start with an exact name==version package pin before automatic installation",
            req_path.display(),
            line_number
        );
    }

    let hash_start = if tokens.get(1) == Some(&";") {
        let hash_start = tokens
            .iter()
            .position(|token| token.starts_with("--hash=sha256:"))
            .unwrap_or(tokens.len());
        let marker = tokens.get(1..hash_start).unwrap_or_default();
        if !is_supported_environment_marker(marker) {
            bail!(
                "{}:{} contains an unsupported environment marker; automatic installation only permits safe interpreter-version or implementation comparisons joined by 'and'",
                req_path.display(),
                line_number
            );
        }
        hash_start
    } else {
        1
    };

    let mut hash_count = 0usize;
    for token in &tokens[hash_start..] {
        if !token.starts_with("--hash=sha256:") {
            bail!(
                "{}:{} contains unsupported requirement token {token:?}; only --hash=sha256:<64hex> is permitted after the package pin",
                req_path.display(),
                line_number
            );
        }
        let digest = token.trim_start_matches("--hash=sha256:");
        if !is_sha256_hex(digest) {
            bail!(
                "{}:{} contains an invalid SHA-256 hash {digest:?}",
                req_path.display(),
                line_number
            );
        }
        hash_count += 1;
    }

    if hash_count == 0 {
        bail!(
            "{}:{} must include at least one SHA-256 hash before automatic installation",
            req_path.display(),
            line_number
        );
    }

    Ok(())
}

fn is_supported_environment_marker(tokens: &[&str]) -> bool {
    if tokens.first() != Some(&";") {
        return false;
    }
    let mut index = 1usize;
    while index < tokens.len() {
        let Some(variable) = tokens.get(index) else {
            return false;
        };
        if !matches!(
            *variable,
            "python_version"
                | "python_full_version"
                | "platform_python_implementation"
                | "implementation_name"
        ) {
            return false;
        }
        let Some(operator) = tokens.get(index + 1) else {
            return false;
        };
        if !matches!(*operator, "<" | "<=" | "==" | "!=" | ">=" | ">") {
            return false;
        }
        let Some(quoted_value) = tokens.get(index + 2) else {
            return false;
        };
        if !is_safe_quoted_marker_value(quoted_value) {
            return false;
        }
        index += 3;
        if index == tokens.len() {
            return true;
        }
        if tokens.get(index) != Some(&"and") {
            return false;
        }
        index += 1;
    }
    false
}

fn is_safe_quoted_marker_value(value: &str) -> bool {
    if value.len() < 3 {
        return false;
    }
    let quote = value.as_bytes()[0];
    if !matches!(quote, b'\'' | b'"') || value.as_bytes().last() != Some(&quote) {
        return false;
    }
    let inner = &value[1..value.len() - 1];
    !inner.is_empty()
        && inner
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'+' | b'-'))
}

fn is_exact_package_pin(token: &str) -> bool {
    let Some((name, version)) = token.split_once("==") else {
        return false;
    };
    !name.is_empty()
        && !version.is_empty()
        && is_valid_package_name(name)
        && is_valid_version_token(version)
}

fn is_valid_package_name(name: &str) -> bool {
    name.as_bytes()
        .iter()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn is_valid_version_token(version: &str) -> bool {
    version.as_bytes().iter().all(|byte| {
        byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'!' | b'+' | b'-')
    })
}

fn is_sha256_hex(value: &str) -> bool {
    value.len() == 64 && value.as_bytes().iter().all(|byte| byte.is_ascii_hexdigit())
}

fn install_requirements<F>(
    config: &AppConfig,
    uv: &Path,
    cache_dir: &Path,
    log_path: &Path,
    trust_key: &[u8],
    runtime: &RuntimeTrust,
    requirements_sha256: &str,
    venv_ready: bool,
    options: ProvisionOptions,
    progress: &F,
) -> Result<()>
where
    F: Fn(&str) + ?Sized,
{
    let req_path = locked_requirements_path(config)?;
    if !req_path.exists() {
        bail!("locked requirements are missing at {}", req_path.display());
    }

    if sha256_regular_file(&req_path)? != requirements_sha256 {
        bail!("locked requirements changed during Python provisioning");
    }
    let venv_python = config.venv_python();
    if venv_ready {
        verify_runtime_trust(config, &config.python_dir, uv, trust_key)?;
        verify_venv_trust(config, uv, trust_key, runtime, requirements_sha256)?;
        info!("Cryptographic venv identity matches; skipping dependency installation");
        return Ok(());
    }

    verify_runtime_trust(config, &config.python_dir, uv, trust_key)?;
    verify_venv_python(config, &config.venv_dir)?;
    let venv_guard = guarded_directory(&config.venv_dir)?;
    stable_tree_identity(&config.venv_dir)?;
    verify_directory_guard(&config.venv_dir, &venv_guard)?;
    verify_directory_guard(&config.python_dir, &runtime.root_guard)?;

    let mut command = uv_command(uv, cache_dir);
    command.args([
        "pip",
        "install",
        "--require-hashes",
        "--only-binary",
        ":all:",
        "--python",
        &venv_python.to_string_lossy(),
        "-r",
        &req_path.to_string_lossy(),
    ]);
    run_command_until(
        command,
        "uv pip install",
        "Installing the runtime dependencies...",
        "Runtime dependency installation is still running; keep Tobkiri Launcher open...",
        options,
        log_path,
        progress,
    )?;
    verify_directory_guard(&config.venv_dir, &venv_guard)?;
    verify_directory_guard(&config.python_dir, &runtime.root_guard)?;

    if sha256_regular_file(&req_path)? != requirements_sha256 {
        bail!("locked requirements changed while dependencies were installed");
    }
    let legacy_stamp = config.venv_dir.join(".rumi_requirements_stamp");
    if path_exists_or_reparse_point(&legacy_stamp) {
        remove_path_or_reparse_point(&legacy_stamp)?;
    }
    set_tree_writable_state(&config.venv_dir, false)?;
    seal_venv(config, uv, trust_key, runtime, requirements_sha256)?;
    verify_venv_trust(config, uv, trust_key, runtime, requirements_sha256)?;

    info!("Requirements installed and cryptographically sealed");
    Ok(())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[cfg(unix)]
    fn test_options(timeout: Duration, notice: Duration) -> ProvisionOptions {
        ProvisionOptions {
            deadline: Instant::now() + timeout,
            timeout_secs: timeout.as_secs().max(1),
            lock_wait: Duration::from_secs(5),
            long_operation_notice: notice,
        }
    }

    #[cfg(unix)]
    fn test_config(root: &Path) -> AppConfig {
        AppConfig::detect_for_tauri(root.join("resources"), root.join("appdata")).unwrap()
    }

    #[test]
    fn packaged_setup_defers_invalid_environment_to_authoritative_role_spawn() {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-packaged-python-setup-{}",
            unix_timestamp_nanos()
        ));
        let resource_dir = root.join("resources");
        fs::create_dir_all(resource_dir.join("app")).unwrap();
        let config = AppConfig::detect_for_tauri(resource_dir, root.join("appdata")).unwrap();
        assert!(!config.is_dev_workspace());

        // This deliberately invalid sealed root would make the former
        // preflight verifier fail. Setup must leave validation to the
        // fail-closed role spawn that owns the executable snapshot.
        fs::write(config.app_dir.join("sealed_python"), b"tampered").unwrap();
        let progress = RefCell::new(Vec::new());
        ensure_python_env_with_progress(&config, |message| {
            progress.borrow_mut().push(message.to_string());
        })
        .unwrap();
        assert_eq!(
            progress.into_inner(),
            vec!["Packaged Python will be verified immediately before the runtime starts..."]
        );

        assert!(spawn_python_role(
            &config,
            PythonRole::Kernel,
            RoleArguments::default(),
            |_| Ok(()),
        )
        .is_err());
        fs::remove_dir_all(root).ok();
    }

    #[cfg(unix)]
    fn fake_uv(root: &Path, mode: &str, count_path: Option<&Path>) -> PathBuf {
        let path = root.join("fake-uv");
        let mut script = String::from("#!/bin/sh\nset -eu\nMODE='");
        script.push_str(mode);
        script.push_str("'\n");
        script.push_str("if [ \"$1\" = \"--no-config\" ]; then shift; fi\n");
        script.push_str("if [ \"$MODE\" = \"slow\" ]; then sleep 1; fi\n");
        script.push_str(
            "if [ \"$MODE\" = \"network\" ]; then echo 'network unavailable' >&2; exit 7; fi\n",
        );
        script.push_str("if [ \"$MODE\" = \"fail\" ]; then echo 'child failed' >&2; exit 9; fi\n");
        script.push_str("if [ \"$MODE\" = \"empty\" ]; then exit 0; fi\n");
        script.push_str(
            "if [ \"$1\" = \"venv\" ]; then\n\
             for last; do :; done\n\
             mkdir -p \"$last/bin\" \"$last/site-packages\"\n\
             printf '#!/bin/sh\\nprintf \"3 13 13\\\\n\"\\n' > \"$last/bin/python3\"\n\
             chmod +x \"$last/bin/python3\"\n\
             exit 0\n\
             fi\n\
             if [ \"$1\" = \"pip\" ]; then\n\
             python_path=''\n\
             previous=''\n\
             for argument in \"$@\"; do\n\
             if [ \"$previous\" = \"--python\" ]; then python_path=\"$argument\"; fi\n\
             previous=\"$argument\"\n\
             done\n\
             venv_root=$(dirname \"$(dirname \"$python_path\")\")\n\
             printf 'installed\\n' > \"$venv_root/site-packages/locked-package.txt\"\n\
             exit 0\n\
             fi\n",
        );
        script.push_str("if [ \"$1\" != \"python\" ]; then exit 0; fi\n");
        script.push_str("install_dir=''\nprevious=''\n");
        script.push_str(
            "for argument in \"$@\"; do\n\
             if [ \"$previous\" = \"--install-dir\" ]; then install_dir=\"$argument\"; fi\n\
             previous=\"$argument\"\n\
             done\n",
        );
        script.push_str("if [ -z \"$install_dir\" ]; then exit 8; fi\n");
        if let Some(count_path) = count_path {
            script.push_str("printf x >> '");
            script.push_str(&count_path.to_string_lossy());
            script.push_str("'\n");
        }
        script.push_str(
            "runtime_root=\"$install_dir/cpython-3.13.13-fake\"\n\
             mkdir -p \"$runtime_root/bin\" \"$runtime_root/lib\"\n\
             printf '#!/bin/sh\\nprintf \"3 13 13\\\\n\"\\n' > \"$runtime_root/bin/python3\"\n\
             printf 'trusted library\\n' > \"$runtime_root/lib/core.txt\"\n\
             chmod +x \"$runtime_root/bin/python3\"\n",
        );
        fs::write(&path, script).unwrap();
        let mut permissions = fs::metadata(&path).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&path, permissions).unwrap();
        path
    }

    #[test]
    fn provisioning_error_codes_are_stable_and_actionable() {
        let error = PythonProvisioningError::new(
            PythonProvisioningCode::Timeout,
            "download exceeded the bounded deadline; Retry",
        );

        assert_eq!(error.code(), PythonProvisioningCode::Timeout);
        assert_eq!(
            error.to_string(),
            "[PYTHON_PROVISION_TIMEOUT] download exceeded the bounded deadline; Retry"
        );
    }

    #[test]
    #[cfg(unix)]
    fn unix_external_provisioning_fails_before_child_spawn() {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-python-no-external-spawn-{}",
            unix_timestamp_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let sentinel = root.join("spawned");
        let mut command = process_utils::command("/bin/sh");
        command.args(["-c", &format!("touch {}", sentinel.display())]);
        let error = run_command_until(
            command,
            "external provisioner",
            "starting",
            "running",
            test_options(Duration::from_secs(1), Duration::from_millis(10)),
            &root.join("diagnostic.log"),
            &|_| {},
        )
        .unwrap_err();
        assert!(error
            .to_string()
            .contains("PYTHON_PROVISION_CONTAINMENT_UNAVAILABLE"));
        assert!(!sentinel.exists());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    #[cfg(unix)]
    fn stale_lock_reclamation_rejects_replaced_object() {
        let unique = unix_timestamp_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_python_lock_race_{unique}"));
        fs::create_dir_all(&root).unwrap();
        let lock_path = root.join("_python_provision.lock");
        let old_contents = "pid=4294967295\nstarted_nanos=1\n";
        let new_contents = "pid=4294967294\nstarted_nanos=2\n";
        fs::write(&lock_path, old_contents).unwrap();

        let inspected = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&lock_path)
            .unwrap();
        assert!(try_lock_exclusive(&inspected).unwrap());
        let observed = read_lock_contents(&inspected).unwrap();

        let replacement = root.join("_python_provision.lock.replacement");
        fs::write(&replacement, new_contents).unwrap();
        fs::rename(&replacement, &lock_path).unwrap();

        assert!(!lock_reclaim_identity_matches(&lock_path, &inspected, &observed).unwrap());
        assert_eq!(fs::read_to_string(&lock_path).unwrap(), new_contents);

        unlock_exclusive(&inspected).unwrap();
        drop(inspected);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    #[cfg(windows)]
    fn windows_process_containment_uses_a_job_object() {
        let job = WindowsJob::new().unwrap();
        assert!(!job.handle.is_null());
    }

    #[test]
    #[cfg(windows)]
    fn windows_child_is_suspended_until_job_assignment_then_resumed() {
        use std::os::windows::process::CommandExt;

        let mut command = process_utils::command("cmd");
        command
            .args(["/C", "exit", "0"])
            .creation_flags(CREATE_SUSPENDED);
        let containment = WindowsJob::new().unwrap();
        let mut child = command.spawn().unwrap();
        containment.assign(&child).unwrap();
        containment.resume(&child).unwrap();
        assert!(child.wait().unwrap().success());
    }

    #[test]
    #[cfg(unix)]
    fn forged_venv_stamp_and_site_packages_never_match_signed_identity() {
        let unique = unix_timestamp_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_venv_forgery_{unique}"));
        fs::create_dir_all(&root).unwrap();
        let config = test_config(&root);
        let uv = fake_uv(&root, "success", None);
        let key = load_or_create_trust_key(&config).unwrap();
        let runtime = RuntimeTrust {
            install_identity: "sha256:".to_string() + &"a".repeat(64),
            root_guard: guarded_directory(&provision_state_root(&config).unwrap()).unwrap(),
        };
        let requirements = "sha256:".to_string() + &"b".repeat(64);
        fs::create_dir_all(config.venv_dir.join("bin")).unwrap();
        fs::create_dir_all(config.venv_dir.join("site-packages")).unwrap();
        fs::write(config.venv_dir.join("bin/python3"), b"python").unwrap();
        fs::write(config.venv_dir.join("site-packages/package.py"), b"trusted").unwrap();
        seal_venv(&config, &uv, &key, &runtime, &requirements).unwrap();
        verify_venv_trust(&config, &uv, &key, &runtime, &requirements).unwrap();

        fs::write(
            config.venv_dir.join("site-packages/package.py"),
            b"poisoned",
        )
        .unwrap();
        fs::write(
            config.venv_dir.join(".rumi_requirements_stamp"),
            requirements.as_bytes(),
        )
        .unwrap();
        assert!(verify_venv_trust(&config, &uv, &key, &runtime, &requirements).is_err());

        let trust_path = venv_trust_path(&config).unwrap();
        let mut forged: SignedInstallTrust =
            serde_json::from_slice(&read_regular_nofollow(&trust_path).unwrap()).unwrap();
        forged.payload.tree.digest = "sha256:".to_string() + &"c".repeat(64);
        atomic_write_private(&trust_path, &serde_json::to_vec(&forged).unwrap()).unwrap();
        assert!(read_signed_trust(&trust_path, &key).is_err());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    #[cfg(unix)]
    fn trust_key_and_state_root_permissions_fail_closed() {
        let unique = unix_timestamp_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_python_permissions_{unique}"));
        fs::create_dir_all(&root).unwrap();
        let config = test_config(&root);
        load_or_create_trust_key(&config).unwrap();
        let key_path = trust_directory(&config).unwrap().join(TRUST_KEY_FILE);
        fs::set_permissions(&key_path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(load_or_create_trust_key(&config).is_err());

        let linked_root = root.join("linked-appdata");
        std::os::unix::fs::symlink(root.join("appdata"), &linked_root).unwrap();
        let linked = AppConfig::detect_for_tauri(root.join("resources"), linked_root).unwrap();
        assert!(provision_state_root(&linked).is_err());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn ensure_uv_fails_closed_without_trusted_uv() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_no_trusted_uv_{unique}"));
        let config =
            AppConfig::detect_for_tauri(root.join("resources"), root.join("appdata")).unwrap();

        let old_path = std::env::var_os("PATH");
        let old_uv_path = std::env::var_os("RUMI_UV_PATH");
        std::env::set_var("PATH", "");
        std::env::remove_var("RUMI_UV_PATH");
        let err = ensure_uv(&config).unwrap_err().to_string();
        if let Some(path) = old_path {
            std::env::set_var("PATH", path);
        } else {
            std::env::remove_var("PATH");
        }
        if let Some(path) = old_uv_path {
            std::env::set_var("RUMI_UV_PATH", path);
        } else {
            std::env::remove_var("RUMI_UV_PATH");
        }

        assert!(err.contains("no trusted uv binary found"));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn validate_hashed_requirements_rejects_unpinned_lines() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_unhashed_requirements_{unique}"));
        let req_path = root.join("requirements.txt");
        fs::create_dir_all(&root).unwrap();
        fs::write(&req_path, format!("pyyaml{}6.0\n", ">=")).unwrap();

        let err = validate_hashed_requirements(&req_path)
            .unwrap_err()
            .to_string();

        assert!(err.contains("must start with an exact name==version package pin"));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn validate_hashed_requirements_accepts_pinned_hashes() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_hashed_requirements_{unique}"));
        let req_path = root.join("requirements.txt");
        fs::create_dir_all(&root).unwrap();
        fs::write(
            &req_path,
            "--only-binary :all:\npyyaml==6.0.2 --hash=sha256:70b189594dbe54f75ab3a1acec5f1e3faa7e8cf2f1e08d9b561cb41b845f69d5\n",
        )
        .unwrap();

        validate_hashed_requirements(&req_path).unwrap();
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn validate_hashed_requirements_accepts_continued_multi_hash_lines() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_multi_hash_requirements_{unique}"));
        let req_path = root.join("requirements.txt");
        fs::create_dir_all(&root).unwrap();
        fs::write(
            &req_path,
            concat!(
                "--only-binary=:all:\n",
                "pyyaml==6.0.2 \\\n",
                "  --hash=sha256:70b189594dbe54f75ab3a1acec5f1e3faa7e8cf2f1e08d9b561cb41b845f69d5 \\\n",
                "  --hash=sha256:8388ee1976c416731879ac16da0aff3f63b286ffdd57cdeb95f3f2e085687563\n",
            ),
        )
        .unwrap();

        validate_hashed_requirements(&req_path).unwrap();
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn validate_hashed_requirements_accepts_python_version_markers() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_marker_requirements_{unique}"));
        let req_path = root.join("requirements.txt");
        fs::create_dir_all(&root).unwrap();
        fs::write(
            &req_path,
            concat!(
                "rpds-py==2026.6.3 ; python_version >= \"3.11\" ",
                "--hash=sha256:0be972be84cfcaf46c8c6edf690ca0f154ac17babf1f6a955a51579b34ad2dc5\n",
            ),
        )
        .unwrap();

        validate_hashed_requirements(&req_path).unwrap();
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn validate_hashed_requirements_accepts_uv_implementation_markers() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_uv_marker_requirements_{unique}"));
        let req_path = root.join("requirements.txt");
        fs::create_dir_all(&root).unwrap();
        fs::write(
            &req_path,
            concat!(
                "cffi==2.1.0 ; platform_python_implementation != 'PyPy' ",
                "--hash=sha256:02cb7ff33ded4f1532476731f89ede53e2e488a8e6205515a82144246ffa7dcc\n",
                "pycparser==3.0 ; implementation_name != 'PyPy' and ",
                "platform_python_implementation != 'PyPy' ",
                "--hash=sha256:600f49d217304a5902ac3c37e1281c9fe94e4d0489de643a9504c5cdfdfc6b29\n",
                "rpds-py==2026.6.3 ; python_full_version >= '3.11' ",
                "--hash=sha256:0be972be84cfcaf46c8c6edf690ca0f154ac17babf1f6a955a51579b34ad2dc5\n",
            ),
        )
        .unwrap();

        validate_hashed_requirements(&req_path).unwrap();
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn bundled_runtime_requirements_pass_launcher_validation() {
        let req_path =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tobkiri_runtime/requirements.txt");

        validate_hashed_requirements(&req_path).unwrap();
    }

    #[test]
    fn validate_hashed_requirements_rejects_arbitrary_environment_markers() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_bad_marker_requirements_{unique}"));
        let req_path = root.join("requirements.txt");
        fs::create_dir_all(&root).unwrap();
        fs::write(
            &req_path,
            concat!(
                "pyyaml==6.0.2 ; sys_platform == \"darwin\" ",
                "--hash=sha256:70b189594dbe54f75ab3a1acec5f1e3faa7e8cf2f1e08d9b561cb41b845f69d5\n",
            ),
        )
        .unwrap();

        let err = validate_hashed_requirements(&req_path)
            .unwrap_err()
            .to_string();

        assert!(err.contains("unsupported environment marker"));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn validate_hashed_requirements_rejects_extra_package_options() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_extra_option_requirements_{unique}"));
        let req_path = root.join("requirements.txt");
        fs::create_dir_all(&root).unwrap();
        fs::write(
            &req_path,
            concat!(
                "pyyaml==6.0.2 ",
                "--hash=sha256:70b189594dbe54f75ab3a1acec5f1e3faa7e8cf2f1e08d9b561cb41b845f69d5 ",
                "--index-url https://example.invalid/simple\n",
            ),
        )
        .unwrap();

        let err = validate_hashed_requirements(&req_path)
            .unwrap_err()
            .to_string();

        assert!(err.contains("unsupported requirement token"));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn validate_hashed_requirements_rejects_invalid_hashes() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_bad_hash_requirements_{unique}"));
        let req_path = root.join("requirements.txt");
        fs::create_dir_all(&root).unwrap();
        fs::write(&req_path, "pyyaml==6.0.2 --hash=sha256:not-a-real-hash\n").unwrap();

        let err = validate_hashed_requirements(&req_path)
            .unwrap_err()
            .to_string();

        assert!(err.contains("invalid SHA-256 hash"));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn validate_hashed_requirements_rejects_source_build_options() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_source_build_requirements_{unique}"));
        let req_path = root.join("requirements.txt");
        fs::create_dir_all(&root).unwrap();
        fs::write(
            &req_path,
            "--no-binary :all:\npyyaml==6.0.2 --hash=sha256:70b189594dbe54f75ab3a1acec5f1e3faa7e8cf2f1e08d9b561cb41b845f69d5\n",
        )
        .unwrap();

        let err = validate_hashed_requirements(&req_path)
            .unwrap_err()
            .to_string();

        assert!(err.contains("unsupported pip option"));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn python_install_dir_prefers_patch_version_over_minor_alias() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_python_install_dir_{unique}"));
        let alias = root.join("cpython-3.13-windows-x86_64-none");
        let patch = root.join("cpython-3.13.13-windows-x86_64-none");

        fs::create_dir_all(python_bin_under(&alias).parent().unwrap()).unwrap();
        fs::create_dir_all(python_bin_under(&patch).parent().unwrap()).unwrap();
        fs::write(python_bin_under(&alias), b"alias").unwrap();
        fs::write(python_bin_under(&patch), b"patch").unwrap();

        let selected = find_installed_python_dir(&root).unwrap();

        assert_eq!(selected, patch);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    #[cfg(windows)]
    fn remove_path_handles_broken_junction() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("rumi_broken_junction_{unique}"));
        let target = root.join("target");
        let link = root.join("link");
        fs::create_dir_all(&target).unwrap();

        let status = process_utils::command("cmd")
            .args([
                "/C",
                "mklink",
                "/J",
                &link.to_string_lossy(),
                &target.to_string_lossy(),
            ])
            .status()
            .unwrap();
        assert!(status.success());

        fs::remove_dir_all(&target).unwrap();
        assert!(path_exists_or_reparse_point(&link));

        remove_path_or_reparse_point(&link).unwrap();

        assert!(!path_exists_or_reparse_point(&link));
        fs::remove_dir_all(root).ok();
    }
}
