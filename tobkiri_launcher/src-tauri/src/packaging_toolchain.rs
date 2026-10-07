//! Formal, digest-bound tool inputs for production packaging.
//!
//! Release build code must never resolve Python or Git through PATH, nor
//! trust the ambient PYTHON variable. CI binds these values from a checked
//! toolchain-resolution step; every caller revalidates the absolute file and
//! its raw SHA-256 before spawning it.

use std::env;
use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Output, Stdio};
#[cfg(target_os = "macos")]
use std::sync::Arc;

#[cfg(target_os = "macos")]
const DARWIN_MAX_CAPTURE_BYTES: usize = 64 * 1024 * 1024;
#[cfg(target_os = "macos")]
const DARWIN_OUTPUT_DIAGNOSTIC_BYTES: usize = 4 * 1024;
const SEALED_RESEAL_MAX_INVENTORY_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const SEALED_RESEAL_MAX_FILE_COUNT: u64 = 100_000;
const SEALED_RESEAL_MIN_SECONDS: u64 = 30;
const SEALED_RESEAL_MAX_SECONDS: u64 = 240;
const SEALED_RESEAL_BYTES_PER_SECOND: u64 = 4 * 1024 * 1024;
const SEALED_RESEAL_FILES_PER_SECOND: u64 = 500;

use sha2::{Digest, Sha256};

#[cfg(target_os = "macos")]
const SEC_CS_NO_NETWORK_ACCESS: u32 = 1 << 29;
#[cfg(target_os = "macos")]
const SEC_CS_STRICT_VALIDATE: u32 = 1 << 4;
#[cfg(target_os = "macos")]
const SEC_CS_CHECK_ALL_ARCHITECTURES: u32 = 1;
#[cfg(target_os = "macos")]
const SEC_CODE_SIGNATURE_ADHOC: i64 = 0x2;

#[cfg(target_os = "macos")]
fn accepted_macos_signature_flags(flags: i64) -> bool {
    flags & SEC_CODE_SIGNATURE_ADHOC == 0
}

pub const PYTHON_PATH_ENV: &str = "TOBKIRI_PACKAGING_PYTHON";
pub const PYTHON_SHA256_ENV: &str = "TOBKIRI_PACKAGING_PYTHON_SHA256";
pub const GIT_PATH_ENV: &str = "TOBKIRI_PACKAGING_GIT";
pub const GIT_SHA256_ENV: &str = "TOBKIRI_PACKAGING_GIT_SHA256";
#[cfg(target_os = "macos")]
pub const PYTHON_SNAPSHOT_ENV: &str = "TOBKIRI_PACKAGING_PYTHON_SNAPSHOT";
#[cfg(target_os = "macos")]
pub const PYTHON_INVENTORY_SHA256_ENV: &str = "TOBKIRI_PACKAGING_PYTHON_INVENTORY_SHA256";

/// A fail-closed execution budget derived from a verified packaging contract.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct VerifiedOutputBudget {
    duration: std::time::Duration,
}

impl VerifiedOutputBudget {
    /// Derive the sealed-application re-seal budget from its digest-bound
    /// inventory. Callers must obtain both values from that verified manifest.
    pub fn sealed_python_reseal(inventory_bytes: u64, file_count: u64) -> io::Result<Self> {
        if inventory_bytes == 0
            || inventory_bytes > SEALED_RESEAL_MAX_INVENTORY_BYTES
            || file_count == 0
            || file_count > SEALED_RESEAL_MAX_FILE_COUNT
        {
            return Err(invalid(
                "sealed Python re-seal inventory work exceeds the formal contract",
            ));
        }
        let byte_seconds = inventory_bytes
            .checked_add(SEALED_RESEAL_BYTES_PER_SECOND - 1)
            .ok_or_else(|| invalid("sealed Python re-seal byte budget overflow"))?
            / SEALED_RESEAL_BYTES_PER_SECOND;
        let file_seconds = file_count
            .checked_add(SEALED_RESEAL_FILES_PER_SECOND - 1)
            .ok_or_else(|| invalid("sealed Python re-seal file budget overflow"))?
            / SEALED_RESEAL_FILES_PER_SECOND;
        let seconds = SEALED_RESEAL_MIN_SECONDS
            .checked_add(byte_seconds)
            .and_then(|value| value.checked_add(file_seconds))
            .ok_or_else(|| invalid("sealed Python re-seal execution budget overflow"))?
            .min(SEALED_RESEAL_MAX_SECONDS);
        Ok(Self {
            duration: std::time::Duration::from_secs(seconds),
        })
    }
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

fn valid_raw_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn file_identity(metadata: &fs::Metadata) -> (u64, u64, u64, u64, u64) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;

        return (
            metadata.dev(),
            metadata.ino(),
            metadata.len(),
            metadata.mtime_nsec() as u64,
            metadata.ctime_nsec() as u64,
        );
    }
    #[cfg(not(unix))]
    {
        let modified = metadata
            .modified()
            .ok()
            .and_then(|value| value.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(0, |value| value.as_nanos() as u64);
        let created = metadata
            .created()
            .ok()
            .and_then(|value| value.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(0, |value| value.as_nanos() as u64);
        (0, 0, metadata.len(), modified, created)
    }
}

fn validate_parent_path(path: &Path) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| invalid("packaging tool has no parent directory"))?;
    let canonical = parent.canonicalize()?;
    if canonical != parent {
        return Err(invalid(format!(
            "packaging tool parent path is not canonical: {}",
            parent.display()
        )));
    }
    let metadata = fs::symlink_metadata(parent)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(invalid("packaging tool parent is not a real directory"));
    }
    Ok(())
}

fn open_hashed_regular_executable(path: &Path) -> io::Result<(File, fs::Metadata, String)> {
    validate_parent_path(path)?;
    let before = fs::symlink_metadata(path)?;
    if before.file_type().is_symlink() || !before.is_file() {
        return Err(invalid(format!(
            "packaging tool is not a regular file: {}",
            path.display()
        )));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        let mode = before.permissions().mode();
        if mode & 0o111 == 0 {
            return Err(invalid(format!(
                "packaging tool is not executable: {}",
                path.display()
            )));
        }
        if mode & 0o022 != 0 {
            return Err(invalid(format!(
                "packaging tool is writable: {}",
                path.display()
            )));
        }
    }
    let canonical = path.canonicalize()?;
    if canonical != path {
        return Err(invalid(format!(
            "packaging tool path is not canonical: {}",
            path.display()
        )));
    }
    let mut digest = Sha256::new();
    let mut input = File::open(path)?;
    let opened = input.metadata()?;
    if file_identity(&opened) != file_identity(&before) {
        return Err(invalid(format!(
            "packaging tool changed while opened: {}",
            path.display()
        )));
    }
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    let after = fs::symlink_metadata(path)?;
    if after.file_type().is_symlink()
        || !after.is_file()
        || file_identity(&before) != file_identity(&after)
    {
        return Err(invalid(format!(
            "packaging tool changed while hashed: {}",
            path.display()
        )));
    }
    Ok((input, before, format!("{:x}", digest.finalize())))
}

#[allow(dead_code)]
pub struct VerifiedTool {
    kind: String,
    original_path: PathBuf,
    identity: (u64, u64, u64, u64, u64),
    #[cfg(unix)]
    execution_path: PathBuf,
    #[cfg(unix)]
    execution_owner: Option<PathBuf>,
    #[cfg(unix)]
    execution_identity: (u64, u64, u64, u64, u64),
    #[cfg(unix)]
    owns_execution_copy: bool,
    #[cfg(target_os = "macos")]
    macos_cdhash: Vec<u8>,
    #[cfg(target_os = "macos")]
    python_installation: Option<Arc<MacOSPythonInstallationLease>>,
    lock: File,
}

#[cfg(target_os = "macos")]
struct MacOSPythonInstallationLease {
    root: PathBuf,
    identity: (u64, u64, u64, u64, u64),
    inventory_sha256: String,
    _root_handle: File,
    _inventory_handle: File,
    snapshot_path: Option<PathBuf>,
}

#[cfg(target_os = "macos")]
impl MacOSPythonInstallationLease {
    fn verify_unchanged(&self) -> io::Result<()> {
        use std::collections::BTreeSet;
        use std::os::unix::fs::{MetadataExt, PermissionsExt};

        if macos_fd_has_nontrivial_acl(&self._root_handle)? {
            return Err(invalid("macOS Python authority ACL changed"));
        }
        if macos_fd_has_nontrivial_acl(&self._inventory_handle)? {
            return Err(invalid("macOS Python inventory ACL changed"));
        }
        let current = fs::symlink_metadata(&self.root)?;
        if current.file_type().is_symlink()
            || !current.is_dir()
            || file_identity(&current) != self.identity
        {
            return Err(invalid("macOS Python installation root identity changed"));
        }
        let mut inventory = self._inventory_handle.try_clone()?;
        inventory.seek(SeekFrom::Start(0))?;
        let mut digest = Sha256::new();
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = inventory.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            digest.update(&buffer[..count]);
        }
        let actual = format!("{:x}", digest.finalize());
        if actual != self.inventory_sha256 {
            return Err(invalid("macOS Python installation inventory changed"));
        }
        inventory.seek(SeekFrom::Start(0))?;
        let document: serde_json::Value = serde_json::from_reader(inventory)
            .map_err(|error| invalid(format!("sealed Python manifest is invalid: {error}")))?;
        let files = document
            .get("files")
            .and_then(serde_json::Value::as_array)
            .ok_or_else(|| invalid("sealed Python manifest files are missing"))?;
        let mut expected = BTreeSet::new();
        for entry in files {
            let relative = entry
                .get("path")
                .and_then(serde_json::Value::as_str)
                .ok_or_else(|| invalid("sealed Python inventory path is missing"))?;
            let relative_path = Path::new(relative);
            if relative_path.is_absolute()
                || relative_path
                    .components()
                    .any(|component| !matches!(component, std::path::Component::Normal(_)))
                || !expected.insert(relative.to_owned())
            {
                return Err(invalid("sealed Python inventory path is unsafe"));
            }
            let path = self.root.join(relative_path);
            let metadata = fs::symlink_metadata(&path)?;
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.permissions().mode() & 0o222 != 0
            {
                return Err(invalid("sealed Python inventory entry is unsafe"));
            }
            let bytes = fs::read(&path)?;
            let digest = format!("{:x}", Sha256::digest(&bytes));
            if entry.get("size").and_then(serde_json::Value::as_u64) != Some(bytes.len() as u64)
                || entry.get("sha256").and_then(serde_json::Value::as_str) != Some(digest.as_str())
                || entry.get("executable").and_then(serde_json::Value::as_bool)
                    != Some(metadata.permissions().mode() & 0o111 != 0)
            {
                return Err(invalid("sealed Python inventory entry changed"));
            }
        }
        let mut actual_paths = BTreeSet::new();
        collect_snapshot_files(&self.root, &self.root, &mut actual_paths)?;
        actual_paths.remove("sealed-environment.v1.json");
        if actual_paths != expected {
            return Err(invalid("sealed Python snapshot has missing or extra files"));
        }
        Ok(())
    }
}

#[cfg(target_os = "macos")]
impl Drop for MacOSPythonInstallationLease {
    fn drop(&mut self) {
        if let Some(path) = self.snapshot_path.take() {
            let _ = cleanup_private_macos_python_snapshot(&path, Some(&self._root_handle));
        }
    }
}

#[cfg(target_os = "macos")]
fn cleanup_private_macos_python_snapshot(path: &Path, root: Option<&File>) -> io::Result<()> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    let named = fs::symlink_metadata(path)?;
    if named.file_type().is_symlink() || !named.is_dir() {
        return Err(invalid("private Python cleanup root changed type"));
    }
    if let Some(root) = root {
        let held = root.metadata()?;
        if named.dev() != held.dev() || named.ino() != held.ino() {
            return Err(invalid("private Python cleanup root identity changed"));
        }
    }
    fn unseal(path: &Path) -> io::Result<()> {
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
        for entry in fs::read_dir(path)? {
            let entry = entry?;
            let metadata = fs::symlink_metadata(entry.path())?;
            if metadata.file_type().is_symlink() {
                return Err(invalid("private Python cleanup found a symlink"));
            }
            if metadata.is_dir() {
                unseal(&entry.path())?;
            }
        }
        Ok(())
    }
    unseal(path)?;
    fs::remove_dir_all(path)
}

