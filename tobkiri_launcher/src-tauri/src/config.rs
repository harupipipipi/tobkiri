//! Path resolution and application configuration for Tauri.
//!
//! All paths are derived from Tauri's `resource_dir` and `app_data_dir`
//! so that the application works correctly when bundled.

use anyhow::{bail, Context, Result};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};

const UV_PATH_ENV: &str = "RUMI_UV_PATH";
const LOCAL_DEV_WORKSPACE_BUILD: Option<&str> = option_env!("TOBKIRI_LOCAL_DEV_WORKSPACE");

/// Central configuration resolved from Tauri path APIs.
#[derive(Debug, Clone)]
pub struct AppConfig {
    /// `{resource_dir}/app` — bundled tobkiri_runtime contents (= kernel root).
    pub app_dir: PathBuf,
    /// Same as `app_dir` — the Kernel's working directory.
    pub rumi_home: PathBuf,
    /// `{app_data_dir}/python` — PBS standalone Python.
    pub python_dir: PathBuf,
    /// Legacy app-data `uv` path retained for diagnostics and migration state.
    pub uv_path: PathBuf,
    /// `{app_data_dir}/venv` — Python virtual-environment.
    pub venv_dir: PathBuf,
    /// `{app_data_dir}/user_data` — persistent user data.
    pub user_data_dir: PathBuf,
    /// `{app_data_dir}/logs` — log files.
    pub log_dir: PathBuf,
    /// Kernel HTTP port (default 8765).
    pub kernel_port: u16,
    /// Repo root when running against a development checkout.
    pub dev_workspace_root: Option<PathBuf>,
}

impl AppConfig {
    /// Detect configuration from Tauri-provided directories.
    ///
    /// Layout:
    /// ```text
    /// {resource_dir}/
    /// └── app/               ← bundled tobkiri_runtime contents
    ///     ├── app.py
    ///     ├── core_runtime/
    ///     ├── requirements.txt
    ///     └── bundled/
    ///         └── uv(.exe)   ← optional pre-bundled uv
    ///
    /// {app_data_dir}/
    /// ├── python/
    /// ├── uv(.exe)
    /// ├── venv/
    /// ├── user_data/
    /// └── logs/
    /// ```
    pub fn detect_for_tauri(resource_dir: PathBuf, app_data_dir: PathBuf) -> Result<Self> {
        let staged_app_dir = resource_dir.join("app");
        let detected_workspace_root = find_dev_workspace_root(&resource_dir);
        // A direct Cargo executable uses the checkout runtime. A debug `.app`
        // uses its staged Resources/app tree or checkout-local gen/app stage.
        let is_debug_artifact = cfg!(debug_assertions)
            && detected_workspace_root.is_some()
            && (is_cargo_debug_resource_dir(&resource_dir)
                || resource_dir.ancestors().any(|ancestor| {
                    ancestor.file_name().and_then(|name| name.to_str()) == Some("target")
                }));
        let is_app_bundle = resource_dir
            .ancestors()
            .any(|ancestor| ancestor.extension().and_then(|value| value.to_str()) == Some("app"));
        let development_bundle = detected_workspace_root.as_ref().and_then(|workspace_root| {
            debug_macos_bundle_for_resources(
                &workspace_root.join("tobkiri_launcher/src-tauri/target"),
                &resource_dir,
            )
        });
        let generated_development_runtime = detected_workspace_root
            .as_ref()
            .map(|workspace_root| workspace_root.join("tobkiri_launcher/src-tauri/gen/app"));
        // The explicit local configuration runs the checkout runtime even if
        // a prior build left Resources/app or global gen/app behind. Other
        // builds keep their bundled Resources/app runtime boundary.
        let prefer_dev_runtime = (is_debug_artifact && !is_app_bundle)
            || is_explicit_local_development_workspace_build();
        let dev_workspace_root = if is_debug_artifact {
            detected_workspace_root.clone()
        } else if staged_app_dir.exists() {
            None
        } else {
            detected_workspace_root.clone()
        };

        let mut app_dir = staged_app_dir;
        if is_debug_artifact && development_bundle.is_some() && !app_dir.exists() {
            if let Some(staged_development_runtime) = generated_development_runtime {
                if staged_development_runtime.join("app.py").is_file() {
                    app_dir = staged_development_runtime;
                }
            }
        }
        if prefer_dev_runtime {
            let workspace_root = dev_workspace_root
                .as_ref()
                .context("debug runtime has no workspace root")?;
            let candidate = workspace_root.join("tobkiri_runtime");
            if candidate.join("app.py").exists() {
                app_dir = candidate;
            }
        }
        let rumi_home = app_dir.clone();

        // An unsigned checkout must never consume the installed application's
        // activation. Reuse development data across restarts of the same
        // executable, but keep changed executables away from earlier macOS
        // MACL-bound files.
        let writable_root = match dev_workspace_root.as_ref() {
            Some(workspace_root) => {
                development_writable_root(workspace_root, &std::env::current_exe()?)?
            }
            None => app_data_dir,
        };

        let python_dir = writable_root.join("python");
        let uv_path = if cfg!(target_os = "windows") {
            writable_root.join("uv.exe")
        } else {
            writable_root.join("uv")
        };
        let venv_dir = dev_workspace_root
            .as_ref()
            .map(|workspace_root| development_venv_dir(&app_dir, workspace_root, is_app_bundle))
            .unwrap_or_else(|| writable_root.join("venv"));
        let user_data_dir = writable_root.join("user_data");
        let log_dir = writable_root.join("logs");

        Ok(Self {
            app_dir,
            rumi_home,
            python_dir,
            uv_path,
            venv_dir,
            user_data_dir,
            log_dir,
            kernel_port: 8765,
            dev_workspace_root,
        })
    }

