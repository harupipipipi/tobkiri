//! Path resolution and application configuration for Tauri.
//!
//! All paths are derived from Tauri's `resource_dir` and `app_data_dir`
//! so that the application works correctly when bundled.

use anyhow::{Context, Result};
use std::path::{Path, PathBuf};

const UV_PATH_ENV: &str = "RUMI_UV_PATH";

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
        let prefer_dev_runtime =
            cfg!(debug_assertions) && is_cargo_debug_resource_dir(&resource_dir);
        let dev_workspace_root = if prefer_dev_runtime {
            detected_workspace_root
        } else if staged_app_dir.exists() {
            None
        } else {
            detected_workspace_root
        };

        let mut app_dir = staged_app_dir;
        if let Some(workspace_root) = &dev_workspace_root {
            let candidate = workspace_root.join("tobkiri_runtime");
            if candidate.join("app.py").exists() {
                app_dir = candidate;
            }
        }
        let rumi_home = app_dir.clone();

        let python_dir = app_data_dir.join("python");
        let uv_path = if cfg!(target_os = "windows") {
            app_data_dir.join("uv.exe")
        } else {
            app_data_dir.join("uv")
        };
        let venv_dir = app_data_dir.join("venv");
        let user_data_dir = app_data_dir.join("user_data");
        let log_dir = app_data_dir.join("logs");

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
    /// root. Keeping Python, the virtual environment, logs, secrets, and user
    /// data together prevents a debug process from mutating production app
    /// data or another concurrently running Viewer.
    pub fn isolate_writable_state(&mut self, state_root: &Path, user_data_dir: PathBuf) {
        self.python_dir = state_root.join("python");
        self.uv_path = state_root.join(uv_binary_name());
        self.venv_dir = state_root.join("venv");
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
        self.user_data_dir
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| self.user_data_dir.clone())
            .join(".rumi_panel_bootstrap_secret")
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
            state_root.join(".rumi_panel_bootstrap_secret")
        );
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
            PathBuf::from("/tmp/data/.rumi_panel_bootstrap_secret")
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
        assert!(config.is_dev_workspace());

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
        assert!(config.is_dev_workspace());

        fs::remove_dir_all(&root).ok();
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