#[cfg(target_os = "macos")]
fn collect_snapshot_files(
    root: &Path,
    directory: &Path,
    output: &mut std::collections::BTreeSet<String>,
) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() || metadata.permissions().mode() & 0o222 != 0 {
            return Err(invalid("sealed Python snapshot contains unsafe entry"));
        }
        if metadata.is_dir() {
            collect_snapshot_files(root, &path, output)?;
        } else if metadata.is_file() {
            let relative = path
                .strip_prefix(root)
                .map_err(|_| invalid("sealed Python snapshot path escaped"))?;
            output.insert(relative.to_string_lossy().replace('\\', "/"));
        } else {
            return Err(invalid("sealed Python snapshot contains special entry"));
        }
    }
    Ok(())
}

impl VerifiedTool {
    fn configure_command<'a>(&'a self, command: &mut VerifiedCommand<'a>) {
        if self.kind != "git" {
            return;
        }
        #[cfg(target_os = "macos")]
        command
            .env_clear()
            .args([
                "--no-optional-locks",
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.attributesFile=/dev/null",
                "-c",
                "core.excludesFile=/dev/null",
                "-c",
                "diff.external=",
                "-c",
                "core.sshCommand=false",
                "-c",
                "core.pager=cat",
                "-c",
                "pager.show=cat",
            ])
            .env("GIT_ATTR_NOSYSTEM", "1")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_OPTIONAL_LOCKS", "0")
            .env("GIT_PAGER", "cat")
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("LC_ALL", "C")
            .env("PATH", "/usr/bin:/bin")
            .env("PAGER", "cat")
            // GIT_CONFIG redirects only `git config`. Other commands are
            // constrained to non-executing plumbing and Core byte checks.
            .env("GIT_CONFIG", "/dev/null")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_SYSTEM", "/dev/null")
            .env("GIT_EXEC_PATH", "/private/var/empty")
            .env("HOME", "/private/var/empty")
            .env("XDG_CONFIG_HOME", "/private/var/empty");
    }

    pub fn command(&self) -> io::Result<VerifiedCommand<'_>> {
        #[cfg(not(unix))]
        {
            let current = fs::symlink_metadata(&self.original_path)?;
            if current.file_type().is_symlink()
                || !current.is_file()
                || file_identity(&current) != self.identity
            {
                return Err(invalid(format!(
                    "{} executable path changed before spawn",
                    self.kind
                )));
            }
        }
        #[cfg(unix)]
        {
            let execution = fs::symlink_metadata(&self.execution_path)?;
            if execution.file_type().is_symlink()
                || !execution.is_file()
                || file_identity(&execution) != self.execution_identity
            {
                return Err(invalid("sealed packaging tool copy was replaced"));
            }
            #[cfg(target_os = "macos")]
            {
                if let Some(installation) = &self.python_installation {
                    installation.verify_unchanged()?;
                }
                if macos_code_identity(&self.execution_path, self.python_installation.is_some())?
                    != self.macos_cdhash
                {
                    return Err(invalid("macOS packaging tool CDHash changed before spawn"));
                }
            }
            let mut command = VerifiedCommand::new(self);
            self.configure_command(&mut command);
            #[cfg(target_os = "macos")]
            if self.python_installation.is_some() {
                command.bind_python_runtime_cwd()?;
            }
            return Ok(command);
        }
        #[cfg(not(unix))]
        {
            let _ = &self.lock;
            let mut command = VerifiedCommand::new(self);
            self.configure_command(&mut command);
            Ok(command)
        }
    }

    #[cfg(test)]
    pub fn original_path(&self) -> &Path {
        &self.original_path
    }
}

impl Drop for VerifiedTool {
    fn drop(&mut self) {
        #[cfg(unix)]
        {
            if self.owns_execution_copy {
                if let Ok(metadata) = fs::symlink_metadata(&self.execution_path) {
                    if metadata.is_file()
                        && !metadata.file_type().is_symlink()
                        && file_identity(&metadata) == self.execution_identity
                    {
                        if let Some(owner) = &self.execution_owner {
                            use std::os::unix::fs::PermissionsExt;
                            let _ = fs::set_permissions(owner, fs::Permissions::from_mode(0o700));
                        }
                        let _ = fs::remove_file(&self.execution_path);
                        if let Some(owner) = &self.execution_owner {
                            let _ = fs::remove_dir(owner);
                        }
                    }
                }
            }
        }
    }
}

/// Command builder whose Unix child replaces itself from the verified open
/// executable descriptor.  No packaging tool pathname is reopened on Linux.
pub struct VerifiedCommand<'a> {
    tool: &'a VerifiedTool,
    args: Vec<std::ffi::OsString>,
    environment: std::collections::BTreeMap<std::ffi::OsString, std::ffi::OsString>,
    clear_environment: bool,
    current_dir: Option<PathBuf>,
    #[cfg(unix)]
    current_dir_handle: Option<File>,
}

pub enum VerifiedChild {
    Standard(Child),
    #[cfg(target_os = "macos")]
    Darwin(DarwinChild),
}

pub enum VerifiedSpawnOutcome {
    NoChild(io::Error),
    ReapedFailure(io::Error),
    Running(VerifiedChild),
    Uncontained(io::Error),
}

impl VerifiedChild {
    pub fn wait(&mut self) -> io::Result<ExitStatus> {
        match self {
            Self::Standard(child) => child.wait(),
            #[cfg(target_os = "macos")]
            Self::Darwin(child) => child.wait(),
        }
    }

    pub fn kill(&mut self) -> io::Result<()> {
        match self {
            Self::Standard(child) => child.kill(),
            #[cfg(target_os = "macos")]
            Self::Darwin(child) => child.kill(),
        }
    }

    pub fn wait_until(&mut self, deadline: std::time::Instant) -> io::Result<Option<ExitStatus>> {
        loop {
            let status = match self {
                Self::Standard(child) => child.try_wait()?,
                #[cfg(target_os = "macos")]
                Self::Darwin(child) => child.wait_nonblocking_until(deadline)?,
            };
            if status.is_some() || std::time::Instant::now() >= deadline {
                return Ok(status);
            }
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
    }
}

impl<'a> VerifiedCommand<'a> {
    fn new(tool: &'a VerifiedTool) -> Self {
        Self {
            tool,
            args: Vec::new(),
            environment: std::collections::BTreeMap::new(),
            clear_environment: false,
            current_dir: None,
            #[cfg(unix)]
            current_dir_handle: None,
        }
    }

    pub fn arg<S: AsRef<std::ffi::OsStr>>(&mut self, arg: S) -> &mut Self {
        self.args.push(arg.as_ref().to_owned());
        self
    }

    pub fn args<I, S>(&mut self, args: I) -> &mut Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<std::ffi::OsStr>,
    {
        self.args
            .extend(args.into_iter().map(|arg| arg.as_ref().to_owned()));
        self
    }

    pub fn env<K, V>(&mut self, key: K, value: V) -> &mut Self
    where
        K: AsRef<std::ffi::OsStr>,
        V: AsRef<std::ffi::OsStr>,
    {
        assert!(
            !key.as_ref().as_encoded_bytes().contains(&b'='),
            "environment key must not contain '='"
        );
        self.environment
            .insert(key.as_ref().to_owned(), value.as_ref().to_owned());
        self
    }

    pub fn env_clear(&mut self) -> &mut Self {
        self.clear_environment = true;
        self.environment.clear();
        self
    }

    pub fn current_dir<P: AsRef<Path>>(&mut self, path: P) -> io::Result<&mut Self> {
        self.current_dir = Some(path.as_ref().to_owned());
        #[cfg(target_os = "macos")]
        if self.tool.kind == "git" {
            self.environment.insert(
                std::ffi::OsString::from("GIT_CEILING_DIRECTORIES"),
                path.as_ref().as_os_str().to_owned(),
            );
        }
        #[cfg(unix)]
        {
            use std::ffi::CString;
            use std::os::fd::FromRawFd;
            use std::os::unix::ffi::OsStrExt;
            let encoded = CString::new(path.as_ref().as_os_str().as_bytes())
                .map_err(|_| invalid("verified command cwd contains NUL"))?;
            let fd = unsafe {
                libc::open(
                    encoded.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
                )
            };
            if fd == -1 {
                return Err(io::Error::last_os_error());
            }
            self.current_dir_handle = Some(unsafe { File::from_raw_fd(fd) });
        }
        Ok(self)
    }

    #[cfg(unix)]
    pub fn current_dir_handle(&mut self, directory: &File) -> io::Result<&mut Self> {
        self.current_dir = None;
        self.current_dir_handle = Some(directory.try_clone()?);
        Ok(self)
    }

    #[cfg(target_os = "macos")]
    /// Anchor a sealed Python child in the root that owns its manifest.
    pub fn bind_python_runtime_cwd(&mut self) -> io::Result<&mut Self> {
        let installation = self
            .tool
            .python_installation
            .as_ref()
            .ok_or_else(|| invalid("verified command is not bound to sealed Python"))?;
        installation.verify_unchanged()?;
        self.current_dir = None;
        self.current_dir_handle = Some(installation._root_handle.try_clone()?);
        Ok(self)
    }

    fn environment(&self) -> std::collections::BTreeMap<std::ffi::OsString, std::ffi::OsString> {
        let mut values = if self.clear_environment {
            std::collections::BTreeMap::new()
        } else {
            std::env::vars_os().collect()
        };
        values.extend(self.environment.clone());
        values
    }

    #[cfg(unix)]
    fn command_with_stdio(&self, capture: bool) -> io::Result<Command> {
        use std::ffi::CString;
        use std::os::fd::AsRawFd;
        use std::os::unix::ffi::OsStrExt;
        use std::os::unix::process::CommandExt;

        let executable = self.tool.execution_path.clone();
        let executable_c = CString::new(executable.as_os_str().as_bytes())
            .map_err(|_| invalid("sealed executable path contains NUL"))?;
        #[cfg(any(target_os = "linux", target_os = "android"))]
        let executable_fd = self.tool.lock.try_clone()?;
        let current_dir_fd = self
            .current_dir_handle
            .as_ref()
            .map(File::try_clone)
            .transpose()?;
        let argv = std::iter::once(self.tool.original_path.as_os_str())
            .chain(self.args.iter().map(std::ffi::OsString::as_os_str))
            .map(|value| {
                CString::new(value.as_bytes()).map_err(|_| invalid("tool argument contains NUL"))
            })
            .collect::<io::Result<Vec<_>>>()?;
        let environment = self
            .environment()
            .into_iter()
            .map(|(key, value)| {
                let mut pair = key.as_bytes().to_vec();
                pair.push(b'=');
                pair.extend_from_slice(value.as_bytes());
                CString::new(pair).map_err(|_| invalid("tool environment contains NUL"))
            })
            .collect::<io::Result<Vec<_>>>()?;
        let argv_ptrs = argv
            .iter()
            .map(|value| value.as_ptr())
            .chain(std::iter::once(std::ptr::null()))
            .map(|value| value as usize)
            .collect::<Vec<_>>();
        let environment_ptrs = environment
            .iter()
            .map(|value| value.as_ptr())
            .chain(std::iter::once(std::ptr::null()))
            .map(|value| value as usize)
            .collect::<Vec<_>>();
        let mut command = Command::new("/usr/bin/false");
        command.env_clear();
        if let Some(directory) = &self.current_dir {
            command.current_dir(directory);
        }
        if capture {
            command.stdout(Stdio::piped()).stderr(Stdio::piped());
        }
        unsafe {
            command.pre_exec(move || {
                let _argv_storage = &argv;
                let _environment_storage = &environment;
                let argv_raw = argv_ptrs.as_ptr().cast::<*const libc::c_char>();
                let environment_raw = environment_ptrs.as_ptr().cast::<*const libc::c_char>();
                if let Some(directory) = &current_dir_fd {
                    if libc::fchdir(directory.as_raw_fd()) == -1 {
                        return Err(io::Error::last_os_error());
                    }
                }
                #[cfg(any(target_os = "linux", target_os = "android"))]
                {
                    let fd = executable_fd.as_raw_fd();
                    if libc::fcntl(fd, libc::F_SETFD, 0) == -1 {
                        return Err(io::Error::last_os_error());
                    }
                    libc::fexecve(fd, argv_raw, environment_raw);
                }
                #[cfg(not(any(target_os = "linux", target_os = "android")))]
                {
                    libc::execve(executable_c.as_ptr(), argv_raw, environment_raw);
                }
                Err(io::Error::last_os_error())
            });
        }
        Ok(command)
    }