    /// Rebase every writable Viewer runtime path into an owned state root.
    ///
    /// Debug parallel instances use this after validating their supervisor
    /// root. Packaged runtimes keep their managed Python environment together
    /// with logs, secrets, and user data. A development workspace instead
    /// reads its already-validated repository venv: creating or provisioning
    /// an isolated replacement would both duplicate dependencies and violate
    /// the development Python policy.
    pub fn isolate_writable_state(&mut self, state_root: &Path, user_data_dir: PathBuf) {
        self.python_dir = state_root.join("python");
        self.uv_path = state_root.join(uv_binary_name());
        if !self.is_dev_workspace() {
            self.venv_dir = state_root.join("venv");
        }
        self.user_data_dir = user_data_dir;
        self.log_dir = state_root.join("logs");
    }

    /// Return the path to the Python binary inside the PBS directory.
    pub fn python_bin(&self) -> PathBuf {
        if cfg!(target_os = "windows") {
            self.python_dir.join("python.exe")
        } else {
            self.python_dir.join("bin").join("python3")
        }
    }

    /// Return the path to the Python binary inside the venv.
    pub fn venv_python(&self) -> PathBuf {
        if cfg!(target_os = "windows") {
            self.venv_dir.join("Scripts").join("python.exe")
        } else {
            self.venv_dir.join("bin").join("python3")
        }
    }

    /// Return the `requirements.txt` path.
    pub fn requirements_txt(&self) -> PathBuf {
        self.rumi_home.join("requirements.txt")
    }

    /// Return the persisted panel bootstrap secret path inside app data.
    pub fn panel_bootstrap_secret_path(&self) -> PathBuf {
        let state_root = self
            .user_data_dir
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| self.user_data_dir.clone());

        // macOS attaches a MACL to files created by an app process. That ACL
        // is bound to the current code identity, so a newly rebuilt unsigned
        // development app can block while opening the previous build's
        // secret. A debug secret only coordinates children of this launcher.
        #[cfg(debug_assertions)]
        {
            return state_root.join(format!(
                ".rumi_panel_bootstrap_secret.{}",
                std::process::id()
            ));
        }

