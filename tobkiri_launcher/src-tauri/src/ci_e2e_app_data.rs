//! Explicit app-data isolation for the non-publishable macOS CI-E2E artifact.

use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

const CI_E2E_BUNDLE_IDENTIFIER: &str = "dev.tobkiri.launcher.ci-e2e";
const CI_E2E_APP_DATA_ROOT_ENV: &str = "TOBKIRI_CI_E2E_APP_DATA_ROOT";
const CI_E2E_APP_DATA_ROOT_NAME: &str = "ci-e2e-app-data";

/// Resolve the opt-in app-data root for a native CI-E2E launch.
///
/// Tauri's macOS app-data directory is derived from the native user account,
/// not the process `HOME` environment variable. The override is therefore
/// accepted only by the non-publishable CI-E2E identifier. It is rejected,
/// rather than falling back to the real app-data directory, when the caller
/// supplies an unsafe path.
pub(crate) fn resolve_app_data_dir_from_env(
    identifier: &str,
    default_path: &Path,
) -> Result<PathBuf> {
    resolve_app_data_dir(
        identifier,
        default_path,
        std::env::var_os(CI_E2E_APP_DATA_ROOT_ENV).as_deref(),
    )
}

fn resolve_app_data_dir(
    identifier: &str,
    default_path: &Path,
    override_value: Option<&OsStr>,
) -> Result<PathBuf> {
    if identifier != CI_E2E_BUNDLE_IDENTIFIER {
        return Ok(default_path.to_path_buf());
    }
    let Some(override_value) = override_value else {
        return Ok(default_path.to_path_buf());
    };
    let path = PathBuf::from(override_value);
    if !secure_app_data_root(&path) {
        bail!(
            "{} must name an owned, non-symlinked, 0700 absolute {} directory under a secure parent",
            CI_E2E_APP_DATA_ROOT_ENV,
            CI_E2E_APP_DATA_ROOT_NAME,
        );
    }
    Ok(path)
}

fn secure_app_data_root(path: &Path) -> bool {
    if !is_clean_absolute_path(path)
        || path.file_name().and_then(|name| name.to_str()) != Some(CI_E2E_APP_DATA_ROOT_NAME)
    {
        return false;
    }
    let Some(parent) = path.parent() else {
        return false;
    };
    if !secure_directory(parent) {
        return false;
    }
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    metadata.is_dir()
        && !metadata.file_type().is_symlink()
        && path.canonicalize().ok().as_deref() == Some(path)
        && secure_permissions_and_owner(&metadata)
}

fn is_clean_absolute_path(path: &Path) -> bool {
    path.is_absolute()
        && path.components().all(|component| {
            matches!(
                component,
                std::path::Component::RootDir | std::path::Component::Normal(_)
            ) || cfg!(windows) && matches!(component, std::path::Component::Prefix(_))
        })
}

fn secure_directory(path: &Path) -> bool {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    metadata.is_dir()
        && !metadata.file_type().is_symlink()
        && path.canonicalize().ok().as_deref() == Some(path)
        && secure_permissions_and_owner(&metadata)
}

fn secure_permissions_and_owner(metadata: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};

        unsafe extern "C" {
            fn geteuid() -> u32;
        }

        metadata.permissions().mode() & 0o777 == 0o700 && metadata.uid() == unsafe { geteuid() }
    }
    #[cfg(not(unix))]
    {
        let _ = metadata;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn fixture_root(label: &str) -> PathBuf {
        std::env::temp_dir().canonicalize().unwrap().join(format!(
            "tobkiri_ci_e2e_app_data_{label}_{}_{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    #[test]
    fn override_is_opt_in_and_identifier_scoped() {
        let root = fixture_root("scoped");
        fs::create_dir(&root).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;

            fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let override_path = root.join(CI_E2E_APP_DATA_ROOT_NAME);
        fs::create_dir(&override_path).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;

            fs::set_permissions(&override_path, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let default_path = PathBuf::from("/Users/example/Library/Application Support/default");

        assert_eq!(
            resolve_app_data_dir(
                "dev.rumiai.app",
                &default_path,
                Some(OsStr::new("relative")),
            )
            .unwrap(),
            default_path
        );
        assert_eq!(
            resolve_app_data_dir(CI_E2E_BUNDLE_IDENTIFIER, &default_path, None).unwrap(),
            default_path
        );
        assert_eq!(
            resolve_app_data_dir(
                CI_E2E_BUNDLE_IDENTIFIER,
                &default_path,
                Some(override_path.as_os_str()),
            )
            .unwrap(),
            override_path
        );

        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn unsafe_override_roots_fail_closed() {
        use std::os::unix::fs::{symlink, PermissionsExt};

        let root = fixture_root("unsafe");
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let default_path = PathBuf::from("/Users/example/Library/Application Support/default");

        for invalid in [
            PathBuf::from("relative/ci-e2e-app-data"),
            root.join("wrong-name"),
        ] {
            assert!(resolve_app_data_dir(
                CI_E2E_BUNDLE_IDENTIFIER,
                &default_path,
                Some(invalid.as_os_str()),
            )
            .is_err());
        }

        let permissive = root.join(CI_E2E_APP_DATA_ROOT_NAME);
        fs::create_dir(&permissive).unwrap();
        fs::set_permissions(&permissive, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(resolve_app_data_dir(
            CI_E2E_BUNDLE_IDENTIFIER,
            &default_path,
            Some(permissive.as_os_str()),
        )
        .is_err());
        fs::remove_dir(&permissive).unwrap();

        let external = root.join("external");
        fs::create_dir(&external).unwrap();
        fs::set_permissions(&external, fs::Permissions::from_mode(0o700)).unwrap();
        let symlinked = root.join(CI_E2E_APP_DATA_ROOT_NAME);
        symlink(&external, &symlinked).unwrap();
        assert!(resolve_app_data_dir(
            CI_E2E_BUNDLE_IDENTIFIER,
            &default_path,
            Some(symlinked.as_os_str()),
        )
        .is_err());

        fs::remove_dir_all(root).unwrap();
    }
}