    #[cfg(windows)]
    fn command_with_stdio(&self, capture: bool) -> io::Result<Command> {
        let mut command = Command::new(&self.tool.original_path);
        if self.clear_environment {
            command.env_clear();
        }
        command.args(&self.args).envs(&self.environment);
        if let Some(directory) = &self.current_dir {
            command.current_dir(directory);
        }
        if capture {
            command.stdout(Stdio::piped()).stderr(Stdio::piped());
        }
        Ok(command)
    }

    #[cfg(target_os = "macos")]
    fn spawn_darwin(&self, capture: bool) -> VerifiedSpawnOutcome {
        let identity = match macos_code_identity(
            &self.tool.execution_path,
            self.tool.python_installation.is_some(),
        ) {
            Ok(identity) => identity,
            Err(error) => return VerifiedSpawnOutcome::NoChild(error),
        };
        if identity != self.tool.macos_cdhash {
            return VerifiedSpawnOutcome::NoChild(invalid(
                "macOS packaging tool identity changed before spawn",
            ));
        }
        if self.current_dir.is_some() && self.current_dir_handle.is_none() {
            return VerifiedSpawnOutcome::NoChild(invalid(
                "Darwin verified command cwd could not be anchored",
            ));
        }
        let mut child = match spawn_suspended_darwin(
            &self.tool.execution_path,
            &self.args,
            &self.environment(),
            self.current_dir_handle.as_ref(),
            capture,
        ) {
            Ok(child) => child,
            Err(error) => return VerifiedSpawnOutcome::NoChild(error),
        };
        child._python_installation = self.tool.python_installation.clone();
        if let Err(primary) = child.start_output_readers() {
            let containment = child.kill().and_then(|()| {
                child
                    .wait_nonblocking_until(
                        std::time::Instant::now() + std::time::Duration::from_secs(2),
                    )?
                    .ok_or_else(|| invalid("timed out reaping Darwin child after reader failure"))
                    .map(|_| ())
            });
            return match containment {
                Ok(()) => VerifiedSpawnOutcome::ReapedFailure(primary),
                Err(containment) => VerifiedSpawnOutcome::Uncontained(invalid(format!(
                    "{primary}; Darwin child containment also failed: {containment}"
                ))),
            };
        }
        let pid = child.pid;
        let result = (|| {
            if macos_guest_code_identity(pid)? != self.tool.macos_cdhash {
                return Err(invalid("suspended macOS child identity mismatch"));
            }
            if unsafe { libc::kill(pid, libc::SIGCONT) } == -1 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        })();
        if let Err(error) = result {
            let mut child = child;
            let containment = child.kill().and_then(|()| {
                child
                    .wait_nonblocking_until(
                        std::time::Instant::now() + std::time::Duration::from_secs(2),
                    )?
                    .ok_or_else(|| invalid("timed out reaping rejected Darwin child"))
                    .map(|_| ())
            });
            return match containment {
                Ok(()) => VerifiedSpawnOutcome::ReapedFailure(error),
                Err(containment) => VerifiedSpawnOutcome::Uncontained(invalid(format!(
                    "{error}; rejected Darwin child containment also failed: {containment}"
                ))),
            };
        }
        VerifiedSpawnOutcome::Running(VerifiedChild::Darwin(child))
    }

    pub fn spawn_outcome(&mut self) -> VerifiedSpawnOutcome {
        #[cfg(target_os = "macos")]
        {
            return self.spawn_darwin(false);
        }
        #[cfg(not(target_os = "macos"))]
        {
            match self
                .command_with_stdio(false)
                .and_then(|mut command| command.spawn())
            {
                Ok(child) => VerifiedSpawnOutcome::Running(VerifiedChild::Standard(child)),
                Err(error) => VerifiedSpawnOutcome::NoChild(error),
            }
        }
    }

    pub fn spawn(&mut self) -> io::Result<VerifiedChild> {
        match self.spawn_outcome() {
            VerifiedSpawnOutcome::Running(child) => Ok(child),
            VerifiedSpawnOutcome::NoChild(error)
            | VerifiedSpawnOutcome::ReapedFailure(error)
            | VerifiedSpawnOutcome::Uncontained(error) => Err(error),
        }
    }

    pub fn status(&mut self) -> io::Result<ExitStatus> {
        self.spawn()?.wait()
    }

    pub fn output(&mut self) -> io::Result<Output> {
        self.output_with_optional_budget(None)
    }

    /// Collect output under a typed packaging-operation work budget.
    pub fn output_with_budget(&mut self, budget: VerifiedOutputBudget) -> io::Result<Output> {
        self.output_with_optional_budget(Some(budget))
    }

    fn output_with_optional_budget(
        &mut self,
        budget: Option<VerifiedOutputBudget>,
    ) -> io::Result<Output> {
        #[cfg(target_os = "macos")]
        {
            return match self.spawn_darwin(true) {
                VerifiedSpawnOutcome::Running(VerifiedChild::Darwin(child)) => match budget {
                    Some(budget) => child.wait_with_output_until(
                        std::time::Instant::now() + budget.duration,
                        std::time::Duration::from_secs(2),
                    ),
                    None => child.wait_with_output(),
                },
                VerifiedSpawnOutcome::NoChild(error)
                | VerifiedSpawnOutcome::ReapedFailure(error)
                | VerifiedSpawnOutcome::Uncontained(error) => Err(error),
                VerifiedSpawnOutcome::Running(VerifiedChild::Standard(_)) => unreachable!(),
            };
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = budget;
            let mut command = self.command_with_stdio(true)?;
            command.stdin(Stdio::null());
            command.spawn()?.wait_with_output()
        }
    }
}

#[cfg(target_os = "macos")]
fn spawn_suspended_darwin(
    executable: &Path,
    arguments: &[std::ffi::OsString],
    environment: &std::collections::BTreeMap<std::ffi::OsString, std::ffi::OsString>,
    directory: Option<&File>,
    capture: bool,
) -> io::Result<DarwinChild> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;

    unsafe extern "C" {
        fn posix_spawn(
            pid: *mut i32,
            path: *const libc::c_char,
            actions: *const *mut std::ffi::c_void,
            attributes: *const *mut std::ffi::c_void,
            argv: *const *mut libc::c_char,
            envp: *const *mut libc::c_char,
        ) -> i32;
        fn posix_spawnattr_init(attributes: *mut *mut std::ffi::c_void) -> i32;
        fn posix_spawnattr_setflags(attributes: *mut *mut std::ffi::c_void, flags: i16) -> i32;
        fn posix_spawnattr_setpgroup(attributes: *mut *mut std::ffi::c_void, pgroup: i32) -> i32;
        fn posix_spawnattr_destroy(attributes: *mut *mut std::ffi::c_void) -> i32;
        fn posix_spawn_file_actions_init(actions: *mut *mut std::ffi::c_void) -> i32;
        fn posix_spawn_file_actions_addfchdir_np(
            actions: *mut *mut std::ffi::c_void,
            fd: i32,
        ) -> i32;
        fn posix_spawn_file_actions_adddup2(
            actions: *mut *mut std::ffi::c_void,
            fd: i32,
            newfd: i32,
        ) -> i32;
        fn posix_spawn_file_actions_addclose(actions: *mut *mut std::ffi::c_void, fd: i32) -> i32;
        fn posix_spawn_file_actions_destroy(actions: *mut *mut std::ffi::c_void) -> i32;
    }
    fn pipe() -> io::Result<(File, File)> {
        let mut fds = [-1; 2];
        if unsafe { libc::pipe(fds.as_mut_ptr()) } == -1 {
            return Err(io::Error::last_os_error());
        }
        for fd in fds {
            if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } == -1 {
                unsafe {
                    libc::close(fds[0]);
                    libc::close(fds[1]);
                }
                return Err(io::Error::last_os_error());
            }
        }
        Ok(unsafe { (File::from_raw_fd(fds[0]), File::from_raw_fd(fds[1])) })
    }
    let path = CString::new(executable.as_os_str().as_bytes())
        .map_err(|_| invalid("Darwin executable path contains NUL"))?;
    let argv = std::iter::once(executable.as_os_str())
        .chain(arguments.iter().map(std::ffi::OsString::as_os_str))
        .map(|value| {
            CString::new(value.as_bytes()).map_err(|_| invalid("Darwin argument contains NUL"))
        })
        .collect::<io::Result<Vec<_>>>()?;
    let env = environment
        .iter()
        .map(|(key, value)| {
            let mut pair = key.as_bytes().to_vec();
            pair.push(b'=');
            pair.extend_from_slice(value.as_bytes());
            CString::new(pair).map_err(|_| invalid("Darwin environment contains NUL"))
        })
        .collect::<io::Result<Vec<_>>>()?;
    let mut argv_ptrs = argv
        .iter()
        .map(|v| v.as_ptr() as *mut _)
        .chain(std::iter::once(std::ptr::null_mut()))
        .collect::<Vec<_>>();
    let mut env_ptrs = env
        .iter()
        .map(|v| v.as_ptr() as *mut _)
        .chain(std::iter::once(std::ptr::null_mut()))
        .collect::<Vec<_>>();
    let (stdout_read, stdout_write) = if capture {
        let (r, w) = pipe()?;
        (Some(r), Some(w))
    } else {
        (None, None)
    };
    let (stderr_read, stderr_write) = if capture {
        let (r, w) = pipe()?;
        (Some(r), Some(w))
    } else {
        (None, None)
    };
    let null_input = if capture {
        Some(File::open("/dev/null")?)
    } else {
        None
    };
    let mut attributes = std::ptr::null_mut();
    let mut actions = std::ptr::null_mut();
    let mut pid = 0_i32;
    let result = unsafe {
        let mut code = posix_spawnattr_init(&mut attributes);
        if code == 0 {
            code = posix_spawnattr_setflags(
                &mut attributes,
                (libc::POSIX_SPAWN_START_SUSPENDED | libc::POSIX_SPAWN_SETPGROUP) as i16,
            );
        }
        if code == 0 {
            // A zero pgroup makes the child the leader of a fresh process
            // group. This is established by the kernel before any child code
            // runs, so timeout containment never relies on a racy setpgid.
            code = posix_spawnattr_setpgroup(&mut attributes, 0);
        }
        if code == 0 {
            code = posix_spawn_file_actions_init(&mut actions);
        }
        if code == 0 {
            if let Some(directory) = directory {
                code = posix_spawn_file_actions_addfchdir_np(&mut actions, directory.as_raw_fd());
            }
        }
        if code == 0 {
            if let Some(file) = &null_input {
                code = posix_spawn_file_actions_adddup2(
                    &mut actions,
                    file.as_raw_fd(),
                    libc::STDIN_FILENO,
                );
            }
        }
        if code == 0 {
            if let Some(file) = &stdout_write {
                code = posix_spawn_file_actions_adddup2(
                    &mut actions,
                    file.as_raw_fd(),
                    libc::STDOUT_FILENO,
                );
            }
        }
        if code == 0 {
            if let Some(file) = &stderr_write {
                code = posix_spawn_file_actions_adddup2(
                    &mut actions,
                    file.as_raw_fd(),
                    libc::STDERR_FILENO,
                );
            }
        }
        if code == 0 {
            if let Some(file) = &stdout_read {
                code = posix_spawn_file_actions_addclose(&mut actions, file.as_raw_fd());
            }
        }
        if code == 0 {
            if let Some(file) = &stderr_read {
                code = posix_spawn_file_actions_addclose(&mut actions, file.as_raw_fd());
            }
        }
        for file in [&null_input, &stdout_write, &stderr_write]
            .into_iter()
            .flatten()
        {
            if code == 0
                && ![libc::STDIN_FILENO, libc::STDOUT_FILENO, libc::STDERR_FILENO]
                    .contains(&file.as_raw_fd())
            {
                code = posix_spawn_file_actions_addclose(&mut actions, file.as_raw_fd());
            }
        }
        if code == 0 {
            code = posix_spawn(
                &mut pid,
                path.as_ptr(),
                &actions,
                &attributes,
                argv_ptrs.as_mut_ptr(),
                env_ptrs.as_mut_ptr(),
            );
        }
        if !actions.is_null() {
            posix_spawn_file_actions_destroy(&mut actions);
        }
        if !attributes.is_null() {
            posix_spawnattr_destroy(&mut attributes);
        }
        code
    };
    drop(stdout_write);
    drop(stderr_write);
    if result != 0 {
        return Err(io::Error::from_raw_os_error(result));
    }
    if unsafe { libc::getpgid(pid) } != pid {
        let primary = invalid("Darwin child did not enter its dedicated process group");
        let killed = unsafe { libc::kill(pid, libc::SIGKILL) };
        if killed == -1 {
            return Err(invalid(format!(
                "{primary}; failed to signal child with invalid process-group identity"
            )));
        }
        let mut status = 0;
        let reaped = loop {
            let result = unsafe { libc::waitpid(pid, &mut status, 0) };
            if result == pid {
                break true;
            }
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::EINTR) {
                break false;
            }
        };
        if !reaped {
            return Err(invalid(format!(
                "{primary}; failed to contain child with invalid process-group identity"
            )));
        }
        return Err(primary);
    }
    let child = DarwinChild {
        pid,
        process_group: pid,
        stdout: stdout_read,
        stderr: stderr_read,
        stdout_reader: None,
        stderr_reader: None,
        status: None,
        state: DarwinChildState::Running,
        _python_installation: None,
    };
    Ok(child)
}