        #[cfg(not(debug_assertions))]
        state_root.join(".rumi_panel_bootstrap_secret")
    }

    /// Return the directory where Viewer host broker files are stored.
    pub fn host_broker_dir(&self) -> PathBuf {
        self.user_data_dir.join("host_broker")
    }

    /// Return the path to the Viewer host broker connection file.
    pub fn host_broker_connection_path(&self) -> PathBuf {
        if let Some(path) = std::env::var_os("RUMI_VIEWER_HOST_BROKER_CONNECTION") {
            if !path.is_empty() {
                return PathBuf::from(path);
            }
        }
        self.host_broker_dir().join("connection.json")
    }

    /// Return the path to the Viewer host broker audit log.
    pub fn host_broker_audit_log_path(&self) -> PathBuf {
        self.host_broker_dir().join("audit.jsonl")
    }

    /// Return the path where a bundled `uv` binary would live.
    ///
    /// Layout: `{app_dir}/bundled/uv` (Unix) or `{app_dir}/bundled/uv.exe` (Windows).
    pub fn bundled_uv_path(&self) -> PathBuf {
        self.app_dir.join("bundled").join(uv_binary_name())
    }

    /// Return the path where a development-checkout bundled `uv` binary lives.
    ///
    /// Layout: `{workspace_root}/tobkiri_runtime/bundled/uv` (Unix) or
    /// `{workspace_root}/tobkiri_runtime/bundled/uv.exe` (Windows).
    pub fn dev_bundled_uv_path(&self) -> Option<PathBuf> {
        self.dev_workspace_root.as_ref().map(|root| {
            root.join("tobkiri_runtime")
                .join("bundled")
                .join(uv_binary_name())
        })
    }

    /// Resolve a trusted `uv` binary path.
    ///
    /// Runtime downloads are intentionally not part of this trust boundary. The
    /// viewer may use a bundled `uv`, a development-checkout bundle, an explicit
    /// `RUMI_UV_PATH`, or a user-managed `uv` on PATH.
    pub fn trusted_uv_path(&self) -> Option<PathBuf> {
        let bundled = self.bundled_uv_path();
        if bundled.exists() {
            return Some(bundled);
        }

        if let Some(dev_bundled) = self.dev_bundled_uv_path() {
            if dev_bundled.exists() {
                return Some(dev_bundled);
            }
        }

        if let Some(configured) = configured_uv_path() {
            if configured.is_file() {
                return Some(configured);
            }
        }

        which::which(uv_binary_name()).ok()
    }

    pub fn is_dev_workspace(&self) -> bool {
        self.dev_workspace_root.is_some()
    }

    /// Resolve `pack-shell` only from the packaged application root.
    pub fn pack_shell_path(&self) -> Option<PathBuf> {
        self.ensure_pack_shell_path().ok()
    }

    /// Require packaged `pack-shell`; no environment, PATH, or build fallback exists.
    pub fn ensure_pack_shell_path(&self) -> Result<PathBuf> {
        self.bundled_pack_shell_path().context(
            "verified packaged pack-shell is missing; environment, PATH, and source-build fallbacks are disabled",
        )
    }

    fn bundled_pack_shell_path(&self) -> Option<PathBuf> {
        let bundled = self.app_dir.join("bundled").join(pack_shell_binary_name());
        if bundled.is_file() {
            Some(bundled)
        } else {
            None
        }
    }

    /// Return the path where the desktop API token is stored.
    ///
    /// Layout: `{app_data_dir}/.desktop_api_token`
    pub fn desktop_api_token_path(&self) -> PathBuf {
        self.user_data_dir
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| self.user_data_dir.clone())
            .join(".desktop_api_token")
    }
}

fn find_dev_workspace_root(resource_dir: &Path) -> Option<PathBuf> {
    for ancestor in resource_dir.ancestors() {
        let candidate = ancestor.join("tobkiri_runtime");
        if candidate.join("app.py").exists() {
            return Some(ancestor.to_path_buf());
        }
    }
    None
}

fn is_cargo_debug_resource_dir(resource_dir: &Path) -> bool {
    let mut components = resource_dir.components().rev();
    matches!(
        (
            components
                .next()
                .and_then(|component| component.as_os_str().to_str()),
            components
                .next()
                .and_then(|component| component.as_os_str().to_str()),
        ),
        (Some("debug"), Some("target"))
    )
}

/// Match only a debug macOS bundle produced inside this checkout's Cargo target.
/// The same layout gate is used for runtime and PackVM helper discovery.
pub(crate) fn debug_macos_bundle_for_resources(
    target_root: &Path,
    resource_dir: &Path,
) -> Option<PathBuf> {
    let relative = resource_dir.strip_prefix(target_root).ok()?;
    let parts = relative
        .components()
        .map(|part| part.as_os_str())
        .collect::<Vec<_>>();
    let offset = match parts.len() {
        6 if parts[0] == "debug" => 0,
        7 if parts[1] == "debug" => 1,
        _ => return None,
    };
    if parts[offset + 1] != "bundle"
        || parts[offset + 2] != "macos"
        || parts[offset + 4] != "Contents"
        || parts[offset + 5] != "Resources"
    {
        return None;
    }
    let bundle = resource_dir.parent()?.parent()?;
    (bundle.extension().and_then(|value| value.to_str()) == Some("app"))
        .then(|| bundle.to_path_buf())
}

fn is_explicit_local_development_workspace_build() -> bool {
    LOCAL_DEV_WORKSPACE_BUILD == Some("1")
}

/// Select the venv that belongs to the runtime boundary selected for a
/// development launch.
///
/// An unbundled debug `.app` deliberately runs the checkout runtime and must
/// therefore use the checkout's `.venv`. A debug `.app` that contains a
/// staged runtime instead uses the staged development venv copied beside that
/// runtime. Production launches never call this helper.
fn development_venv_dir(app_dir: &Path, workspace_root: &Path, is_app_bundle: bool) -> PathBuf {
    let workspace_runtime = workspace_root.join("tobkiri_runtime");
    if !is_app_bundle || app_dir == workspace_runtime {
        workspace_root.join(".venv")
    } else {
        app_dir.join("dev-venv")
    }
}