#[cfg(target_os = "macos")]
fn macos_guest_code_identity(pid: i32) -> io::Result<Vec<u8>> {
    type CFTypeRef = *const std::ffi::c_void;
    type CFDictionaryRef = *const std::ffi::c_void;
    type SecCodeRef = *const std::ffi::c_void;
    #[link(name = "CoreFoundation", kind = "framework")]
    unsafe extern "C" {
        fn CFNumberCreate(
            allocator: CFTypeRef,
            kind: i32,
            value: *const std::ffi::c_void,
        ) -> CFTypeRef;
        fn CFDictionaryCreate(
            allocator: CFTypeRef,
            keys: *const CFTypeRef,
            values: *const CFTypeRef,
            count: isize,
            key_callbacks: *const std::ffi::c_void,
            value_callbacks: *const std::ffi::c_void,
        ) -> CFDictionaryRef;
        fn CFDictionaryGetValue(dictionary: CFDictionaryRef, key: CFTypeRef) -> CFTypeRef;
        fn CFDataGetLength(data: CFTypeRef) -> isize;
        fn CFDataGetBytePtr(data: CFTypeRef) -> *const u8;
        fn CFRelease(value: CFTypeRef);
    }
    #[link(name = "Security", kind = "framework")]
    unsafe extern "C" {
        static kSecGuestAttributePid: CFTypeRef;
        static kSecCodeInfoUnique: CFTypeRef;
        fn SecCodeCopyGuestWithAttributes(
            host: SecCodeRef,
            attributes: CFDictionaryRef,
            flags: u32,
            guest: *mut SecCodeRef,
        ) -> i32;
        fn SecCodeCopySigningInformation(
            code: SecCodeRef,
            flags: u32,
            information: *mut CFDictionaryRef,
        ) -> i32;
    }
    let number = unsafe {
        CFNumberCreate(
            std::ptr::null(),
            9,
            (&pid as *const i32).cast::<std::ffi::c_void>(),
        )
    };
    if number.is_null() {
        return Err(invalid("could not encode suspended macOS child PID"));
    }
    let attributes = unsafe {
        CFDictionaryCreate(
            std::ptr::null(),
            &kSecGuestAttributePid,
            &number,
            1,
            std::ptr::null(),
            std::ptr::null(),
        )
    };
    if attributes.is_null() {
        unsafe { CFRelease(number) };
        return Err(invalid("could not create suspended macOS child attributes"));
    }
    let mut guest = std::ptr::null();
    let copied =
        unsafe { SecCodeCopyGuestWithAttributes(std::ptr::null(), attributes, 0, &mut guest) };
    unsafe { CFRelease(attributes) };
    unsafe { CFRelease(number) };
    if copied != 0 || guest.is_null() {
        return Err(invalid(
            "could not resolve suspended macOS child code object",
        ));
    }
    let mut information = std::ptr::null();
    let signed = unsafe { SecCodeCopySigningInformation(guest, 1 << 1, &mut information) };
    unsafe { CFRelease(guest) };
    if signed != 0 || information.is_null() {
        return Err(invalid("could not read suspended macOS child signature"));
    }
    let unique = unsafe { CFDictionaryGetValue(information, kSecCodeInfoUnique) };
    let length = if unique.is_null() {
        0
    } else {
        unsafe { CFDataGetLength(unique) }
    };
    if length <= 0 || length > 64 {
        unsafe { CFRelease(information) };
        return Err(invalid("suspended macOS child CDHash is unavailable"));
    }
    let digest =
        unsafe { std::slice::from_raw_parts(CFDataGetBytePtr(unique), length as usize) }.to_vec();
    unsafe { CFRelease(information) };
    Ok(digest)
}

#[cfg(target_os = "macos")]
pub struct DarwinChild {
    pid: i32,
    process_group: i32,
    stdout: Option<File>,
    stderr: Option<File>,
    stdout_reader: Option<DarwinOutputReader>,
    stderr_reader: Option<DarwinOutputReader>,
    status: Option<ExitStatus>,
    state: DarwinChildState,
    _python_installation: Option<Arc<MacOSPythonInstallationLease>>,
}

#[cfg(target_os = "macos")]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DarwinChildState {
    Running,
    KillSent,
    Reaped,
    ExternalReaped,
    Lost,
}

#[cfg(target_os = "macos")]
#[derive(Default)]
struct DarwinCapturedStream {
    bytes: Vec<u8>,
    error: Option<String>,
}

#[cfg(target_os = "macos")]
struct DarwinOutputReader {
    receiver: std::sync::mpsc::Receiver<DarwinCapturedStream>,
    thread: Option<std::thread::JoinHandle<()>>,
    stop: Arc<std::sync::atomic::AtomicBool>,
}

#[cfg(target_os = "macos")]
enum DarwinReaderPoll {
    Finished(DarwinCapturedStream),
    Pending,
    Failed(String),
}

#[cfg(target_os = "macos")]
impl DarwinChild {
    fn start_output_readers(&mut self) -> io::Result<()> {
        use std::sync::atomic::{AtomicBool, AtomicUsize};

        if self.stdout_reader.is_some() || self.stderr_reader.is_some() {
            return Err(invalid("Darwin output readers were already started"));
        }
        let budget = Arc::new(AtomicUsize::new(DARWIN_MAX_CAPTURE_BYTES));
        let stop = Arc::new(AtomicBool::new(false));
        if let Some(file) = self.stdout.take() {
            set_nonblocking(&file)?;
            self.stdout_reader = Some(spawn_darwin_output_reader(
                file,
                Arc::clone(&budget),
                Arc::clone(&stop),
                "stdout",
            )?);
        }
        if let Some(file) = self.stderr.take() {
            set_nonblocking(&file)?;
            self.stderr_reader = Some(spawn_darwin_output_reader(file, budget, stop, "stderr")?);
        }
        Ok(())
    }

    fn wait_nonblocking_until(
        &mut self,
        deadline: std::time::Instant,
    ) -> io::Result<Option<ExitStatus>> {
        if let Some(status) = self.status {
            self.state = DarwinChildState::Reaped;
            return Ok(Some(status));
        }
        loop {
            let mut raw = 0;
            let result = unsafe { libc::waitpid(self.pid, &mut raw, libc::WNOHANG) };
            if result == self.pid {
                use std::os::unix::process::ExitStatusExt;
                let status = ExitStatus::from_raw(raw);
                self.status = Some(status);
                self.state = DarwinChildState::Reaped;
                return Ok(Some(status));
            }
            if result == -1 {
                let error = io::Error::last_os_error();
                if matches!(error.raw_os_error(), Some(libc::ECHILD)) {
                    self.state = DarwinChildState::ExternalReaped;
                    return Err(invalid("Darwin child was already reaped externally"));
                }
                if error.raw_os_error() != Some(libc::EINTR) {
                    self.state = DarwinChildState::Lost;
                    return Err(error);
                }
            }
            if std::time::Instant::now() >= deadline {
                return Ok(None);
            }
            std::thread::yield_now();
        }
    }