/// Keep development state stable for one executable while isolating changed binaries.
/// The target tree is ignored by Git and never overlaps the installed app data.
fn development_writable_root(workspace_root: &Path, executable: &Path) -> Result<PathBuf> {
    let metadata = fs::symlink_metadata(executable).with_context(|| {
        format!(
            "failed to inspect development executable {}",
            executable.display()
        )
    })?;
    if !metadata.is_file() || redirected_path(&metadata) {
        bail!(
            "development executable is not a regular, unredirected file: {}",
            executable.display()
        );
    }
    let mut executable_file = fs::File::open(executable).with_context(|| {
        format!(
            "failed to open development executable {}",
            executable.display()
        )
    })?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = executable_file.read(&mut buffer).with_context(|| {
            format!(
                "failed to hash development executable {}",
                executable.display()
            )
        })?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }

    let launcher_root = workspace_root.join("tobkiri_launcher");
    let cargo_root = launcher_root.join("src-tauri");
    let target_root = cargo_root.join("target");
    let state_root = target_root.join("dev-state");
    let builds_root = state_root.join("builds");
    let writable_root = builds_root.join(format!("{:x}", hasher.finalize()));
    // A planted link in a persistent development path must not redirect reads
    // or writes outside the ignored checkout state tree. Check children used
    // immediately by setup as well as the parent directories.
    for path in [
        &launcher_root,
        &cargo_root,
        &target_root,
        &state_root,
        &builds_root,
        &writable_root,
        &writable_root.join("python"),
        &writable_root.join("user_data"),
        &writable_root.join("user_data/host_broker"),
        &writable_root.join("user_data/profiles"),
        &writable_root.join("user_data/packvm-vz"),
        &writable_root.join("logs"),
    ] {
        match fs::symlink_metadata(path) {
            Ok(metadata) if metadata.is_dir() && !redirected_path(&metadata) => {}
            Ok(_) => bail!(
                "development state path is not an unredirected directory: {}",
                path.display()
            ),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error).with_context(|| {
                    format!(
                        "failed to inspect development state path {}",
                        path.display()
                    )
                })
            }
        }
    }
    Ok(writable_root)
}

fn redirected_path(metadata: &fs::Metadata) -> bool {
    if metadata.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;

        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return true;
        }
    }
    false
}

fn configured_uv_path() -> Option<PathBuf> {
    std::env::var_os(UV_PATH_ENV)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

/// Return the platform-appropriate file name for the `uv` binary.
fn uv_binary_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "uv.exe"
    } else {
        "uv"
    }
}