    fn wait(&mut self) -> io::Result<ExitStatus> {
        if let Some(status) = self.status {
            self.state = DarwinChildState::Reaped;
            return Ok(status);
        }
        if self.state == DarwinChildState::Lost {
            return Err(invalid("refusing to wait on a lost Darwin child PID"));
        }
        if self.state == DarwinChildState::ExternalReaped {
            return Err(invalid("Darwin child was already reaped externally"));
        }
        let mut raw = 0;
        loop {
            if unsafe { libc::waitpid(self.pid, &mut raw, 0) } != -1 {
                break;
            }
            let error = io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ECHILD) {
                self.state = DarwinChildState::ExternalReaped;
                return Err(invalid("Darwin child was already reaped externally"));
            }
            if error.raw_os_error() != Some(libc::EINTR) {
                self.state = DarwinChildState::Lost;
                return Err(error);
            }
        }
        use std::os::unix::process::ExitStatusExt;
        let status = ExitStatus::from_raw(raw);
        self.status = Some(status);
        self.state = DarwinChildState::Reaped;
        Ok(status)
    }

    fn kill_process_group(&mut self) -> io::Result<()> {
        if self.state == DarwinChildState::Lost {
            return Err(invalid(
                "refusing to signal a lost Darwin child process group",
            ));
        }
        if self.state == DarwinChildState::ExternalReaped {
            return Err(invalid(
                "refusing to signal an externally reaped Darwin child process group",
            ));
        }
        if self.process_group <= 0 || self.process_group != self.pid {
            return Err(invalid("Darwin child process group identity is invalid"));
        }
        if unsafe { libc::kill(-self.process_group, libc::SIGKILL) } == -1 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::ESRCH) {
                return Err(error);
            }
        }
        if self.status.is_none() {
            self.state = DarwinChildState::KillSent;
        }
        Ok(())
    }

    fn kill(&mut self) -> io::Result<()> {
        if self.status.is_some() {
            self.state = DarwinChildState::Reaped;
            return Ok(());
        }
        if self.state == DarwinChildState::Lost {
            return Err(invalid("refusing to signal a lost Darwin child PID"));
        }
        if self.state == DarwinChildState::ExternalReaped {
            return Err(invalid("Darwin child was already reaped externally"));
        }
        self.kill_process_group()
    }

    fn wait_with_output(self) -> io::Result<Output> {
        self.wait_with_output_until(
            std::time::Instant::now() + std::time::Duration::from_secs(30),
            std::time::Duration::from_secs(2),
        )
    }

    fn wait_with_output_until(
        mut self,
        execution_deadline: std::time::Instant,
        drain_grace: std::time::Duration,
    ) -> io::Result<Output> {
        if (self.stdout.is_some() || self.stderr.is_some())
            && self.stdout_reader.is_none()
            && self.stderr_reader.is_none()
        {
            self.start_output_readers()?;
        }
        let mut errors = Vec::new();
        let status_result = match self.wait_nonblocking_until(execution_deadline) {
            Ok(Some(status)) => Ok(status),
            Ok(None) => {
                let primary = invalid("timed out waiting for Darwin child");
                match self.kill_process_group().and_then(|()| {
                    self.wait_nonblocking_until(
                        std::time::Instant::now() + std::time::Duration::from_secs(2),
                    )?
                    .ok_or_else(|| invalid("timed out reaping Darwin child"))
                }) {
                    Ok(_) => Err(primary),
                    Err(cleanup) => Err(invalid(format!(
                        "{primary}; Darwin child containment also failed: {cleanup}"
                    ))),
                }
            }
            Err(primary) if self.state == DarwinChildState::ExternalReaped => Err(primary),
            Err(primary) => Err(invalid(format!(
                "{primary}; Darwin child identity is lost, so PID containment was stopped to avoid signaling a reused PID"
            ))),
        };
        if let Err(error) = &status_result {
            errors.push(error.to_string());
        }

        let first_drain_deadline = std::time::Instant::now() + drain_grace;
        let mut stdout =
            poll_darwin_reader(&mut self.stdout_reader, first_drain_deadline, "stdout");
        let mut stderr =
            poll_darwin_reader(&mut self.stderr_reader, first_drain_deadline, "stderr");
        if matches!(stdout, DarwinReaderPoll::Pending)
            || matches!(stderr, DarwinReaderPoll::Pending)
        {
            errors.push(
                "Darwin child descendants kept output pipes open after child completion".into(),
            );
            if let Err(error) = self.kill_process_group() {
                errors.push(format!(
                    "Darwin child process-group containment failed: {error}"
                ));
            }
            let containment_deadline = std::time::Instant::now() + drain_grace;
            if matches!(stdout, DarwinReaderPoll::Pending) {
                stdout =
                    poll_darwin_reader(&mut self.stdout_reader, containment_deadline, "stdout");
            }
            if matches!(stderr, DarwinReaderPoll::Pending) {
                stderr =
                    poll_darwin_reader(&mut self.stderr_reader, containment_deadline, "stderr");
            }
        }
        if matches!(stdout, DarwinReaderPoll::Pending)
            || matches!(stderr, DarwinReaderPoll::Pending)
        {
            stop_darwin_reader(&self.stdout_reader);
            stop_darwin_reader(&self.stderr_reader);
            let stop_deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
            if matches!(stdout, DarwinReaderPoll::Pending) {
                stdout = poll_darwin_reader(&mut self.stdout_reader, stop_deadline, "stdout");
            }
            if matches!(stderr, DarwinReaderPoll::Pending) {
                stderr = poll_darwin_reader(&mut self.stderr_reader, stop_deadline, "stderr");
            }
        }

        let stdout = finish_darwin_capture(stdout, "stdout", &mut errors);
        let stderr = finish_darwin_capture(stderr, "stderr", &mut errors);
        if !errors.is_empty() {
            let diagnostics = format_darwin_diagnostics(&stdout.bytes, &stderr.bytes);
            return Err(invalid(format!(
                "Darwin child output collection failed: {}{diagnostics}",
                errors.join("; ")
            )));
        }
        let status = status_result.expect("status error is included above");
        Ok(Output {
            status,
            stdout: stdout.bytes,
            stderr: stderr.bytes,
        })
    }
}

#[cfg(target_os = "macos")]
fn set_nonblocking(file: &File) -> io::Result<()> {
    use std::os::fd::AsRawFd;

    let flags = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETFL) };
    if flags == -1
        || unsafe { libc::fcntl(file.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) } == -1
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn spawn_darwin_output_reader(
    mut file: File,
    budget: Arc<std::sync::atomic::AtomicUsize>,
    stop: Arc<std::sync::atomic::AtomicBool>,
    stream: &str,
) -> io::Result<DarwinOutputReader> {
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    let reader_stop = Arc::clone(&stop);
    let thread = std::thread::Builder::new()
        .name(format!("tobkiri-darwin-{stream}-reader"))
        .spawn(move || {
            let capture = read_nonblocking_to_end(&mut file, &budget, &reader_stop);
            let _ = sender.send(capture);
        })?;
    Ok(DarwinOutputReader {
        receiver,
        thread: Some(thread),
        stop,
    })
}

#[cfg(target_os = "macos")]
fn read_nonblocking_to_end(
    file: &mut File,
    budget: &std::sync::atomic::AtomicUsize,
    stop: &std::sync::atomic::AtomicBool,
) -> DarwinCapturedStream {
    use std::sync::atomic::Ordering;
    let mut bytes = Vec::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        if stop.load(Ordering::Acquire) {
            return DarwinCapturedStream {
                bytes,
                error: Some("collector stopped before output pipe reached EOF".into()),
            };
        }
        match file.read(&mut buffer) {
            Ok(0) => return DarwinCapturedStream { bytes, error: None },
            Ok(count) => {
                if budget
                    .fetch_update(Ordering::AcqRel, Ordering::Acquire, |remaining| {
                        remaining.checked_sub(count)
                    })
                    .is_err()
                {
                    stop.store(true, Ordering::Release);
                    return DarwinCapturedStream {
                        bytes,
                        error: Some("Darwin child output exceeded capture limit".into()),
                    };
                }
                bytes.extend_from_slice(&buffer[..count]);
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                std::thread::sleep(std::time::Duration::from_millis(2));
            }
            Err(error) => {
                return DarwinCapturedStream {
                    bytes,
                    error: Some(error.to_string()),
                };
            }
        }
    }
}

#[cfg(target_os = "macos")]
fn poll_darwin_reader(
    reader: &mut Option<DarwinOutputReader>,
    deadline: std::time::Instant,
    stream: &str,
) -> DarwinReaderPoll {
    let Some(active) = reader.as_mut() else {
        return DarwinReaderPoll::Finished(DarwinCapturedStream::default());
    };
    let remaining = deadline.saturating_duration_since(std::time::Instant::now());
    match active.receiver.recv_timeout(remaining) {
        Ok(capture) => {
            let mut finished = reader.take().expect("active reader must remain present");
            match finished
                .thread
                .take()
                .expect("reader thread must exist")
                .join()
            {
                Ok(()) => DarwinReaderPoll::Finished(capture),
                Err(_) => DarwinReaderPoll::Failed(format!("{stream} reader panicked")),
            }
        }
        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => DarwinReaderPoll::Pending,
        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
            let mut finished = reader.take().expect("active reader must remain present");
            let _ = finished
                .thread
                .take()
                .expect("reader thread must exist")
                .join();
            DarwinReaderPoll::Failed(format!("{stream} reader disconnected"))
        }
    }
}

#[cfg(target_os = "macos")]
fn stop_darwin_reader(reader: &Option<DarwinOutputReader>) {
    use std::sync::atomic::Ordering;
    if let Some(reader) = reader {
        reader.stop.store(true, Ordering::Release);
    }
}

#[cfg(target_os = "macos")]
fn finish_darwin_capture(
    result: DarwinReaderPoll,
    stream: &str,
    errors: &mut Vec<String>,
) -> DarwinCapturedStream {
    match result {
        DarwinReaderPoll::Finished(capture) => {
            if let Some(error) = &capture.error {
                errors.push(format!("{stream} reader failed: {error}"));
            }
            capture
        }
        DarwinReaderPoll::Pending => {
            errors.push(format!("timed out joining {stream} reader"));
            DarwinCapturedStream::default()
        }
        DarwinReaderPoll::Failed(error) => {
            errors.push(error);
            DarwinCapturedStream::default()
        }
    }
}

#[cfg(target_os = "macos")]
fn format_darwin_diagnostics(stdout: &[u8], stderr: &[u8]) -> String {
    fn tail(bytes: &[u8]) -> String {
        let start = bytes.len().saturating_sub(DARWIN_OUTPUT_DIAGNOSTIC_BYTES);
        String::from_utf8_lossy(&bytes[start..])
            .chars()
            .flat_map(char::escape_default)
            .collect()
    }

    let mut diagnostics = String::new();
    if !stdout.is_empty() {
        diagnostics.push_str(&format!("; bounded stdout tail: {}", tail(stdout)));
    }
    if !stderr.is_empty() {
        diagnostics.push_str(&format!("; bounded stderr tail: {}", tail(stderr)));
    }
    diagnostics
}

#[cfg(target_os = "macos")]
impl Drop for DarwinChild {
    fn drop(&mut self) {
        if !matches!(
            self.state,
            DarwinChildState::ExternalReaped | DarwinChildState::Lost
        ) {
            if self.status.is_none() {
                let _ = self.kill_process_group();
            }
            let _ = self.wait_nonblocking_until(
                std::time::Instant::now() + std::time::Duration::from_secs(2),
            );
        }
        stop_darwin_reader(&self.stdout_reader);
        stop_darwin_reader(&self.stderr_reader);
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
        let _ = poll_darwin_reader(&mut self.stdout_reader, deadline, "stdout");
        let _ = poll_darwin_reader(&mut self.stderr_reader, deadline, "stderr");
    }
}

#[cfg(unix)]
fn sealed_executable_copy(
    source: &mut File,
    _original: &Path,
    expected: &str,
) -> io::Result<(PathBuf, Option<PathBuf>, fs::Metadata, bool)> {
    use std::os::unix::fs::PermissionsExt;
    use std::time::{SystemTime, UNIX_EPOCH};

    let base = env::temp_dir().canonicalize()?;
    for attempt in 0..128_u32 {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_nanos();
        let owner = base.join(format!(
            "tobkiri-verified-tool-{}-{nonce}-{attempt}",
            std::process::id()
        ));
        match fs::create_dir(&owner) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
        fs::set_permissions(&owner, fs::Permissions::from_mode(0o700))?;
        let target = owner.join("executable");
        match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&target)
        {
            Ok(mut output) => {
                source.seek(SeekFrom::Start(0))?;
                io::copy(source, &mut output)?;
                output.sync_all()?;
                fs::set_permissions(&target, fs::Permissions::from_mode(0o500))?;
                let metadata = fs::symlink_metadata(&target)?;
                let copied = fs::read(&target)?;
                if format!("{:x}", Sha256::digest(&copied)) != expected {
                    let _ = fs::remove_file(&target);
                    return Err(invalid("sealed packaging tool copy digest mismatch"));
                }
                fs::set_permissions(&owner, fs::Permissions::from_mode(0o500))?;
                return Ok((target, Some(owner), metadata, true));
            }
            Err(error) => {
                let _ = fs::remove_dir(&owner);
                return Err(error);
            }
        }
    }
    Err(invalid("could not create sealed packaging tool copy"))
}

#[cfg(target_os = "macos")]
fn macos_code_identity(path: &Path, allow_adhoc: bool) -> io::Result<Vec<u8>> {
    use std::os::unix::ffi::OsStrExt;
    use std::ptr;

    type CFTypeRef = *const std::ffi::c_void;
    type CFURLRef = *const std::ffi::c_void;
    type CFDictionaryRef = *const std::ffi::c_void;
    type SecStaticCodeRef = *const std::ffi::c_void;
    #[link(name = "CoreFoundation", kind = "framework")]
    unsafe extern "C" {
        fn CFURLCreateFromFileSystemRepresentation(
            allocator: CFTypeRef,
            bytes: *const u8,
            length: isize,
            is_directory: u8,
        ) -> CFURLRef;
        fn CFDictionaryGetValue(dictionary: CFDictionaryRef, key: CFTypeRef) -> CFTypeRef;
        fn CFNumberGetValue(number: CFTypeRef, kind: i32, value: *mut std::ffi::c_void) -> u8;
        fn CFDataGetLength(data: CFTypeRef) -> isize;
        fn CFDataGetBytePtr(data: CFTypeRef) -> *const u8;
        fn CFRelease(value: CFTypeRef);
    }
    #[link(name = "Security", kind = "framework")]
    unsafe extern "C" {
        static kSecCodeInfoFlags: CFTypeRef;
        static kSecCodeInfoUnique: CFTypeRef;
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
        fn SecCodeCopySigningInformation(
            code: SecStaticCodeRef,
            flags: u32,
            information: *mut CFDictionaryRef,
        ) -> i32;
    }
    let bytes = path.as_os_str().as_bytes();
    let url = unsafe {
        CFURLCreateFromFileSystemRepresentation(
            ptr::null(),
            bytes.as_ptr(),
            bytes.len() as isize,
            0,
        )
    };
    if url.is_null() {
        return Err(invalid("could not create macOS packaging tool URL"));
    }
    let mut code = ptr::null();
    let create = unsafe { SecStaticCodeCreateWithPath(url, 0, &mut code) };
    unsafe { CFRelease(url) };
    if create != 0 || code.is_null() {
        return Err(invalid("macOS packaging tool is unsigned"));
    }
    const STRICT_VALIDITY: u32 =
        SEC_CS_NO_NETWORK_ACCESS | SEC_CS_STRICT_VALIDATE | SEC_CS_CHECK_ALL_ARCHITECTURES;
    let validity = unsafe { SecStaticCodeCheckValidity(code, STRICT_VALIDITY, ptr::null()) };
    let mut information = ptr::null();
    let copied = unsafe { SecCodeCopySigningInformation(code, 1 << 1, &mut information) };
    unsafe { CFRelease(code) };
    if validity != 0 || copied != 0 || information.is_null() {
        return Err(invalid("macOS packaging tool signature is invalid"));
    }
    let flags_value = unsafe { CFDictionaryGetValue(information, kSecCodeInfoFlags) };
    let mut flags = 0_i64;
    let flags_ok = !flags_value.is_null()
        && unsafe {
            CFNumberGetValue(
                flags_value,
                4,
                (&mut flags as *mut i64).cast::<std::ffi::c_void>(),
            )
        } != 0;
    if !flags_ok || (!allow_adhoc && !accepted_macos_signature_flags(flags)) {
        unsafe { CFRelease(information) };
        return Err(invalid(
            "macOS packaging tool is ad-hoc or has unavailable signature flags",
        ));
    }
    let unique = unsafe { CFDictionaryGetValue(information, kSecCodeInfoUnique) };
    let length = if unique.is_null() {
        0
    } else {
        unsafe { CFDataGetLength(unique) }
    };
    if length <= 0 || length > 64 {
        unsafe { CFRelease(information) };
        return Err(invalid("macOS packaging tool CDHash is unavailable"));
    }
    let digest =
        unsafe { std::slice::from_raw_parts(CFDataGetBytePtr(unique), length as usize) }.to_vec();
    unsafe { CFRelease(information) };
    Ok(digest)
}

#[cfg(target_os = "macos")]
fn mode_writable_by_caller(
    owner: u32,
    group: u32,
    mode: u32,
    caller: u32,
    caller_groups: &[u32],
) -> bool {
    if owner == caller {
        mode & 0o200 != 0
    } else if caller_groups.contains(&group) {
        mode & 0o020 != 0
    } else {
        mode & 0o002 != 0
    }
}

#[cfg(target_os = "macos")]
fn current_process_groups() -> io::Result<Vec<u32>> {
    let count = unsafe { libc::getgroups(0, std::ptr::null_mut()) };
    if count < 0 {
        return Err(io::Error::last_os_error());
    }
    let mut raw = vec![0 as libc::gid_t; count as usize];
    if count > 0 && unsafe { libc::getgroups(count, raw.as_mut_ptr()) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let mut groups = raw
        .into_iter()
        .map(|group| group as u32)
        .collect::<Vec<_>>();
    let effective = unsafe { libc::getegid() } as u32;
    if !groups.contains(&effective) {
        groups.push(effective);
    }
    Ok(groups)
}

#[cfg(target_os = "macos")]
fn macos_fd_has_nontrivial_acl(file: &File) -> io::Result<bool> {
    use std::os::fd::AsRawFd;

    unsafe extern "C" {
        fn acl_get_fd_np(fd: libc::c_int, acl_type: libc::c_int) -> *mut std::ffi::c_void;
        fn acl_free(value: *mut std::ffi::c_void) -> libc::c_int;
    }
    unsafe { *libc::__error() = 0 };
    let acl = unsafe { acl_get_fd_np(file.as_raw_fd(), 0x0000_0100) };
    if acl.is_null() {
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ENOENT) {
            return Ok(false);
        }
        return Err(error);
    }
    let freed = unsafe { acl_free(acl) };
    if freed != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(true)
}

#[cfg(target_os = "macos")]
fn open_macos_authority_chain(
    root: &Path,
    caller: u32,
    caller_groups: &[u32],
) -> io::Result<Vec<File>> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::path::Component;

    let mut handles = vec![File::open("/")?];
    for component in root.components() {
        let Component::Normal(name) = component else {
            continue;
        };
        let name = CString::new(name.as_bytes())
            .map_err(|_| invalid("macOS Python authority component contains NUL"))?;
        let descriptor = unsafe {
            libc::openat(
                handles.last().expect("root authority handle").as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
            )
        };
        if descriptor < 0 {
            return Err(io::Error::last_os_error());
        }
        handles.push(unsafe { File::from_raw_fd(descriptor) });
    }
    for handle in &handles {
        let metadata = handle.metadata()?;
        if !metadata.is_dir()
            || metadata.uid() != 0
            || mode_writable_by_caller(
                metadata.uid(),
                metadata.gid(),
                metadata.permissions().mode(),
                caller,
                caller_groups,
            )
            || macos_fd_has_nontrivial_acl(handle)?
        {
            return Err(invalid(
                "macOS Python authority chain is writable, non-root, or has an ACL",
            ));
        }
    }
    Ok(handles)
}

#[cfg(target_os = "macos")]
fn macos_python_installation_lease(
    path: &Path,
) -> io::Result<(MacOSPythonInstallationLease, PathBuf)> {
    use std::os::unix::fs::OpenOptionsExt;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    let raw_root = env::var_os(PYTHON_SNAPSHOT_ENV)
        .ok_or_else(|| invalid(format!("{PYTHON_SNAPSHOT_ENV} is required for Python")))?;
    let root = PathBuf::from(raw_root);
    if !root.is_absolute() || root.canonicalize()? != root {
        return Err(invalid("macOS Python installation root is not canonical"));
    }
    let caller = unsafe { libc::geteuid() } as u32;
    let root_handle = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(&root)?;
    let before = root_handle.metadata()?;
    if !before.is_dir()
        || before.uid() != caller
        || before.permissions().mode() & 0o222 != 0
        || macos_fd_has_nontrivial_acl(&root_handle)?
    {
        return Err(invalid(
            "rootless macOS Python snapshot is writable, foreign-owned, or has an ACL",
        ));
    }
    let canonical_executable = path.canonicalize()?;
    if !path.starts_with(&root) || canonical_executable != path {
        return Err(invalid("macOS Python executable escapes its installation"));
    }
    let inventory = root.join("sealed-environment.v1.json");
    let inventory_handle = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(&inventory)?;
    let inventory_metadata = inventory_handle.metadata()?;
    if inventory_metadata.file_type().is_symlink()
        || !inventory_metadata.is_file()
        || inventory_metadata.uid() != caller
        || inventory_metadata.permissions().mode() & 0o222 != 0
        || inventory_metadata.nlink() != 1
        || macos_fd_has_nontrivial_acl(&inventory_handle)?
    {
        return Err(invalid("macOS Python inventory is not immutable"));
    }
    let inventory_sha256 = env::var(PYTHON_INVENTORY_SHA256_ENV).map_err(|_| {
        invalid(format!(
            "{PYTHON_INVENTORY_SHA256_ENV} is required for Python"
        ))
    })?;
    if !valid_raw_sha256(&inventory_sha256) {
        return Err(invalid(format!(
            "{PYTHON_INVENTORY_SHA256_ENV} must be lowercase raw SHA-256"
        )));
    }
    if file_identity(&fs::symlink_metadata(&root)?) != file_identity(&before) {
        return Err(invalid("macOS Python installation changed while leased"));
    }
    let lease = MacOSPythonInstallationLease {
        root,
        identity: file_identity(&before),
        inventory_sha256,
        _root_handle: root_handle,
        _inventory_handle: inventory_handle,
        snapshot_path: None,
    };
    lease.verify_unchanged()?;
    create_private_macos_python_snapshot(&lease, path)
}

#[cfg(target_os = "macos")]
fn create_private_macos_python_snapshot(
    source: &MacOSPythonInstallationLease,
    source_executable: &Path,
) -> io::Result<(MacOSPythonInstallationLease, PathBuf)> {
    use std::collections::BTreeSet;
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
    use std::time::{SystemTime, UNIX_EPOCH};

    source.verify_unchanged()?;
    let mut inventory = source._inventory_handle.try_clone()?;
    inventory.seek(SeekFrom::Start(0))?;
    let document: serde_json::Value = serde_json::from_reader(inventory)
        .map_err(|error| invalid(format!("sealed Python manifest is invalid: {error}")))?;
    let files = document
        .get("files")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| invalid("sealed Python manifest files are missing"))?;
    let executable_relative = source_executable
        .strip_prefix(&source.root)
        .map_err(|_| invalid("packaging Python executable escapes source snapshot"))?
        .to_path_buf();
    let base = env::temp_dir().canonicalize()?;
    let mut snapshot_path = None;
    for attempt in 0..128_u32 {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_nanos();
        let candidate = base.join(format!(
            ".tobkiri-packaging-python-{}-{nonce}-{attempt}",
            std::process::id()
        ));
        let mut builder = fs::DirBuilder::new();
        builder.mode(0o700);
        match builder.create(&candidate) {
            Ok(()) => {
                snapshot_path = Some(candidate);
                break;
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    let snapshot_path = snapshot_path
        .ok_or_else(|| invalid("could not allocate private packaging Python snapshot"))?;
    let result = (|| {
        let destination_root = fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&snapshot_path)?;
        copy_macos_snapshot_file(
            source._root_handle.as_raw_fd(),
            destination_root.as_raw_fd(),
            Path::new("sealed-environment.v1.json"),
            false,
        )?;
        let mut directories = BTreeSet::new();
        for entry in files {
            let relative = entry
                .get("path")
                .and_then(serde_json::Value::as_str)
                .ok_or_else(|| invalid("sealed Python inventory path is missing"))?;
            let executable = entry
                .get("executable")
                .and_then(serde_json::Value::as_bool)
                .ok_or_else(|| invalid("sealed Python executable flag is missing"))?;
            let relative = Path::new(relative);
            copy_macos_snapshot_file(
                source._root_handle.as_raw_fd(),
                destination_root.as_raw_fd(),
                relative,
                executable,
            )?;
            let mut parent = relative.parent();
            while let Some(value) = parent.filter(|value| !value.as_os_str().is_empty()) {
                directories.insert(value.to_path_buf());
                parent = value.parent();
            }
        }
        for relative in directories.iter().rev() {
            let directory = open_macos_snapshot_directory(destination_root.as_raw_fd(), relative)?;
            if unsafe { libc::fchmod(directory.as_raw_fd(), 0o500) } != 0 {
                return Err(io::Error::last_os_error());
            }
        }
        if unsafe { libc::fchmod(destination_root.as_raw_fd(), 0o500) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let metadata = destination_root.metadata()?;
        let inventory_path = snapshot_path.join("sealed-environment.v1.json");
        let inventory_handle = fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&inventory_path)?;
        let lease = MacOSPythonInstallationLease {
            root: snapshot_path.clone(),
            identity: file_identity(&metadata),
            inventory_sha256: source.inventory_sha256.clone(),
            _root_handle: destination_root,
            _inventory_handle: inventory_handle,
            snapshot_path: Some(snapshot_path.clone()),
        };
        lease.verify_unchanged()?;
        source.verify_unchanged()?;
        let private_executable = snapshot_path.join(&executable_relative);
        let executable_metadata = fs::symlink_metadata(&private_executable)?;
        if !executable_metadata.is_file()
            || executable_metadata.file_type().is_symlink()
            || executable_metadata.permissions().mode() & 0o111 == 0
        {
            return Err(invalid("private packaging Python executable is invalid"));
        }
        Ok((lease, private_executable))
    })();
    if result.is_err() {
        // Construction residue is retained if its private name cannot be
        // proven. A later run never adopts an existing name.
        let _ = cleanup_private_macos_python_snapshot(&snapshot_path, None);
    }
    result
}

#[cfg(target_os = "macos")]
fn copy_macos_snapshot_file(
    source_root: std::os::fd::RawFd,
    destination_root: std::os::fd::RawFd,
    relative: &Path,
    executable: bool,
) -> io::Result<()> {
    use std::ffi::CString;
    use std::io::Write;
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;
    use std::path::Component;

    if relative.is_absolute()
        || relative
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err(invalid("private Python snapshot path is unsafe"));
    }
    let components = relative.components().collect::<Vec<_>>();
    let mut source_directory = duplicate_macos_fd(source_root)?;
    let mut destination_directory = duplicate_macos_fd(destination_root)?;
    for component in &components[..components.len().saturating_sub(1)] {
        let Component::Normal(name) = component else {
            return Err(invalid("private Python snapshot component is unsafe"));
        };
        source_directory = open_macos_child_directory(source_directory, name, false)?;
        destination_directory = open_macos_child_directory(destination_directory, name, true)?;
    }
    let name = components
        .last()
        .and_then(|component| match component {
            Component::Normal(name) => Some(name),
            _ => None,
        })
        .ok_or_else(|| invalid("private Python snapshot file path is empty"))?;
    let name = CString::new(name.as_bytes()).map_err(|_| invalid("snapshot path contains NUL"))?;
    let source_fd = unsafe {
        libc::openat(
            source_directory.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if source_fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let destination_fd = unsafe {
        libc::openat(
            destination_directory.as_raw_fd(),
            name.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            if executable { 0o500 } else { 0o400 },
        )
    };
    if destination_fd < 0 {
        unsafe { libc::close(source_fd) };
        return Err(io::Error::last_os_error());
    }
    let mut source = unsafe { File::from_raw_fd(source_fd) };
    let mut destination = unsafe { File::from_raw_fd(destination_fd) };
    let before = source.metadata()?;
    if !before.is_file() || before.nlink() != 1 {
        return Err(invalid("private Python source is not a singly-linked file"));
    }
    io::copy(&mut source, &mut destination)?;
    destination.flush()?;
    destination.sync_all()?;
    let after = source.metadata()?;
    if file_identity(&before) != file_identity(&after) {
        return Err(invalid("private Python source changed during copy"));
    }
    drop(unsafe { OwnedFd::from_raw_fd(source_directory) });
    drop(unsafe { OwnedFd::from_raw_fd(destination_directory) });
    Ok(())
}

#[cfg(target_os = "macos")]
fn duplicate_macos_fd(fd: std::os::fd::RawFd) -> io::Result<std::os::fd::RawFd> {
    let duplicate = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) };
    if duplicate < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(duplicate)
}