/// Return the platform-appropriate file name for the `pack-shell` binary.
fn pack_shell_binary_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "pack-shell.exe"
    } else {
        "pack-shell"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn detect_for_tauri_produces_valid_paths() {
        let resource = PathBuf::from("/tmp/test_resource");
        let appdata = PathBuf::from("/tmp/test_appdata");
        let config = AppConfig::detect_for_tauri(resource, appdata).unwrap();
        assert!(config.app_dir.to_string_lossy().contains("test_resource"));
        assert!(config.python_dir.to_string_lossy().contains("test_appdata"));
        assert_eq!(config.rumi_home, config.app_dir);
        assert!(!config.is_dev_workspace());
    }

    #[test]
    fn app_config_separates_app_dir_from_user_data_dir() {
        let config = AppConfig::detect_for_tauri(
            PathBuf::from("/tmp/resources"),
            PathBuf::from("/tmp/app-data"),
        )
        .unwrap();

        assert_eq!(config.app_dir, PathBuf::from("/tmp/resources/app"));
        assert_eq!(config.rumi_home, PathBuf::from("/tmp/resources/app"));
        assert_eq!(
            config.user_data_dir,
            PathBuf::from("/tmp/app-data/user_data")
        );
    }

    #[test]
    fn isolated_writable_state_rebases_every_app_data_path() {
        let mut config = AppConfig::detect_for_tauri(
            PathBuf::from("/tmp/resources"),
            PathBuf::from("/tmp/shared-app-data"),
        )
        .unwrap();
        let original_app_dir = config.app_dir.clone();
        let state_root = PathBuf::from("/tmp/owned-debug-supervisor");
        let user_data = state_root.join("viewer_user_data");

        config.isolate_writable_state(&state_root, user_data.clone());

        assert_eq!(config.app_dir, original_app_dir);
        assert_eq!(config.rumi_home, original_app_dir);
        assert_eq!(config.python_dir, state_root.join("python"));
        assert_eq!(config.uv_path, state_root.join(uv_binary_name()));
        assert_eq!(config.venv_dir, state_root.join("venv"));
        assert_eq!(config.user_data_dir, user_data);
        assert_eq!(config.log_dir, state_root.join("logs"));
        assert_eq!(
            config.panel_bootstrap_secret_path(),
            state_root.join(format!(
                ".rumi_panel_bootstrap_secret.{}",
                std::process::id()
            ))
        );
    }

    #[test]
    fn isolated_writable_state_keeps_the_development_workspace_venv() {
        let workspace_root = PathBuf::from("/tmp/tobkiri-workspace");
        let workspace_venv = workspace_root.join(".venv");
        let mut config = AppConfig {
            app_dir: workspace_root.join("tobkiri_runtime"),
            rumi_home: workspace_root.join("tobkiri_runtime"),
            python_dir: PathBuf::from("/tmp/shared-app-data/python"),
            uv_path: PathBuf::from("/tmp/shared-app-data/uv"),
            venv_dir: workspace_venv.clone(),
            user_data_dir: PathBuf::from("/tmp/shared-app-data/user_data"),
            log_dir: PathBuf::from("/tmp/shared-app-data/logs"),
            kernel_port: 8765,
            dev_workspace_root: Some(workspace_root),
        };
        let state_root = PathBuf::from("/tmp/owned-debug-supervisor");

        config.isolate_writable_state(&state_root, state_root.join("viewer_user_data"));

        assert_eq!(config.venv_dir, workspace_venv);
        assert_eq!(config.python_dir, state_root.join("python"));
        assert_eq!(config.uv_path, state_root.join(uv_binary_name()));
    }

    #[test]
    fn venv_python_path_is_reasonable() {
        let resource = PathBuf::from("/tmp/res");
        let appdata = PathBuf::from("/tmp/data");
        let config = AppConfig::detect_for_tauri(resource, appdata).unwrap();
        let vp = config.venv_python();
        assert!(vp.to_string_lossy().contains("venv"));
    }

    #[test]
    fn panel_bootstrap_secret_path_uses_appdata_root() {
        let resource = PathBuf::from("/tmp/res");
        let appdata = PathBuf::from("/tmp/data");
        let config = AppConfig::detect_for_tauri(resource, appdata).unwrap();
        assert_eq!(
            config.panel_bootstrap_secret_path(),
            PathBuf::from("/tmp/data").join(format!(
                ".rumi_panel_bootstrap_secret.{}",
                std::process::id()
            ))
        );
    }

    #[test]
    fn detect_for_tauri_falls_back_to_repo_checkout_in_dev() {
        use std::fs;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_launcher_config_{unique}"));
        let resource = root
            .join("tobkiri_launcher")
            .join("src-tauri")
            .join("target")
            .join("debug");
        let appdata = root.join("appdata");
        let app_py = root.join("tobkiri_runtime").join("app.py");

        fs::create_dir_all(&resource).unwrap();
        fs::create_dir_all(app_py.parent().unwrap()).unwrap();
        fs::write(&app_py, "print('ok')\n").unwrap();

        let config = AppConfig::detect_for_tauri(resource, appdata).unwrap();
        assert_eq!(config.app_dir, root.join("tobkiri_runtime"));
        assert_eq!(config.venv_dir, root.join(".venv"));
        assert_eq!(
            config.user_data_dir,
            development_writable_root(&root, &std::env::current_exe().unwrap())
                .unwrap()
                .join("user_data")
        );
        assert!(config.is_dev_workspace());

        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn development_writable_root_reuses_one_executable_and_isolates_a_rebuild() {
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_dev_build_state_{unique}"));
        let executable = root.join("launcher-binary");
        let installed_state = root.join("installed-state");
        fs::create_dir_all(&root).unwrap();
        fs::write(&executable, b"first build").unwrap();

        let first = development_writable_root(&root, &executable).unwrap();
        assert!(first.starts_with(root.join("tobkiri_launcher/src-tauri/target/dev-state/builds")));
        assert!(!first.starts_with(&installed_state));
        fs::create_dir_all(first.join("user_data/profiles")).unwrap();
        fs::write(
            first.join("user_data/profiles/active.json"),
            b"saved activation",
        )
        .unwrap();

        let restarted = development_writable_root(&root, &executable).unwrap();
        assert_eq!(restarted, first);
        assert_eq!(
            fs::read(restarted.join("user_data/profiles/active.json")).unwrap(),
            b"saved activation"
        );

        fs::write(&executable, b"rebuilt binary").unwrap();
        let rebuilt = development_writable_root(&root, &executable).unwrap();
        assert_ne!(rebuilt, first);
        assert!(!rebuilt.join("user_data/profiles/active.json").exists());

        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn development_writable_root_rejects_redirected_executable() {
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_dev_executable_link_{unique}"));
        fs::create_dir_all(&root).unwrap();
        let executable = root.join("launcher-binary");
        let link = root.join("launcher-link");
        fs::write(&executable, b"build").unwrap();
        #[cfg(unix)]
        let linked = std::os::unix::fs::symlink(&executable, &link);
        #[cfg(windows)]
        let linked = std::os::windows::fs::symlink_file(&executable, &link);
        if linked.is_ok() {
            assert!(development_writable_root(&root, &link).is_err());
        }
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn development_writable_root_rejects_preplanted_state_link() {
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_dev_state_link_{unique}"));
        let target = root.join("tobkiri_launcher/src-tauri/target");
        let installed_state = root.join("installed-state");
        let executable = root.join("launcher-binary");
        fs::create_dir_all(&target).unwrap();
        fs::create_dir_all(&installed_state).unwrap();
        fs::write(&executable, b"build").unwrap();
        #[cfg(unix)]
        let linked = std::os::unix::fs::symlink(&installed_state, target.join("dev-state"));
        #[cfg(windows)]
        let linked = std::os::windows::fs::symlink_dir(&installed_state, target.join("dev-state"));
        if linked.is_ok() {
            assert!(development_writable_root(&root, &executable).is_err());
            assert!(fs::read_dir(&installed_state).unwrap().next().is_none());
        }
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn detect_for_tauri_does_not_trust_ancestors_when_bundled_app_exists() {
        use std::fs;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_launcher_config_staged_{unique}"));
        let resource = root.join("resources");
        let staged_app_py = resource.join("app").join("app.py");
        let repo_app_py = root.join("tobkiri_runtime").join("app.py");

        fs::create_dir_all(staged_app_py.parent().unwrap()).unwrap();
        fs::create_dir_all(repo_app_py.parent().unwrap()).unwrap();
        fs::write(&staged_app_py, "print('staged')\n").unwrap();
        fs::write(&repo_app_py, "print('repo')\n").unwrap();

        let config = AppConfig::detect_for_tauri(resource.clone(), root.join("appdata")).unwrap();

        assert_eq!(config.app_dir, resource.join("app"));
        assert_eq!(config.dev_workspace_root, None);
        assert!(!config.is_dev_workspace());

        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn detect_for_tauri_prefers_repo_checkout_over_stale_debug_bundle() {
        use std::fs;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("tobkiri_launcher_config_debug_staged_{unique}"));
        let resource = root
            .join("tobkiri_launcher")
            .join("src-tauri")
            .join("target")
            .join("debug");
        let staged_app_py = resource.join("app").join("app.py");
        let repo_app_py = root.join("tobkiri_runtime").join("app.py");

        fs::create_dir_all(staged_app_py.parent().unwrap()).unwrap();
        fs::create_dir_all(repo_app_py.parent().unwrap()).unwrap();
        fs::write(&staged_app_py, "print('staged')\n").unwrap();
        fs::write(&repo_app_py, "print('repo')\n").unwrap();

        let config = AppConfig::detect_for_tauri(resource.clone(), root.join("appdata")).unwrap();

        assert_eq!(config.app_dir, root.join("tobkiri_runtime"));
        assert_eq!(config.dev_workspace_root, Some(root.clone()));
        assert_eq!(config.venv_dir, root.join(".venv"));
        assert!(config.is_dev_workspace());

        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn detect_for_tauri_recognizes_target_triple_debug_directory() {
        use std::fs;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_launcher_config_target_{unique}"));
        let resource = root.join("tobkiri_launcher/src-tauri/target/aarch64-apple-darwin/debug");
        let staged_app_py = resource.join("app/app.py");
        let repo_app_py = root.join("tobkiri_runtime/app.py");
        fs::create_dir_all(staged_app_py.parent().unwrap()).unwrap();
        fs::create_dir_all(repo_app_py.parent().unwrap()).unwrap();
        fs::write(&staged_app_py, "print('staged')\n").unwrap();
        fs::write(&repo_app_py, "print('repo')\n").unwrap();

        let config = AppConfig::detect_for_tauri(resource.clone(), root.join("appdata")).unwrap();

        assert_eq!(config.app_dir, root.join("tobkiri_runtime"));
        assert_eq!(config.venv_dir, root.join(".venv"));
        assert!(config.is_dev_workspace());
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn detect_for_tauri_recognizes_debug_app_bundle_resources() {
        use std::fs;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_launcher_config_app_{unique}"));
        let resource = root.join(
            "tobkiri_launcher/src-tauri/target/debug/bundle/macos/Tobkiri Launcher.app/Contents/Resources",
        );
        let staged_app_py = resource.join("app/app.py");
        let repo_app_py = root.join("tobkiri_runtime/app.py");
        fs::create_dir_all(staged_app_py.parent().unwrap()).unwrap();
        fs::create_dir_all(repo_app_py.parent().unwrap()).unwrap();
        fs::write(&staged_app_py, "print('staged')\n").unwrap();
        fs::write(&repo_app_py, "print('repo')\n").unwrap();

        let config = AppConfig::detect_for_tauri(resource.clone(), root.join("appdata")).unwrap();

        assert_eq!(config.app_dir, resource.join("app"));
        assert_eq!(config.dev_workspace_root, Some(root.clone()));
        assert_eq!(config.venv_dir, resource.join("app/dev-venv"));
        assert_eq!(
            config.user_data_dir,
            development_writable_root(&root, &std::env::current_exe().unwrap())
                .unwrap()
                .join("user_data")
        );
        assert!(config.is_dev_workspace());
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn debug_app_without_resource_map_keeps_local_runtime_isolated() {
        use std::fs;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_debug_unbundled_{unique}"));
        let resource = root.join(
            "tobkiri_launcher/src-tauri/target/debug/bundle/macos/Tobkiri Launcher Developer.app/Contents/Resources",
        );
        let staged = root.join("tobkiri_launcher/src-tauri/gen/app");
        fs::create_dir_all(&resource).unwrap();
        fs::create_dir_all(&staged).unwrap();
        fs::create_dir_all(root.join("tobkiri_runtime")).unwrap();
        fs::write(staged.join("app.py"), "print('staged')\n").unwrap();
        fs::write(root.join("tobkiri_runtime/app.py"), "print('repo')\n").unwrap();

        let config = AppConfig::detect_for_tauri(resource, root.join("installed-state")).unwrap();
        if is_explicit_local_development_workspace_build() {
            assert_eq!(config.app_dir, root.join("tobkiri_runtime"));
            assert_eq!(config.venv_dir, root.join(".venv"));
        } else {
            assert_eq!(config.app_dir, staged);
            assert_eq!(config.venv_dir, staged.join("dev-venv"));
        }
        assert!(config
            .user_data_dir
            .starts_with(root.join("tobkiri_launcher/src-tauri/target/dev-state")));
        assert!(!config
            .user_data_dir
            .starts_with(root.join("installed-state")));
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn explicit_local_debug_app_does_not_consume_stale_resource_runtime() {
        use std::fs;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_local_stale_resource_{unique}"));
        let resources = root.join(
            "tobkiri_launcher/src-tauri/target/debug/bundle/macos/Tobkiri Launcher Developer.app/Contents/Resources",
        );
        fs::create_dir_all(resources.join("app")).unwrap();
        fs::create_dir_all(root.join("tobkiri_runtime")).unwrap();
        fs::write(resources.join("app/app.py"), "print('stale')\n").unwrap();
        fs::write(root.join("tobkiri_runtime/app.py"), "print('checkout')\n").unwrap();

        let config =
            AppConfig::detect_for_tauri(resources.clone(), root.join("installed-state")).unwrap();
        if is_explicit_local_development_workspace_build() {
            assert_eq!(config.app_dir, root.join("tobkiri_runtime"));
            assert_eq!(config.venv_dir, root.join(".venv"));
        } else {
            assert_eq!(config.app_dir, resources.join("app"));
            assert_eq!(config.venv_dir, resources.join("app/dev-venv"));
        }
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn development_app_bundle_venv_follows_the_selected_runtime_boundary() {
        let workspace_root = PathBuf::from("/tmp/tobkiri-workspace");
        let workspace_runtime = workspace_root.join("tobkiri_runtime");
        let staged_runtime = PathBuf::from("/tmp/Launcher.app/Contents/Resources/app");

        assert_eq!(
            development_venv_dir(&workspace_runtime, &workspace_root, true),
            workspace_root.join(".venv"),
            "an unbundled debug app uses the checkout runtime and venv"
        );
        assert_eq!(
            development_venv_dir(&staged_runtime, &workspace_root, true),
            staged_runtime.join("dev-venv"),
            "a staged debug app uses the venv staged with that runtime"
        );
    }

    #[test]
    fn trusted_uv_path_prefers_bundled_copy_in_app_dir() {
        use std::fs;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_launcher_uv_app_bundle_{unique}"));
        let resource = root.join("resources");
        let app_dir = resource.join("app");
        let appdata = root.join("appdata");
        let bundled_uv = app_dir.join("bundled").join(uv_binary_name());

        fs::create_dir_all(bundled_uv.parent().unwrap()).unwrap();
        fs::write(&bundled_uv, b"uv").unwrap();

        let config = AppConfig::detect_for_tauri(resource, appdata.clone()).unwrap();
        assert_eq!(
            config.trusted_uv_path().as_deref(),
            Some(bundled_uv.as_path())
        );
        assert_eq!(config.bundled_uv_path(), bundled_uv);

        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn trusted_uv_path_prefers_dev_workspace_bundle_over_appdata_uv() {
        use std::fs;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("tobkiri_launcher_uv_dev_bundle_{unique}"));
        let resource = root
            .join("tobkiri_launcher")
            .join("src-tauri")
            .join("target")
            .join("debug");
        let appdata = root.join("appdata");
        let app_py = root.join("tobkiri_runtime").join("app.py");
        let dev_bundled_uv = root
            .join("tobkiri_runtime")
            .join("bundled")
            .join(uv_binary_name());

        fs::create_dir_all(&resource).unwrap();
        fs::create_dir_all(app_py.parent().unwrap()).unwrap();
        fs::create_dir_all(dev_bundled_uv.parent().unwrap()).unwrap();
        fs::write(&app_py, "print('ok')\n").unwrap();
        fs::write(&dev_bundled_uv, b"uv").unwrap();

        let config = AppConfig::detect_for_tauri(resource, appdata.clone()).unwrap();
        assert_eq!(
            config.dev_bundled_uv_path().as_deref(),
            Some(dev_bundled_uv.as_path())
        );
        assert_eq!(
            config.trusted_uv_path().as_deref(),
            Some(dev_bundled_uv.as_path())
        );
        assert!(config.is_dev_workspace());

        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn trusted_uv_path_ignores_appdata_uv_when_no_trusted_source_exists() {
        use std::fs;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("tobkiri_launcher_uv_appdata_ignored_{unique}"));
        let resource = root.join("resources");
        let appdata = root.join("appdata");

        fs::create_dir_all(&appdata).unwrap();
        fs::write(appdata.join(uv_binary_name()), b"uv").unwrap();

        let old_path = std::env::var_os("PATH");
        let old_uv_path = std::env::var_os(UV_PATH_ENV);
        std::env::set_var("PATH", "");
        std::env::remove_var(UV_PATH_ENV);
        let config = AppConfig::detect_for_tauri(resource, appdata.clone()).unwrap();

        assert_eq!(config.trusted_uv_path(), None);
        assert_eq!(config.uv_path, appdata.join(uv_binary_name()));

        if let Some(path) = old_path {
            std::env::set_var("PATH", path);
        } else {
            std::env::remove_var("PATH");
        }
        if let Some(path) = old_uv_path {
            std::env::set_var(UV_PATH_ENV, path);
        } else {
            std::env::remove_var(UV_PATH_ENV);
        }
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn ensure_pack_shell_path_uses_staged_bundle_without_building_ancestor_workspace() {
        use std::fs;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("tobkiri_launcher_pack_shell_staged_{unique}"));
        let resource = root.join("resources");
        let appdata = root.join("appdata");
        let app_py = root.join("tobkiri_runtime").join("app.py");
        let manifest = root.join("pack-shell").join("Cargo.toml");
        let staged_pack_shell = resource
            .join("app")
            .join("bundled")
            .join(pack_shell_binary_name());

        fs::create_dir_all(&resource).unwrap();
        fs::create_dir_all(app_py.parent().unwrap()).unwrap();
        fs::create_dir_all(manifest.parent().unwrap()).unwrap();
        fs::create_dir_all(staged_pack_shell.parent().unwrap()).unwrap();
        fs::write(&app_py, "print('ok')\n").unwrap();
        fs::write(&manifest, "[package]\nname = \"pack-shell\"\n").unwrap();
        fs::write(&staged_pack_shell, b"staged-pack-shell").unwrap();

        let config = AppConfig::detect_for_tauri(resource, appdata).unwrap();
        let resolved = config.ensure_pack_shell_path().unwrap();

        assert_eq!(resolved, staged_pack_shell);

        fs::remove_dir_all(&root).ok();
    }
}