#[cfg(target_os = "macos")]
fn open_macos_child_directory(
    parent: std::os::fd::RawFd,
    name: &std::ffi::OsStr,
    create: bool,
) -> io::Result<std::os::fd::RawFd> {
    use std::ffi::CString;
    use std::os::fd::{FromRawFd, OwnedFd};
    use std::os::unix::ffi::OsStrExt;

    let name = CString::new(name.as_bytes()).map_err(|_| invalid("snapshot path contains NUL"))?;
    if create {
        let result = unsafe { libc::mkdirat(parent, name.as_ptr(), 0o700) };
        if result != 0 && io::Error::last_os_error().raw_os_error() != Some(libc::EEXIST) {
            drop(unsafe { OwnedFd::from_raw_fd(parent) });
            return Err(io::Error::last_os_error());
        }
    }
    let child = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    drop(unsafe { OwnedFd::from_raw_fd(parent) });
    if child < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(child)
}

#[cfg(target_os = "macos")]
fn open_macos_snapshot_directory(root: std::os::fd::RawFd, relative: &Path) -> io::Result<File> {
    use std::os::fd::{FromRawFd, OwnedFd};
    use std::path::Component;

    let mut directory = duplicate_macos_fd(root)?;
    for component in relative.components() {
        let Component::Normal(name) = component else {
            drop(unsafe { OwnedFd::from_raw_fd(directory) });
            return Err(invalid("snapshot directory path is unsafe"));
        };
        directory = open_macos_child_directory(directory, name, false)?;
    }
    Ok(unsafe { File::from_raw_fd(directory) })
}

#[cfg(windows)]
fn locked_windows_executable(path: &Path, expected: &str) -> io::Result<File> {
    use std::mem::{size_of, MaybeUninit};
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::io::FromRawHandle;
    use windows_sys::Win32::Foundation::{GENERIC_READ, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, FileAttributeTagInfo, FileStandardInfo, GetFileInformationByHandleEx,
        FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_NORMAL, FILE_ATTRIBUTE_REPARSE_POINT,
        FILE_ATTRIBUTE_TAG_INFO, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ, FILE_STANDARD_INFO,
        OPEN_EXISTING,
    };

    let wide = path
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let handle = unsafe {
        CreateFileW(
            wide.as_ptr(),
            GENERIC_READ,
            FILE_SHARE_READ,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
            std::ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    let mut file = unsafe { File::from_raw_handle(handle) };
    let mut attributes = MaybeUninit::<FILE_ATTRIBUTE_TAG_INFO>::zeroed();
    if unsafe {
        GetFileInformationByHandleEx(
            handle,
            FileAttributeTagInfo,
            attributes.as_mut_ptr().cast(),
            size_of::<FILE_ATTRIBUTE_TAG_INFO>() as u32,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    let attributes = unsafe { attributes.assume_init() };
    if attributes.FileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY) != 0 {
        return Err(invalid(
            "Windows packaging tool is a reparse point or directory",
        ));
    }
    let mut standard = MaybeUninit::<FILE_STANDARD_INFO>::zeroed();
    if unsafe {
        GetFileInformationByHandleEx(
            handle,
            FileStandardInfo,
            standard.as_mut_ptr().cast(),
            size_of::<FILE_STANDARD_INFO>() as u32,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    if unsafe { standard.assume_init() }.NumberOfLinks != 1 {
        return Err(invalid("Windows packaging tool is hardlinked"));
    }
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    file.seek(SeekFrom::Start(0))?;
    if format!("{:x}", digest.finalize()) != expected {
        return Err(invalid(
            "packaging tool changed before Windows lock acquisition",
        ));
    }
    Ok(file)
}

fn binding(kind: &str) -> (&'static str, &'static str) {
    match kind {
        "python" => (PYTHON_PATH_ENV, PYTHON_SHA256_ENV),
        "git" => (GIT_PATH_ENV, GIT_SHA256_ENV),
        _ => ("", ""),
    }
}

/// Resolve and verify one formally bound packaging executable.
pub fn verified_tool(kind: &str) -> io::Result<VerifiedTool> {
    let (path_key, digest_key) = binding(kind);
    if path_key.is_empty() {
        return Err(invalid(format!("unknown packaging tool: {kind}")));
    }
    let raw_path = env::var_os(path_key)
        .ok_or_else(|| invalid(format!("{path_key} is required; PATH lookup is forbidden")))?;
    let path = PathBuf::from(raw_path);
    if !path.is_absolute() {
        return Err(invalid(format!("{path_key} must be an absolute path")));
    }
    let expected = env::var(digest_key)
        .map_err(|_| invalid(format!("{digest_key} is required for {path_key}")))?;
    if !valid_raw_sha256(&expected) {
        return Err(invalid(format!(
            "{digest_key} must be lowercase raw SHA-256"
        )));
    }
    verify_tool_binding_guard(kind, &path, &expected)
}

#[cfg(test)]
pub fn verified_tool_executable(kind: &str) -> io::Result<PathBuf> {
    Ok(verified_tool(kind)?.original_path().to_path_buf())
}

#[cfg(test)]
fn verify_tool_binding(kind: &str, path: &Path, expected: &str) -> io::Result<PathBuf> {
    Ok(verify_tool_binding_guard(kind, path, expected)?
        .original_path()
        .to_path_buf())
}

fn verify_tool_binding_guard(kind: &str, path: &Path, expected: &str) -> io::Result<VerifiedTool> {
    let (_, digest_key) = binding(kind);
    if digest_key.is_empty() {
        return Err(invalid(format!("unknown packaging tool: {kind}")));
    }
    if !path.is_absolute() {
        return Err(invalid(format!("{kind} executable path must be absolute")));
    }
    if !valid_raw_sha256(expected) {
        return Err(invalid(format!(
            "{digest_key} must be lowercase raw SHA-256"
        )));
    }
    #[cfg(target_os = "macos")]
    if kind == "git" {
        verify_macos_git_path_authority(path)?;
    }
    #[allow(unused_mut)]
    let (mut file, metadata, actual) = open_hashed_regular_executable(path)?;
    if actual != expected {
        return Err(invalid(format!(
            "{kind} executable digest mismatch: expected {expected}, got {actual}"
        )));
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    let (execution_path, execution_owner, execution_metadata, owns_execution_copy) =
        sealed_executable_copy(&mut file, path, expected)?;
    #[cfg(target_os = "macos")]
    let (
        execution_path,
        execution_owner,
        execution_metadata,
        owns_execution_copy,
        macos_cdhash,
        python_installation,
    ) = {
        let (installation, private_python) = if kind == "python" {
            let (lease, executable) = macos_python_installation_lease(path)?;
            (Some(Arc::new(lease)), Some(executable))
        } else {
            (None, None)
        };
        let execution = private_python.as_deref().unwrap_or(path);
        let execution_metadata = fs::symlink_metadata(execution)?;
        let cdhash = macos_code_identity(execution, installation.is_some())?;
        (
            execution.to_path_buf(),
            None,
            execution_metadata,
            false,
            cdhash,
            installation,
        )
    };
    #[cfg(target_os = "macos")]
    drop(file);
    #[cfg(windows)]
    let locked_file = {
        drop(file);
        locked_windows_executable(path, expected)?
    };
    #[cfg(all(not(unix), not(windows)))]
    let locked_file = file;
    #[cfg(unix)]
    let execution_lock = File::open(&execution_path)?;
    Ok(VerifiedTool {
        kind: kind.to_owned(),
        original_path: path.to_path_buf(),
        identity: file_identity(&metadata),
        #[cfg(unix)]
        execution_path,
        #[cfg(unix)]
        execution_owner,
        #[cfg(unix)]
        execution_identity: file_identity(&execution_metadata),
        #[cfg(unix)]
        owns_execution_copy,
        #[cfg(target_os = "macos")]
        macos_cdhash,
        #[cfg(target_os = "macos")]
        python_installation,
        lock: {
            #[cfg(unix)]
            {
                execution_lock
            }
            #[cfg(not(unix))]
            {
                locked_file
            }
        },
    })
}

#[cfg(target_os = "macos")]
fn verify_macos_git_path_authority(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    const FORMAL_GIT: &str = "/Library/Developer/CommandLineTools/usr/bin/git";
    if path != Path::new(FORMAL_GIT) || path.canonicalize()? != path {
        return Err(invalid(format!(
            "formal macOS Git must be the fixed Command Line Tools executable: {FORMAL_GIT}"
        )));
    }
    for component in path.ancestors() {
        let metadata = fs::symlink_metadata(component)?;
        if metadata.file_type().is_symlink()
            || metadata.uid() != 0
            || metadata.permissions().mode() & 0o022 != 0
        {
            return Err(invalid(format!(
                "formal macOS Git contains writable/non-root authority: {}",
                component.display()
            )));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "macos")]
    #[test]
    fn darwin_security_constants_and_adhoc_policy_are_fixed() {
        assert_eq!(SEC_CS_NO_NETWORK_ACCESS, 1 << 29);
        assert_eq!(SEC_CS_STRICT_VALIDATE, 1 << 4);
        assert_eq!(SEC_CS_CHECK_ALL_ARCHITECTURES, 1);
        assert_eq!(SEC_CODE_SIGNATURE_ADHOC, 0x2);
        assert!(!accepted_macos_signature_flags(SEC_CODE_SIGNATURE_ADHOC));
        assert!(accepted_macos_signature_flags(0));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn darwin_ancestor_write_authority_uses_effective_group_membership() {
        assert!(!mode_writable_by_caller(0, 0, 0o775, 501, &[20, 80]));
        assert!(mode_writable_by_caller(0, 0, 0o775, 501, &[0, 20]));
        assert!(mode_writable_by_caller(501, 0, 0o755, 501, &[20]));
        assert!(mode_writable_by_caller(0, 0, 0o757, 501, &[20]));
    }

    #[cfg(target_os = "macos")]
    fn spawn_darwin_test_shell(script: &str) -> DarwinChild {
        let mut child = spawn_suspended_darwin(
            Path::new("/bin/sh"),
            &[
                std::ffi::OsString::from("-c"),
                std::ffi::OsString::from(script),
            ],
            &std::collections::BTreeMap::new(),
            None,
            true,
        )
        .expect("test shell should spawn suspended");
        assert_eq!(unsafe { libc::getpgid(child.pid) }, child.pid);
        child
            .start_output_readers()
            .expect("test output readers should start before resume");
        assert_eq!(unsafe { libc::kill(child.pid, libc::SIGCONT) }, 0);
        child
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn darwin_output_collector_captures_success_from_real_spawn_path() {
        let child = spawn_darwin_test_shell("printf success; printf diagnostic >&2");
        let output = child
            .wait_with_output_until(
                std::time::Instant::now() + std::time::Duration::from_secs(5),
                std::time::Duration::from_secs(1),
            )
            .expect("short command should complete");
        assert!(output.status.success());
        assert_eq!(output.stdout, b"success");
        assert_eq!(output.stderr, b"diagnostic");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn darwin_output_collector_drains_large_stdout_and_stderr_without_backpressure() {
        let child = spawn_darwin_test_shell(
            "(/bin/dd if=/dev/zero bs=1048576 count=4 2>/dev/null) & \
             (/bin/dd if=/dev/zero bs=1048576 count=4 1>&2 2>/dev/null) & wait",
        );
        let output = child
            .wait_with_output_until(
                std::time::Instant::now() + std::time::Duration::from_secs(10),
                std::time::Duration::from_secs(1),
            )
            .expect("large concurrent output should not deadlock");
        assert!(output.status.success());
        assert_eq!(output.stdout.len(), 4 * 1024 * 1024);
        assert_eq!(output.stderr.len(), 4 * 1024 * 1024);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn darwin_output_collector_contains_descendant_that_holds_pipe_open() {
        let child = spawn_darwin_test_shell("sleep 60 & printf parent-complete");
        let process_group = child.process_group;
        let started = std::time::Instant::now();
        let error = child
            .wait_with_output_until(
                std::time::Instant::now() + std::time::Duration::from_secs(5),
                std::time::Duration::from_millis(200),
            )
            .expect_err("inherited descendant pipe must fail closed");
        assert!(started.elapsed() < std::time::Duration::from_secs(3));
        assert!(error.to_string().contains("kept output pipes open"));
        assert!(error.to_string().contains("parent-complete"));
        assert_eq!(unsafe { libc::kill(-process_group, 0) }, -1);
        assert_eq!(io::Error::last_os_error().raw_os_error(), Some(libc::ESRCH));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn darwin_output_collector_kills_and_reaps_hung_process_group() {
        let child = spawn_darwin_test_shell("while :; do sleep 1; done");
        let process_group = child.process_group;
        let started = std::time::Instant::now();
        let error = child
            .wait_with_output_until(
                std::time::Instant::now() + std::time::Duration::from_millis(200),
                std::time::Duration::from_millis(200),
            )
            .expect_err("hung process group must time out");
        assert!(started.elapsed() < std::time::Duration::from_secs(3));
        assert!(error
            .to_string()
            .contains("timed out waiting for Darwin child"));
        assert_eq!(unsafe { libc::kill(-process_group, 0) }, -1);
        assert_eq!(io::Error::last_os_error().raw_os_error(), Some(libc::ESRCH));
    }

    #[test]
    fn sealed_reseal_budget_is_work_bound_capped_and_overflow_safe() {
        let budget = VerifiedOutputBudget::sealed_python_reseal(442 * 1024 * 1024, 12_000)
            .expect("verified 442 MiB inventory should receive a reseal budget");
        assert!(budget.duration > std::time::Duration::from_secs(30));
        assert!(budget.duration <= std::time::Duration::from_secs(SEALED_RESEAL_MAX_SECONDS));
        assert!(VerifiedOutputBudget::sealed_python_reseal(0, 1).is_err());
        assert!(VerifiedOutputBudget::sealed_python_reseal(1, 0).is_err());
        assert!(VerifiedOutputBudget::sealed_python_reseal(u64::MAX, 1).is_err());
        assert!(VerifiedOutputBudget::sealed_python_reseal(1, u64::MAX).is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn darwin_442_mib_work_budget_allows_bounded_slow_progress() {
        let budget = VerifiedOutputBudget::sealed_python_reseal(442 * 1024 * 1024, 12_000)
            .expect("verified 442 MiB inventory should receive a reseal budget");
        let child = spawn_darwin_test_shell(
            "i=0; while test $i -lt 12; do printf x; /bin/sleep 0.05; i=$((i + 1)); done",
        );
        let output = child
            .wait_with_output_until(
                std::time::Instant::now() + budget.duration,
                std::time::Duration::from_millis(200),
            )
            .expect("bounded slow progress should finish inside its work budget");
        assert!(output.status.success());
        assert_eq!(output.stdout, b"xxxxxxxxxxxx");
    }
    use std::time::{SystemTime, UNIX_EPOCH};

    struct TestFile {
        path: PathBuf,
    }

    impl TestFile {
        fn new(label: &str, payload: &[u8]) -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system clock should be valid")
                .as_nanos();
            let root = env::temp_dir()
                .canonicalize()
                .expect("system temporary directory should canonicalize")
                .join(format!(
                    "tobkiri-packaging-tool-{label}-{}-{nonce}",
                    std::process::id()
                ));
            fs::create_dir_all(&root).expect("tool fixture root should be creatable");
            let path = root.join("tool");
            fs::write(&path, payload).expect("tool fixture should be writable");
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;

                fs::set_permissions(&path, fs::Permissions::from_mode(0o555))
                    .expect("tool fixture should be executable");
            }
            Self { path }
        }

        fn digest(&self) -> String {
            format!(
                "{:x}",
                Sha256::digest(fs::read(&self.path).expect("tool fixture should be readable"))
            )
        }
    }

    impl Drop for TestFile {
        fn drop(&mut self) {
            if let Some(root) = self.path.parent() {
                let _ = fs::remove_dir_all(root);
            }
        }
    }

    #[test]
    fn missing_and_nonabsolute_bindings_fail_before_any_spawn() {
        let missing = verify_tool_binding("python", Path::new("/missing"), "")
            .expect_err("missing digest input must fail");
        assert!(missing.to_string().contains(PYTHON_SHA256_ENV));

        let relative = verify_tool_binding("python", Path::new("python"), &"0".repeat(64))
            .expect_err("relative input must fail");
        assert!(relative.to_string().contains("absolute"));
    }

    #[test]
    fn fake_path_tools_are_never_selected_or_executed() {
        let fake = TestFile::new(
            "fake-path",
            b"#!/bin/sh\nprintf executed > \"$TOBKIRI_FAKE_MARKER\"\n",
        );
        let marker = fake.path.with_file_name("marker");
        let error = verify_tool_binding("python", &fake.path, &"0".repeat(64))
            .expect_err("fake tool digest must fail");
        assert!(error.to_string().contains("digest mismatch"));
        assert!(!marker.exists(), "untrusted PATH executable was spawned");
    }

    #[test]
    fn mismatch_lookalike_and_tamper_fail_closed_after_binding() {
        let tool = TestFile::new("tamper", b"trusted fixture executable");
        let mismatch = verify_tool_binding("python", &tool.path, &"0".repeat(64))
            .expect_err("digest mismatch must fail");
        assert!(mismatch.to_string().contains("digest mismatch"));

        let expected = tool.digest();
        #[cfg(target_os = "macos")]
        {
            let lookalike = verify_tool_binding("python", &tool.path, &expected)
                .expect_err("a digest-matching path outside the sealed root must fail");
            assert!(lookalike.to_string().contains("escapes its installation"));
        }
        #[cfg(not(target_os = "macos"))]
        assert_eq!(
            verify_tool_binding("python", &tool.path, &expected)
                .expect("exact tool identity should pass"),
            tool.path
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;

            fs::set_permissions(&tool.path, fs::Permissions::from_mode(0o755))
                .expect("fixture should become writable");
        }
        fs::write(&tool.path, b"tampered fixture executable")
            .expect("fixture tamper should be writable");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;

            fs::set_permissions(&tool.path, fs::Permissions::from_mode(0o555))
                .expect("fixture should become immutable again");
        }
        let tampered =
            verify_tool_binding("python", &tool.path, &expected).expect_err("tamper must fail");
        assert!(tampered.to_string().contains("digest mismatch"));
    }

    #[cfg(all(unix, not(target_os = "macos")))]
    #[test]
    fn replaced_python_and_git_paths_never_execute_replacement() {
        use std::os::unix::fs::PermissionsExt;

        for kind in ["python", "git"] {
            let tool = TestFile::new(
                &format!("{kind}-replace"),
                b"#!/bin/sh\nprintf trusted > \"$TOBKIRI_TRUSTED_MARKER\"\n",
            );
            let trusted_marker = tool.path.with_file_name(format!("{kind}-trusted"));
            let evil_marker = tool.path.with_file_name(format!("{kind}-evil"));
            let guard = verify_tool_binding_guard(kind, &tool.path, &tool.digest()).unwrap();
            fs::remove_file(&tool.path).unwrap();
            fs::write(
                &tool.path,
                b"#!/bin/sh\nprintf evil > \"$TOBKIRI_EVIL_MARKER\"\n",
            )
            .unwrap();
            fs::set_permissions(&tool.path, fs::Permissions::from_mode(0o555)).unwrap();
            let status = guard
                .command()
                .unwrap()
                .env("TOBKIRI_TRUSTED_MARKER", &trusted_marker)
                .env("TOBKIRI_EVIL_MARKER", &evil_marker)
                .status()
                .unwrap();
            assert!(status.success());
            assert!(trusted_marker.exists(), "trusted {kind} bytes must execute");
            assert!(
                !evil_marker.exists(),
                "replacement {kind} must never execute"
            );
        }
    }

    #[cfg(all(unix, not(target_os = "macos")))]
    #[test]
    fn in_place_overwrite_after_binding_never_executes_modified_original() {
        use std::os::unix::fs::PermissionsExt;
        let tool = TestFile::new(
            "in-place",
            b"#!/bin/sh\nprintf trusted > \"$TOBKIRI_TRUSTED_MARKER\"\n",
        );
        let trusted = tool.path.with_file_name("in-place-trusted");
        let evil = tool.path.with_file_name("in-place-evil");
        let guard = verify_tool_binding_guard("python", &tool.path, &tool.digest()).unwrap();
        fs::set_permissions(&tool.path, fs::Permissions::from_mode(0o755)).unwrap();
        fs::write(
            &tool.path,
            b"#!/bin/sh\nprintf evil > \"$TOBKIRI_EVIL_MARKER\"\n",
        )
        .unwrap();
        let status = guard
            .command()
            .unwrap()
            .env("TOBKIRI_TRUSTED_MARKER", &trusted)
            .env("TOBKIRI_EVIL_MARKER", &evil)
            .status()
            .unwrap();
        assert!(status.success());
        assert!(trusted.exists());
        assert!(!evil.exists());
    }

    #[test]
    fn bound_real_python_and_git_remain_executable() {
        let python = verified_tool("python").unwrap();
        let output = python
            .command()
            .unwrap()
            .args(["-c", "import encodings,sys; print(sys.prefix)"])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "sealed Python must import its relocated standard library"
        );

        let git = verified_tool("git").unwrap();
        let output = git.command().unwrap().arg("--version").output().unwrap();
        assert!(output.status.success(), "sealed Git must remain compatible");
    }
}
